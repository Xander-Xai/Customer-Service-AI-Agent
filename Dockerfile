FROM python:3.10-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码（仅必要文件）
COPY . .

# v3.4: 创建非 root 用户
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app && \
    mkdir -p /app/logs && \
    chown -R appuser:appuser /app/logs
USER appuser

# 暴露端口
EXPOSE 8000

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV LOG_DIR=/app/logs

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

# v3.9: 使用 Gunicorn 多 Worker（生产就绪）
CMD ["gunicorn", "api.app_factory:app", "-c", "gunicorn.conf.py"]
