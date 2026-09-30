"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
STATIC_DIR = Path(__file__).resolve().parent / "statics"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# ── 验证码缓存 ───────────────────────────────────────────────────────────────
CAPTCHA_CACHE_TTL = _int("CAPTCHA_CACHE_TTL", 45_000)          # ms
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（无浏览器：Node + jsdom 模拟浏览器环境，运行阿里云无痕 SDK）
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / "solver.js"
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 40)  # 每次求解超时（秒）

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 60)
# 限流（cooling）冷却时长（秒）
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)

# ── 上游端点 ─────────────────────────────────────────────────────────────────
# Coding Plan 走 ZCode 平台网关（/ultra=bigmodel，/ultra-zai=zai），凭证用 x-api-key。
# 旧的 /zcode-plan/ 路径已失效（401/405）。
UPSTREAM = {
    "zai": os.getenv(
        "ZAI_UPSTREAM_URL",
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
    ),
    "zai_fallback": os.getenv(
        "ZAI_FALLBACK_URL",
        "https://api.z.ai/api/anthropic/v1/messages",
    ),
    "bigmodel": os.getenv(
        "BIGMODEL_UPSTREAM_URL",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
    ),
}

# ── 反代模式 ─────────────────────────────────────────────────────────────────
# coding-plan：原版行为（Coding Plan 反代，走上面 UPSTREAM 的官方端点），一字未改。
# start-plan：新增路径，反代 ZCode 的 Start Plan 额度 —— 走 ZCode 平台网关的
#             /zcode-plan 路径，凭证是 ~/.zcode/v2/credentials.json 里的 zcodejwttoken
#             （JWT），且 body 的 system 必须带平台放行签名（见 zcode_signature.json）。
# 两种模式互不影响：start-plan 才启用签名注入、指纹头、额度耗尽退出等新行为。
PLAN_MODE = (os.getenv("ZCODE_PLAN_MODE", "start-plan") or "").strip().lower()
START_PLAN = PLAN_MODE == "start-plan"

# Start Plan 专用上游端点（coding-plan 模式不使用）
UPSTREAM["bigmodel_start_plan"] = os.getenv(
    "BIGMODEL_START_PLAN_URL",
    "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
)

# 放行签名数据文件（block0 + block1 前 1500 字符）
SIGNATURE_FILE = Path(__file__).resolve().parent / "zcode_signature.json"


def start_plan_signature() -> list[dict]:
    """返回要插入 system 最前面的签名块；读不到则返回空列表。"""
    try:
        with open(SIGNATURE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    blocks = [data.get("block0"), data.get("block1_head")]
    if not all(isinstance(b, str) and b for b in blocks):
        return []
    return [{"type": "text", "text": b, "cache_control": {"type": "ephemeral"}}
            for b in blocks]


# ZCode 客户端指纹头（平台网关按这些头识别合法客户端，缺了会被 WAF 判 unusual activity）
ZCODE_FINGERPRINT = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "mid-conversation-system-2026-04-07",
    "http-referer": "https://zcode.z.ai",
    "user-agent": "ZCode/3.14.4 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
    "x-client-language": "zh-CN",
    "x-client-timezone": "Asia/Shanghai",
    "x-os-category": "windows",
    "x-os-version": "10.0.26200",
    "x-platform": "win32-x64",
    "x-release-channel": "production",
    "x-title": "Z Code@electron",
    "x-zcode-agent": "glm",
    "x-zcode-app-version": "3.14.4",
    "x-zcode-session-type": "main",
}

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = "https://zcode.z.ai/api/v1/zcode-plan"

USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", "ZCode/3.0.1")
APP_VERSION = "2.0.0"
