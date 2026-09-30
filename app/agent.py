"""上游请求构建。

负责根据账号凭证选择端点、组装请求头。实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

import uuid

from . import settings
from .models import Account

# 透传客户端 header 时需要剔除的字段
_DROP_HEADERS = {
    "host",
    "content-length",
    "x-api-key",
    "authorization",
    "user-agent",
    "http-referer",
    "accept-encoding",
    "connection",
}

# 进程级会话 ID：ZCode 客户端在会话内保持稳定，我们跟随（每次重启网关换一个）
_SESSION_ID = str(uuid.uuid4())


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
) -> tuple[str, dict]:
    """返回 (目标 URL, 请求头)。"""
    provider = account.provider

    if settings.START_PLAN and account.mode == "start-plan":
        # Start Plan（新增路径）：平台网关按 ZCode 客户端指纹放行，客户端头一律不透传，
        # 避免 anthropic-beta / x-platform 等被改写而触发 WAF。
        token = account.jwt_token or account.api_key
        if not token:
            raise RuntimeError("Start Plan 账号缺少凭证")
        headers = {
            "content-type": "application/json",
            **settings.ZCODE_FINGERPRINT,
            "authorization": f"Bearer {token}",
            "x-api-key": token,
            "x-query-id": str(uuid.uuid4()),
            "x-request-id": str(uuid.uuid4()),
            "x-session-id": _SESSION_ID,
            "x-zcode-trace-id": str(uuid.uuid4()),
        }
        return settings.UPSTREAM["bigmodel_start_plan"], headers

    if provider == "zai":
        if account.mode == "jwt" and account.jwt_token:
            target_url = settings.UPSTREAM["zai"]
            auth = {"Authorization": f"Bearer {account.jwt_token}"}
        elif account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        target_url = settings.UPSTREAM["bigmodel"]
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": account.api_key}
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    headers = {
        "content-type": "application/json",
        **auth,
        "anthropic-version": "2023-06-01",
        "User-Agent": settings.USER_AGENT,
        "X-ZCode-App-Version": "3.0.1",
        "X-ZCode-Agent": "glm",
        "HTTP-Referer": "https://zcode.z.ai/",
    }
    if verify_param:
        headers["X-Aliyun-Captcha-Verify-Param"] = verify_param

    for key, value in (incoming_headers or {}).items():
        lower = key.lower()
        if lower in _DROP_HEADERS or lower.startswith("x-zcode"):
            continue
        headers[key] = value

    return target_url, headers
