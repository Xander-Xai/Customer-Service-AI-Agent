"""
统一日志模块（v3.9 — 生产就绪版）
- 文件日志轮转（10MB/文件，保留 5 份）
- JSON 结构化格式（生产环境可选）
- gzip 压缩历史日志
- 同时输出到 stderr + 文件
"""
import gzip
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

from config import LOG_CONFIG


class _GzipRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler + gzip 压缩历史日志"""

    def rotate(self, source, dest):
        """压缩已轮转的日志文件"""
        super().rotate(source, dest)
        if os.path.exists(dest) and not dest.endswith(".gz"):
            try:
                with open(dest, "rb") as f_in:
                    compressed = gzip.compress(f_in.read())
                with open(dest + ".gz", "wb") as f_out:
                    f_out.write(compressed)
                os.remove(dest)
            except OSError:
                pass


class _JSONFormatter(logging.Formatter):
    """JSON 结构化日志格式器（生产环境）"""

    def format(self, record):
        log_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[0]:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


# 日志目录
_LOG_DIR = os.getenv("LOG_DIR", "logs")


def _setup_file_handler(logger_instance: logging.Logger, level: int):
    """配置文件日志轮转（静默失败：目录不可写时仅 stderr）"""
    try:
        Path(_LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, "app.log")

        file_handler = _GzipRotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)

        # 生产环境使用 JSON 格式，开发环境使用文本格式
        use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"
        file_handler.setFormatter(
            _JSONFormatter() if use_json else logging.Formatter(
                LOG_CONFIG.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
        )

        logger_instance.addHandler(file_handler)
    except OSError as e:
        # 目录不可写时静默降级，仅输出到 stderr
        logger_instance.warning(f"无法创建日志文件，降级为仅 stderr 输出: {e}")


def get_logger(name: str) -> logging.Logger:
    """
    获取指定模块的 logger（v3.9: 生产就绪）
    - stderr 输出（开发友好）
    - 文件轮转 + gzip 压缩（生产持久化）
    - JSON 结构化（可选，LOG_FORMAT=json 启用）
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = getattr(logging, LOG_CONFIG.get("level", "INFO").upper(), logging.INFO)
        logger.setLevel(level)

        # 1) stderr 输出（始终启用）
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(
            logging.Formatter(
                LOG_CONFIG.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
        )
        logger.addHandler(stderr_handler)

        # 2) 文件轮转 + gzip（生产环境）
        _setup_file_handler(logger, level)

        logger.propagate = False
    return logger
