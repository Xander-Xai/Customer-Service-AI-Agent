"""
Gunicorn 生产配置（v3.9）
用法: gunicorn api.app_factory:app -c gunicorn.conf.py
"""

import multiprocessing
import os

# ===== 服务器套接字 =====
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
backlog = 2048

# ===== Worker 进程 =====
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 120  # 通用超时（含 WebSocket 首次握手）
graceful_timeout = 30  # 优雅关闭等待时间
keepalive = 5  # keep-alive 超时

# ===== 日志 =====
accesslog = "-"  # stdout
errorlog = "-"  # stderr
loglevel = os.getenv("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ===== 进程管理 =====
preload_app = True  # 预加载应用（共享内存，减少 fork 开销）
max_requests = 1000  # Worker 处理 N 个请求后重启（防内存泄漏）
max_requests_jitter = 50  # 随机抖动，避免同时重启

def post_fork(server, worker):
    """
    Worker 进程 fork 后执行。
    因为 preload_app=True，主进程可能已经建立了数据库连接池，
    fork 后的子进程共享这些 socket 会导致 SSL SYSCALL error 或 EOF detected。
    这里需要强制清除连接池。
    """
    try:
        from db.database import engine
        engine.dispose()
        server.log.info("Worker fork: SQLAlchemy engine disposed to prevent connection sharing")
    except ImportError:
        pass
