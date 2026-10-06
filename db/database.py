"""
数据库连接管理（v4.1 — SQLite + PostgreSQL 双数据库支持）
支持：
- SQLAlchemy 引擎和 Session 工厂
- 数据库初始化（创建表）
- 会话上下文管理
- 自动检测 PostgreSQL / SQLite
"""

import os
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.logger import get_logger

from .models import Base

logger = get_logger("db")

# ===== 数据库 URL 解析 =====
# 优先使用 config.DATABASE_URL（环境变量 DATABASE_URL），为空则回退 SQLite
try:
    from core.config import (
        DATABASE_URL as _CFG_DATABASE_URL,
    )
    from core.config import (
        DEV_MODE as _DEV_MODE,
    )
    from core.config import (
        ConfigurationError as _ConfigurationError,
    )
except ImportError:
    class _ConfigurationError(RuntimeError):
        pass

    _CFG_DATABASE_URL = os.getenv("DATABASE_URL", "")
    _DEV_MODE = os.getenv("DEV_MODE", "").lower() == "true"

_DB_DIR = os.getenv("DB_DIR", "data")
_DB_PATH = os.getenv("DB_PATH", os.path.join(_DB_DIR, "csai.db"))

# 判断数据库类型
_is_postgresql = _CFG_DATABASE_URL.startswith(("postgresql://", "postgres://"))

if _is_postgresql:
    DATABASE_URL = _CFG_DATABASE_URL
    # 规范化 postgres:// → postgresql://（部分驱动不支持旧协议）
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    _DB_TYPE = "postgresql"
    logger.info("使用 PostgreSQL 数据库")
else:
    DATABASE_URL = f"sqlite:///{_DB_PATH}"
    _DB_TYPE = "sqlite"
    # 确保数据库目录存在（引擎创建前必须就绪）
    os.makedirs(_DB_DIR, exist_ok=True)
    logger.info(f"使用 SQLite 数据库: {_DB_PATH}")

# ===== 引擎创建 =====
if _is_postgresql:
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},  # SQLite 需要
        echo=False,
        pool_pre_ping=True,
        poolclass=StaticPool,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """初始化数据库：优先使用 alembic upgrade head，失败则回退到 create_all"""
    if not _is_postgresql:
        os.makedirs(_DB_DIR, exist_ok=True)
        # v4.1: SQLite WAL 模式提升并发写入性能
        try:
            from sqlalchemy import text

            with engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA synchronous=NORMAL"))
                conn.commit()
            logger.info("SQLite WAL 模式已启用")
        except Exception as e:
            logger.warning(f"SQLite WAL 模式启用失败: {e}")

    _alembic_ok = False
    try:
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        # 优先使用配置的 alembic.ini 路径
        try:
            from core.config import ALEMBIC_CONFIG_PATH as _alembic_cfg
        except ImportError:
            _alembic_cfg = os.getenv("ALEMBIC_CONFIG_PATH", "alembic.ini")

        alembic_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), _alembic_cfg)

        if os.path.exists(alembic_cfg_path):
            # 设置 DATABASE_URL 环境变量供 alembic 读取
            os.environ["DATABASE_URL"] = DATABASE_URL

            alembic_cfg = AlembicConfig(alembic_cfg_path)
            alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
            alembic_command.upgrade(alembic_cfg, "head")
            _alembic_ok = True
            logger.info(f"数据库迁移完成（alembic upgrade head）: {_DB_TYPE}")
    except Exception as e:
        if not _DEV_MODE:
            logger.error(f"生产模式数据库迁移失败，拒绝回退 create_all: {e}")
            raise _ConfigurationError(
                "数据库迁移失败：生产模式下不会回退到 create_all，请修复 Alembic 环境后重试"
            ) from e
        logger.warning(f"alembic 迁移失败，回退到 create_all: {e}")

    if not _alembic_ok:
        Base.metadata.create_all(bind=engine)
        logger.info(f"数据库初始化完成（create_all）: {_DB_TYPE}")


def get_db():
    """FastAPI 依赖注入：获取数据库会话（v5.1: 异常时自动 rollback）"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_session() -> Session:
    """手动获取数据库会话（非 FastAPI 上下文使用）"""
    return SessionLocal()


def get_database_info() -> dict:
    """返回当前数据库类型和连接信息（不暴露密码）"""
    parsed = urlparse(DATABASE_URL)
    info = {
        "db_type": _DB_TYPE,
        "driver": parsed.scheme,
        "host": parsed.hostname or "",
        "port": parsed.port or (5432 if _is_postgresql else None),
        "database": (parsed.path.lstrip("/") if _is_postgresql else _DB_PATH),
        "username": parsed.username or "",
        "pool_size": 10 if _is_postgresql else None,
        "max_overflow": 20 if _is_postgresql else None,
    }
    return info
