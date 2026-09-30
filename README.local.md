# zcode2api 本地部署与使用指南

本文面向**要在本机调用这个网关的其他人和其他项目**：怎么启动、怎么发请求、它到底怎么工作、踩过哪些坑。

> 只想看「怎么调」→ 直接跳 [快速开始](#快速开始)。
> 想知道「为什么能通」→ 看 [原理](#原理)。

---

## 快速开始

### 1. 启动服务

```bash
cd C:\Users\yegetables\work\zcode2api
python main.py serve
```

默认监听 `0.0.0.0:3000`。本机访问用 `http://127.0.0.1:3000`。

> 若通过桌面 `ZCode中转服务.bat` 启动，端口以 `zcode2api\port.txt` 为准（当前为 **58171**），
> 用菜单 `[6] 修改端口` 可改。下面示例里的 3000 请按实际端口替换。

**不需要**做的：

- ❌ 不需要 `python main.py login zai` —— 账号已持久化在 `data/accounts.db`
- ❌ 不需要 `cd captcha_node && npm install` —— 现在走的链路不校验阿里云无痕验证

### 2. 确认账号状态

```bash
python main.py accounts
```

期望看到 `start-plan-xxxx  bigmodel  start-plan  active`（这是唯一参与轮询的账号）。

### 3. 发请求

```bash
curl -X POST http://127.0.0.1:3000/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"GLM-5.3-Flash","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'
```

流式（Anthropic SSE）：

```bash
curl -N -X POST http://127.0.0.1:3000/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"GLM-5.3-Flash","max_tokens":1024,"stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

---

## 其他项目如何调用

网关实现标准 Anthropic Messages API，任何认这个协议的东西直接指过来即可：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000
export ANTHROPIC_AUTH_TOKEN=dummy   # 网关未配网关 Key 时随便填
```

Claude Code：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:3000 claude
```

Python SDK：

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:3000", api_key="dummy")
resp = client.messages.create(
    model="GLM-5.3-Flash",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.content[0].text)
```

### 支持的模型

| 模型 | 说明 |
|------|------|
| `GLM-5.3-Flash` | 实测可用（Start Plan 主模型） |
| `GLM-5.3` | 实测可用 |

大小写不敏感，`glm-5.3-flash` 会被归一化成上游要求的写法。查询清单：

```bash
curl http://127.0.0.1:3000/v1/models
```

### 鉴权

网关 API Key 当前**未设置**（不校验）。要开启：后台 `http://127.0.0.1:3000/admin/login`（默认密码 `zcode`）里配置，之后请求须带 `Authorization: Bearer <key>` 或 `x-api-key: <key>`。

### 后台管理

`http://127.0.0.1:3000/admin/login`，默认密码 `zcode`。

---

## 反代模式（`ZCODE_PLAN_MODE`）

本项目支持两种互不干扰的反代模式，用环境变量切换：

| 模式 | 说明 |
|------|------|
| **`start-plan`**（默认） | 反代 **ZCode Start Plan** 额度。走 `zcode.z.ai/api/v1/zcode-plan/anthropic`，凭证是桌面端的 `zcodejwttoken`，网关自动注入平台放行签名 + ZCode 客户端指纹头。额度耗尽时整个进程退出。 |
| `coding-plan` | **原版行为**（上游代码一字未改）：Coding Plan 反代，走官方端点（`open.bigmodel.cn/api/anthropic`、`api.z.ai/api/anthropic`、`zcode-plan`），用 API Key / JWT，会走阿里云无痕验证。 |

切换方式：

```bash
# 直接跑
ZCODE_PLAN_MODE=coding-plan python main.py serve

# Docker：改 .env 或 compose 里的 ZCODE_PLAN_MODE
```

> 桌面启动器 `launcher\ZCode中转服务.bat` 不设这个变量，走代码默认值（`start-plan`）。

两种模式互不影响：只有 `start-plan` 才启用签名注入、ZCode 指纹头、只选 start-plan 账号、额度耗尽退出这些新行为；`coding-plan` 跑的还是上游原逻辑。

---

## 原理

### 整体结构

```
客户端 (curl / Anthropic SDK / Claude Code / 其他项目)
        │  POST /v1/messages  (Anthropic 协议)
        ▼
   zcode2api 网关 (FastAPI 单进程)
        │  ① 选账号（只选 start-plan）
        │  ② 改写 body：system 前置放行签名 + 规范化 metadata
        │  ③ 组装 ZCode 客户端指纹头
        ▼
   ZCode 平台网关  https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages
        │  x-api-key + Authorization: Bearer <zcodejwttoken>
        ▼
   GLM-5.3 / GLM-5.3-Flash
```

除 `system` / `metadata` 两处外，body 原样透传（`tools`、`messages`、`stream`、`thinking` 等都不动）；响应含 SSE 流原样回传。

### Start Plan 链路（本次改造核心）

旧方案（已废弃）用 `~/.zcode/v2/credentials.json` 里 `individual-coding-plan` 的 API Key 打 `/ultra/` 端点。该 Key 的**充值余额已耗尽**（上游 429「余额不足或无可用资源包」），且平台查询明确返回 `当前用户不存在coding plan`。

现在改走 ZCode 客户端的真实链路 **Start Plan**：

| 要素 | 值 |
|------|-----|
| 端点 | `https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages` |
| 凭证 | `~/.zcode/v2/credentials.json` 解密出的 `zcodejwttoken`（JWT，无 `exp` 字段） |
| 鉴权头 | `x-api-key: <jwt>` **且** `Authorization: Bearer <jwt>`（两个都要） |

### 放行签名（最容易踩的坑）

`/zcode-plan/` 路径前置了**阿里云 ESA WAF**。它对请求体做特征校验：**body 的 `system` 必须包含 ZCode 客户端系统提示词的特征片段**，否则一律返回：

```
405 {"code":3012,"msg":"request has been blocked due to unusual activity."}
```

实测二分结论：

| system 内容 | 结果 |
|---|---|
| ZCode 原始 3 块 + 客户端自己的 system | ✅ 通过 |
| 只保留 block0 + block1 前 1500 字符 | ✅ 通过 |
| 块顺序/内容被替换（哪怕 93KB 合成 body） | ❌ 405 |

所以网关做的是：把这两块签名**插到 `system` 最前面**，客户端原有的 system **追加在后面**（不丢弃）。

签名数据在 `app/zcode_signature.json`：

- `block0`：`You are ZCode, an interactive coding agent`（精确 42 字符，必须原样）
- `block1_head`：ZCode 代理提示词前 1500 字符

**重要**：签名之外的字段不吃 WAF —— `tools`、`messages` 换成任意内容都能过（已实测）。所以换客户端/换对话内容不影响可用性。

### 指纹头

除签名外，平台还按 ZCode 客户端指纹识别。网关统一发这套头（见 `app/settings.py` 的 `ZCODE_FINGERPRINT`）：

```
anthropic-version: 2023-06-01
anthropic-beta: mid-conversation-system-2026-04-07
http-referer: https://zcode.z.ai
user-agent: ZCode/3.14.4 ai-sdk/provider-utils/4.0.27 runtime/node.js/24
x-client-language / x-client-timezone / x-os-category / x-os-version
x-platform / x-release-channel / x-title
x-zcode-agent / x-zcode-app-version / x-zcode-session-type
x-query-id / x-request-id / x-session-id / x-zcode-trace-id   （每请求随机 UUID）
```

Start Plan 模式下**不透传**客户端自己的这些头，避免被改写而触发 WAF。

### metadata

平台要求 `metadata.user_id` 是含设备与会话的 JSON 字符串：

```json
{"user_id": "{\"device_id\":\"<uuid>\",\"account_uuid\":\"\",\"session_id\":\"<uuid>\"}"}
```

网关每次启动生成一次 device_id / session_id 并固定使用（模拟稳定的客户端设备）。

### 账号状态机

| 状态 | 参与轮询 | 触发 | 恢复 |
|------|:---:|------|------|
| `active` | ✅ | 默认 | — |
| `cooling` | ⏳ 到期后 | 上游 429 / 连接失败 | `cooling_until` 到点（默认 300 秒） |
| `exhausted` | ❌ | 402 / 错误体含额度关键字 | 后台刷新检测到恢复 |
| `invalid` | ❌ | 401 / 403 | 后台改凭证 |
| `disabled` | ❌ | 手动禁用 | 手动启用 |

状态同步落库到 `data/accounts.db`（SQLite，WAL），重启后保留。

### 上游限流（重要）

ZCode 平台网关**按请求频率限流**。短时间连续请求会触发 `429`，账号进入 300 秒冷却，期间网关返回 `503 no_available_account`。

经验值：**间隔 ≥30 秒**比较安全。网关本身**没有**加限速器（按需求），调用方请自行控制频率；一旦被 429，等冷却结束或后台把账号改回 `active`。

### 额度耗尽即终止进程（重要）

按需求，上游返回**明确的额度耗尽**信号时，网关**不再尝试其它账号、不再发任何请求**，直接把整个反代进程退出：

- 触发条件：HTTP `402`，或响应体命中 `余额不足` / `无可用资源包` / `额度已用完` / `insufficient balance` / `no resource package` / `quota exhausted` / `insufficient quota`
- 刻意**不**触发：阿里云 WAF 拦截（`unusual activity`）、纯频率限流（`rate limit`）—— 这两类只走正常冷却流程
- 行为：客户端先收到 `503 insufficient_quota`，约 1 秒后进程退出
- **退出码为 `0`（正常退出）**。这是为了配合 Docker：`restart: on-failure` 下即"额度耗尽停住不重启、真崩溃才重启"。若改成非 0，容器会无限重启、继续打上游
- 重启方式：双击 `launcher\ZCode中转服务.bat` 选 `[1]`、`python main.py serve`，或 `docker compose ... up -d`

判断逻辑见 `app/routes/gateway.py` 的 `_is_quota_fatal()` / `_fatal_exit()`。

---

## 账号过期 / 凭证失效怎么更新

`start-plan` 用的 `zcodejwttoken` 由 ZCode 桌面端维护，会过期或轮换。症状：网关开始返回 **401**，或账号在后台被标成 `invalid`，最终表现为 `503 no_available_account`。

### 第 1 步：重新取出 JWT（在有桌面端的机器上跑）

```bash
node launcher/extract-zcode-jwt.cjs
#  -> 生成 zcode-jwt.txt（内容等同密码）
```

脚本按 `aes-256-gcm` 从 `~/.zcode/v2/credentials.json` 解出 `zcodejwttoken`。**别把 JWT 直接贴进命令行或聊天记录**——放文件里让程序读，用完删掉 `zcode-jwt.txt`（已在 `.gitignore` 里）。

### 第 2 步：换进网关

| 场景 | 做法 |
|------|------|
| **本机 / 桌面启动器** | 打开后台 `http://127.0.0.1:<端口>/admin/login`（默认密码 `zcode`），删掉旧账号、用新 JWT 新建 |
| **Docker / NAS** | 改 `.env` 的 `ZCODE_SEED_ACCOUNTS="bigmodel:<新JWT>"`，先删旧账号再重建容器 |

Docker 那条要特别注意：**`.env` 只在首次启动注入**，已入库的账号不会被覆盖。所以顺序是「先删旧账号 → 再 `up --force-recreate`」：

```bash
docker exec zcode2api python main.py remove-account bigmodel start-plan
docker compose -f docker-compose.1panel.yaml up -d --force-recreate
```

> 其实旧账号留着也不影响：`invalid` 状态的账号不会被轮询选中。

### 第 3 步：验证

```bash
curl -X POST http://127.0.0.1:<端口>/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"GLM-5.3-Flash","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

拿到 `200` 且响应含 `content` 即可。

---

## 本次改动清单

相对上游 `925e929` 的改动，按主题分三块。

### 1. 代码：新增 Start Plan 反代模式（原 Coding Plan 路径保持原样）

| 文件 | 改动 |
|------|------|
| `app/settings.py` | 新增 `ZCODE_PLAN_MODE`（默认 `start-plan`）、`UPSTREAM["bigmodel_start_plan"]`、`start_plan_signature()`、`ZCODE_FINGERPRINT`；`UPSTREAM` 的 zai/bigmodel **保持上游原值** |
| `app/zcode_signature.json` | **新增**：平台放行签名数据（block0 + block1 前 1500 字符） |
| `app/agent.py` | 新增 start-plan 分支（完整指纹头、不透传客户端头）；zai 的 `Authorization: Bearer` **保持原逻辑** |
| `app/routes/gateway.py` | 新增 start-plan 分支（注入签名、规范化 metadata、只选 start-plan 账号、额度耗尽即终止进程）；模型清单随模式切换，`_detect_provider` 保持原逻辑 |
| `app/models.py` | bigmodel 的三段点分 JWT 识别为 `start-plan` 模式 |

### 2. PC 本地部署 + 调用说明

| 文件 | 改动 |
|------|------|
| `launcher/ZCode中转服务.bat` | **新增**：桌面启动器（启动/停止/测连通/账号状态/改端口） |
| `launcher/extract-zcode-jwt.cjs` | **新增**：从桌面端 `credentials.json` 解出 JWT（账号过期时用） |
| `README.local.md` | **新增**：本文档 |
| `.gitignore` | 忽略 `port.txt`、`zcode-jwt.txt` |

### 3. NAS / Docker 部署

| 文件 | 改动 |
|------|------|
| `docker-compose.yml` | 注释切换远程镜像 / 本地构建；加 `ZCODE_PLAN_MODE`、`restart: on-failure` |
| `docker-compose.1panel.yaml` | **新增**：1Panel / fnOS 变体（12158:3000、外部 `1panel-network`） |
| `.env.1panel.example` | **新增** |
| `.env.example` | 补充 `ZCODE_PLAN_MODE` 说明 |
| `Dockerfile` | 构建时校验放行签名文件存在；更新注释 |
| `.dockerignore` | 排除 `launcher/` |
| `app/main.py` | 新增 `_seed_accounts()`：从 `ZCODE_SEED_ACCOUNTS` 注入账号，容器免交互登录 |
| `app/logs.py` | 支持 `NO_COLOR`，便于 `docker logs` 采集 |

> 账号库 `data/accounts.db` 是运行时数据，**不进仓库**。首次部署用 `.env` 的 `ZCODE_SEED_ACCOUNTS` 注入，或直接 scp 过去。
>
> `coding-plan` 模式的代码（`UPSTREAM`、`agent.py` 的 zai 分支、`_detect_provider`、`captcha.py`）全部保持上游原样，本次未改动。

## 已知限制

- **模型可能自称 ZCode**。签名块（ZCode 的身份与行为提示词）必须进 system 才能过 WAF，模型因此可能把自己当成 ZCode。客户端自己的 system 追加在后，通常能覆盖；但对身份敏感的场景要注意。
- **无 OpenAI 协议**，只有 `/v1/messages`（Anthropic）。
- **Trust Build 无法显式指定**。截图里的 "ZCode Trust Build" 与 "ZCode Start Plan" 是平台侧同一凭证下的两个配额桶，客户端（源码里零相关字符串）无法选择，由服务端决定扣哪个。网关能做的是只走 Start Plan、不回退。
- **凭证会轮换**。`zcodejwttoken` 由桌面端维护；若上游开始返回 401，需重新从 `credentials.json` 解密导入。
- **限流**。见上文「上游限流」。
- **仅本机可用**。服务监听 `0.0.0.0`，但网关 Key 未设置，暴露到局域网等于无鉴权。要给别的机器用，先在后台配上网关 Key。
- **`coding-plan` 模式当前跑不通**。Coding Plan 的 API Key 余额已耗尽（上游 429），且该模式需要阿里云无痕验证。保留它是为了不动上游代码；实际请用 `start-plan`。
- **`captcha_node/` 只服务 `coding-plan` 模式**，`start-plan` 下不会执行。
