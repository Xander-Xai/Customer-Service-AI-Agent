# ===== 多阶段构建：v4.1 =====

# --- Stage 1: Builder ---
FROM python:3.10-slim AS builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（利用 Docker 层缓存）
# requirements.txt 是当前唯一权威的部署依赖契约（含安全 floor）。
# requirements-lock.txt 是 2026-06-06 的本地快照，已明确为非权威，刻意不在此使用。
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: Runtime ---
FROM python:3.10-slim AS runtime

# 构建参数：dev / prod
ARG APP_MODE=prod
ENV APP_MODE=${APP_MODE}

WORKDIR /app

# 从 builder 复制已安装的依赖
COPY --from=builder /install /usr/local

# 安装运行时系统依赖（最小化）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# 复制源码（仅必要文件，.dockerignore 已排除非必要文件）
COPY . .

# 创建非 root 用户 + 数据目录
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /app/logs /app/data && \
    chown -R appuser:appuser /app/logs /app/data

USER appuser

# 暴露端口
EXPOSE 8000

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV LOG_DIR=/app/logs

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# 启动命令
CMD if [ "$APP_MODE" = "dev" ]; then \
      echo "🔧 开发模式: uvicorn --reload" && \
      uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app --log-level debug; \
    else \
      echo "🚀 生产模式: gunicorn" && \
      exec gunicorn api.app_factory:app -c gunicorn.conf.py; \
    fi
