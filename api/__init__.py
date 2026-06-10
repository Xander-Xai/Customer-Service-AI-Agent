"""FastAPI 异步服务层"""

from . import app_factory
from .app import create_app

__all__ = ["create_app", "app_factory"]
