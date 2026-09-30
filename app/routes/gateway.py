"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..quota import fetch_quota
from ..store import store

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# Start Plan 平台网关要求 metadata.user_id 是含 device_id / session_id 的 JSON 字符串。
# device_id 进程级生成一次（真实客户端也是稳定的设备 ID）。
_DEVICE_ID = str(uuid.uuid4())
_SESSION_ID = str(uuid.uuid4())

# Z.AI 上游模型名大小写敏感
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}

# /v1/models 对外公布的可用模型（随反代模式切换；coding-plan 保持原版列表）
AVAILABLE_MODELS = (
    ["GLM-5.3", "GLM-5.3-Flash"] if settings.START_PLAN else ["GLM-5.2", "GLM-5-Turbo"]
)

# 命中以下信号则认为账号额度用完
_EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")

# 明确的"额度耗尽"措辞：命中即终止整个反代进程（用户要求，避免继续发请求）。
# 这里刻意收窄，不含裸 "quota"/"balance"/"insufficient"，以免把限流误判成额度耗尽。
_QUOTA_FATAL_PHRASES = (
    "余额不足",
    "无可用资源包",
    "额度已用完",
    "insufficient balance",
    "no resource package",
    "quota exhausted",
    "insufficient quota",
)

# 额度耗尽时的进程退出码。
# 刻意用 0（正常退出）：Docker 下配合 `restart: on-failure` 即"额度耗尽停住，
# 真崩溃才重启"。若用非 0，容器会无限重启、继续发请求，与需求相反。
EXIT_CODE_QUOTA = 0


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _apply_start_plan(body: dict) -> dict:
    """Start Plan 平台网关要求：system 前置放行签名，metadata 为 ZCode 形状。

    签名必须原样位于最前（平台按它识别合法客户端）；客户端自己的 system 追加在其后，
    不被丢弃。客户端若已带同样签名（重复调用）则不重复插入。
    """
    signature = settings.start_plan_signature()
    if signature:
        system = body.get("system")
        already = (
            isinstance(system, list)
            and system
            and isinstance(system[0], dict)
            and system[0].get("text") == signature[0]["text"]
        )
        if not already:
            if isinstance(system, list):
                body["system"] = [*signature, *system]
            elif isinstance(system, str):
                body["system"] = [*signature, {"type": "text", "text": system}]
            else:
                body["system"] = list(signature)

    body["metadata"] = {
        "user_id": json.dumps(
            {"device_id": _DEVICE_ID, "account_uuid": "", "session_id": _SESSION_ID},
            separators=(",", ":"),
        )
    }
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _is_exhausted(status_code: int, text: str) -> bool:
    if status_code in (402,):
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _is_quota_fatal(status_code: int, text: str) -> bool:
    """是否属于"额度彻底耗尽"——命中则不再发任何请求，直接终止进程。"""
    if status_code in (402,):
        return True
    low = text.lower()
    return any(p in low for p in _QUOTA_FATAL_PHRASES)


def _fatal_exit(req_id: str, detail: str) -> None:
    """按用户要求：额度不足时终止整个反代进程，后续不再发任何请求。

    留 1 秒让当前 503 响应写完，再硬退出（os._exit 不等清理，确保没有后续请求）。
    """
    logs.warn(req_id, f"额度不足，终止反代程序（退出码 {EXIT_CODE_QUOTA}）：{detail[:200]}")

    def _die() -> None:
        time.sleep(1.0)
        os._exit(EXIT_CODE_QUOTA)

    threading.Thread(target=_die, daemon=True).start()


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)

    start_plan = settings.START_PLAN
    if start_plan:
        body = _apply_start_plan(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT
    payload = json.dumps(body).encode("utf-8")

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()
    if start_plan:
        # 策略：只用 Start Plan，绝不回退到其它端点/套餐。
        # ponytail: 目前只有一个 Start Plan 账号，直接取；多账号再补 round-robin。
        candidates = [a for a in store.list_accounts() if a.mode == "start-plan"]
    else:
        # 未显式指定 provider 时允许跨池兜底：首选池无可用账号就换另一个池，
        # 否则 zai 账号一旦全部失效，bigmodel 的可用号永远轮不到。
        providers = [provider] if incoming_headers.get("x-provider") else [
            provider,
            *[p for p in ("zai", "bigmodel") if p != provider],
        ]
        candidates = [a for p in providers for a in store.list_accounts(p)]

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = next((a for a in candidates if a.is_selectable() and a.id not in tried), None)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()


async def _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha):
    """尝试用单个账号转发，含验证码续期。返回 Response 或 _NEXT_ACCOUNT。"""
    for attempt in range(MAX_CAPTCHA_RETRIES):
        verify_param = None
        if needs_captcha:
            try:
                verify_param = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"人机校验失败: {err}")
                return JSONResponse(
                    {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                    status_code=500,
                )

        try:
            url, headers = build_request(account, body, verify_param, incoming_headers)
        except RuntimeError as err:
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            return _NEXT_ACCOUNT

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as err:
            await client.aclose()
            _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            return _NEXT_ACCOUNT

        status_code = resp.status_code

        if status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await cm.__aexit__(None, None, None)
            await client.aclose()

            if status_code == 403 and _is_captcha_error(text) and needs_captcha:
                captcha_manager.invalidate()
                logs.warn(req_id, f"账号 {account.name} 验证码失效，刷新重试")
                continue  # 同账号重试验证码

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                asyncio.create_task(_safe_refresh(account))
                # 仅 start-plan 模式下才"额度耗尽即退出整个进程"；coding-plan 保持原逻辑
                if settings.START_PLAN and _is_quota_fatal(status_code, text):
                    # 额度彻底耗尽：不再尝试其它账号，直接终止整个反代进程
                    _fatal_exit(req_id, text)
                    return JSONResponse(
                        {"error": {"message": "上游额度不足，反代进程即将退出",
                                   "type": "insufficient_quota"}},
                        status_code=503,
                    )
                return _NEXT_ACCOUNT

            if status_code in (401, 403):
                _mark(account, Status.INVALID, f"鉴权失败 HTTP {status_code}")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 {status_code}，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 429:
                _mark(account, Status.COOLING, "上游限流 429")
                logs.warn(req_id, f"账号 {account.name} 被限流 429，切换下一个")
                return _NEXT_ACCOUNT

            # 其它错误：直接回传客户端
            account.fail_count += 1
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            return JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            )

        # 成功：记录用量并流式透传
        account.use_count += 1
        account.last_used_at = time.time()
        if account.status in (Status.COOLING, Status.EXHAUSTED):
            account.status = Status.ACTIVE
        store.update_account(account)
        asyncio.create_task(_safe_refresh(account))

        content_type = resp.headers.get("content-type", "application/json")

        async def _body_iter():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                logs.req_ok(req_id)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

        out_headers = {"Cache-Control": "no-cache"}
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)

    # 验证码连续失败
    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
    return _NEXT_ACCOUNT


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
