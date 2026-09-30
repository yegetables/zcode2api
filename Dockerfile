# zcode2api — 纯 Python 网关
#
# 不含 Node.js：阿里云无痕验证求解器（captcha_node/）已随旧端点废弃。
# 当前走 Start Plan：zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages，
# 不校验 verifyParam；平台 WAF 依赖 body 的 system 放行签名 + 客户端指纹头，
# 两者都由网关处理（签名数据在 app/zcode_signature.json，随镜像一起打进）。
# 镜像约 180MB，多架构（x86_64 / arm64）原生可用，无需模拟。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NO_COLOR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_PORT=3000 \
    ZCODE_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# 放行签名数据必须存在，否则平台网关会以 405 unusual activity 拒绝所有请求
RUN test -f app/zcode_signature.json || (echo "缺少 app/zcode_signature.json" >&2; exit 1)

# 账号 / 设置持久化目录（务必挂载到宿主机卷，否则每次重建容器都丢账号）
VOLUME ["/data"]
EXPOSE 3000

# 无交互：前台运行，日志走 stdout/stderr 交给 docker logs 采集
CMD ["python", "main.py", "serve"]
