FROM python:3.10-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 暴露端口
# 8000: FastAPI WebSocket 服务（主服务）
EXPOSE 8000

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/api/health'); assert r.status_code==200"

# 默认启动 FastAPI 服务（v3.0）
CMD ["python", "-m", "uvicorn", "api.app_factory:app", "--host", "0.0.0.0", "--port", "8000"]
