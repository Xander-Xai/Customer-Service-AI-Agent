"""
统一日志模块（v3.0）
替换散落的 print 为结构化 logging
格式: [timestamp][level][module] message
"""
import logging
from config import LOG_CONFIG


def get_logger(name: str) -> logging.Logger:
    """获取指定模块的 logger"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = getattr(logging, LOG_CONFIG.get("level", "INFO").upper(), logging.INFO)
        logger.setLevel(level)
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter(LOG_CONFIG.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
    return logger
