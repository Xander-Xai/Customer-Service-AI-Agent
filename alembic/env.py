"""Alembic 环境配置（v4.1）

从 db.database 导入 engine，从 db.models 导入 Base，
支持从 DATABASE_URL 环境变量读取数据库连接。
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool  # noqa: F401

from alembic import context

# 将项目根目录加入 sys.path，确保能导入 db 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import engine as _default_engine
from db.models import Base

# Alembic Config object
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标元数据（用于 --autogenerate）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well.
    """
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    connectable = _default_engine
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


def _resolve_database_url() -> str:
    """从环境变量或 engine 解析数据库连接 URL"""
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url
    # 从默认 engine 获取 URL
    return str(_default_engine.url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
