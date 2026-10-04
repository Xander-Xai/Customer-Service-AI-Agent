"""Alembic 环境配置（v4.1）

从 db.database 导入 engine，从 db.models 导入 Base，
支持从 DATABASE_URL 环境变量读取数据库连接。
"""

import os
import re
import sys
from logging.config import fileConfig

from sqlalchemy import create_engine, engine_from_config, pool  # noqa: F401

from alembic import context

# 将项目根目录加入 sys.path，确保能导入 db 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import engine as _default_engine
from db.models import Base

# Alembic Config object
config = context.config

# 日志配置
#
# `disable_existing_loggers` 必须显式传 False：`fileConfig()` 的该参数默认值是
# True（本项目 Python 3.10；注意它只认函数参数，**不读** alembic.ini 里的同名
# 键，那是 `dictConfig` 的行为），会把此刻已存在、但未被 alembic.ini 声明的
# logger 全部置为 `disabled = True`。
#
# 本项目在迁移之前就已经通过 `core.logger.get_logger()` 装配好带 stderr +
# 轮转文件 + JSON 格式的 logger —— `api/app_factory.py` 里 "app_factory"
# （第 23 行）就早于紧随其后的 `init_db()`（第 28 行）。一旦被禁用，这些
# logger 的调用会在 `isEnabledFor()` 处直接返回，连 stderr 都不会写，
# "启动失败必须留下可行动错误日志"的契约随即失效。
#
# alembic 只应配置自己的 logger（root/sqlalchemy/alembic），无权关掉应用的。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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

    The target is ``_resolve_database_url()``, i.e. ``DATABASE_URL`` when set.
    Previously this ignored the resolved URL and always used the module-level
    ``db.database.engine``. That made ``alembic upgrade head`` impossible to
    point at PostgreSQL: the URL contract was documented (and migrations carry
    dialect-specific logic such as TIMESTAMPTZ) yet silently ignored, so
    migrations could be "verified" against SQLite while production runs
    PostgreSQL.

    When no URL is configured we reuse the shared engine so the app and the
    migration still see the same connection pool and event listeners.
    """
    url = _resolve_database_url()
    default_url = str(_default_engine.url)
    if url and url != default_url:
        own_engine = create_engine(url, poolclass=pool.NullPool)
        try:
            with own_engine.connect() as connection:
                _run_migrations_on_connection(connection)
        finally:
            own_engine.dispose()
        return

    with _default_engine.connect() as connection:
        _run_migrations_on_connection(connection)


def _run_migrations_on_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _candidate_url(key: str) -> str:
    """Read a URL env var, rejecting values that are not usable URLs.

    ``.env.example`` ships ``DATABASE_URL=   # 为空使用 SQLite（开发环境）``.
    python-dotenv keeps that inline comment as the **value**, and
    ``core/config.py`` runs ``load_dotenv(override=True)``, so a copied
    ``.env`` makes ``os.environ["DATABASE_URL"]`` literally
    ``'# 为空使用 SQLite（开发环境）'``. Passing that to ``create_engine`` raises
    ``ArgumentError: Could not parse SQLAlchemy URL``. Treat anything without a
    ``scheme://`` prefix as unset so the default engine is used instead.

    (The previous code never hit this only because it bypassed URL resolution
    entirely — the failure was latent, not absent.)
    """
    value = (os.getenv(key) or "").strip().strip("\"'")
    if not value or not _URL_SCHEME_RE.match(value):
        return ""
    return value


def _resolve_database_url() -> str:
    """从环境变量或 engine 解析数据库连接 URL。

    优先级：

    1. ``ALEMBIC_DATABASE_URL`` —— 迁移工具专用的显式覆盖。
    2. ``DATABASE_URL``。
    3. ``db.database.engine`` 的实际 URL（兜底，保持与 app 同一连接）。

    为什么需要 (1)：``core/config.py`` 在 import 时执行
    ``load_dotenv(override=True)``，**仓库内的 ``.env`` 会覆盖进程环境里的同名
    变量**。开发者本地 ``.env`` 里通常写的是空值或注释，于是
    ``DATABASE_URL=postgresql://... alembic upgrade head`` 会被静默改写成
    SQLite —— 这正是「只用 SQLite 证明了 TIMESTAMPTZ / 并发正确性」而生产跑
    PostgreSQL 的成因。``ALEMBIC_DATABASE_URL`` 不在任何 ``.env`` 模板里声明，
    因此不会被 dotenv 覆盖，可以确定性地把迁移指向真实 PostgreSQL。
    """
    for key in ("ALEMBIC_DATABASE_URL", "DATABASE_URL"):
        url = _candidate_url(key)
        if url:
            return url
    # 从默认 engine 获取 URL
    return str(_default_engine.url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
