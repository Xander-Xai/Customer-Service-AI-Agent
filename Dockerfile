FROM python:3.10-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码（仅必要文件）
COPY . .

# v3.4: 创建非 root 用户
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

# 暴露端口
EXPOSE 8000

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

# 启动 FastAPI 服务（v3.7: 支持 TLS 配置）
# 设置 TLS_CERT_FILE 和 TLS_KEY_FILE 环境变量启用 HTTPS
CMD if [ -n "$TLS_CERT_FILE" ] && [ -n "$TLS_KEY_FILE" ]; then \
      python -m uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --ssl-certfile "$TLS_CERT_FILE" --ssl-keyfile "$TLS_KEY_FILE"; \
    else \
      python -m uvicorn api.app_factory:app --host 0.0.0.0 --port 8000; \
    fi
