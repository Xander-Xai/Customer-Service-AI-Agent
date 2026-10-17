"""Issue #13：非 PostgreSQL 方言的 claim 接管必须 fail-closed。

缺陷
----
``SideEffectStore.claim`` 的 PostgreSQL 分支用 ``SELECT ... FOR UPDATE`` 串行化
竞争者。SQLite 分支的接管走「普通 SELECT（无锁）+ 按主键 ORM UPDATE」，而
SQLAlchemy 的 SQLite 方言**忽略** ``FOR UPDATE``。两个认领者可同时读到同一条过期
行、同时判定可接管、同时拿到 ``CLAIM_EXECUTE`` -> 同一笔写执行两次。

修复
----
无行级锁的方言上，接管路径一律 fail-closed 返回 ``in_progress``（调用方按可重试的
``TransientError`` 处理，最终进 DLQ 而不会重复执行）。首次插入仍安全：其互斥由
数据库唯一约束提供，与方言无关。

另加启动期硬约束：生产 ``DATABASE_URL`` 必须是 PostgreSQL（见
``core.config.validate_app_database_dsn``），堵住「PG checkpoint + SQLite 应用库」
这条骗过 checkpoint 校验的配置。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def sqlite_store(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime.side_effects import SideEffectStore

    engine = create_engine(
        f"sqlite:///{tmp_path / 'claim.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return SideEffectStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))


def test_fresh_insert_claim_still_executes_on_sqlite(sqlite_store):
    """首次插入的互斥由唯一约束保证，与方言无关 -> 仍可执行。"""
    from runtime.side_effects import CLAIM_EXECUTE

    claim = sqlite_store.claim(
        tool_name="staging_refund",
        operation_key="op-fresh",
        run_id="run-1",
        thread_id="T-1",
        fingerprint="fp",
        claim_ttl_seconds=60.0,
        owner="owner-A",
    )
    assert claim.state == CLAIM_EXECUTE


def test_expired_lease_takeover_is_refused_on_sqlite(sqlite_store):
    """租约过期的接管在无行级锁方言上必须 fail-closed，不得交出 ``execute``。

    修复前：第二次 claim 返回 ``CLAIM_EXECUTE``（两个执行者都可能重复执行）。
    修复后：返回 ``CLAIM_IN_PROGRESS``。
    """
    from runtime.side_effects import CLAIM_EXECUTE, CLAIM_IN_PROGRESS

    first = sqlite_store.claim(
        tool_name="staging_refund",
        operation_key="op-takeover",
        run_id="run-1",
        thread_id="T-1",
        fingerprint="fp",
        claim_ttl_seconds=-1.0,  # 立即可接管
        owner="owner-A",
    )
    assert first.state == CLAIM_EXECUTE

    second = sqlite_store.claim(
        tool_name="staging_refund",
        operation_key="op-takeover",
        run_id="run-2",
        thread_id="T-1",
        fingerprint="fp",
        claim_ttl_seconds=60.0,
        owner="owner-B",
    )
    assert (
        second.state == CLAIM_IN_PROGRESS
    ), "无行级锁的方言不得交出接管执行权；否则并发认领会重复执行同一笔写"


def test_failed_row_retry_is_refused_on_sqlite(sqlite_store):
    """FAILED 行的重试同样属于接管，必须 fail-closed。"""
    from runtime.side_effects import CLAIM_EXECUTE, CLAIM_IN_PROGRESS

    first = sqlite_store.claim(
        tool_name="staging_refund",
        operation_key="op-failed",
        run_id="run-1",
        thread_id="T-1",
        fingerprint="fp",
        claim_ttl_seconds=60.0,
        owner="owner-A",
    )
    assert first.state == CLAIM_EXECUTE
    assert sqlite_store.mark_failed(
        "staging_refund", "op-failed", error_type="X", error_message="boom", owner="owner-A"
    )

    retry = sqlite_store.claim(
        tool_name="staging_refund",
        operation_key="op-failed",
        run_id="run-2",
        thread_id="T-1",
        fingerprint="fp",
        claim_ttl_seconds=60.0,
        owner="owner-B",
    )
    assert retry.state == CLAIM_IN_PROGRESS


class TestAppDatabaseDsnGuard:
    """生产应用库必须是 PostgreSQL（Issue #13 的启动期防线）。"""

    def test_dev_mode_allows_sqlite(self):
        from core.config import validate_app_database_dsn

        assert validate_app_database_dsn(dev_mode=True, database_url="sqlite:///x.db") == []

    def test_production_empty_database_url_is_rejected(self):
        from core.config import validate_app_database_dsn

        problems = validate_app_database_dsn(dev_mode=False, database_url="")
        assert any("DATABASE_URL" in p for p in problems)

    def test_production_sqlite_app_db_is_rejected(self):
        """PG checkpoint + SQLite 应用库必须被拒绝，而不是带病启动。"""
        from core.config import validate_app_database_dsn

        problems = validate_app_database_dsn(dev_mode=False, database_url="sqlite:///data/csai.db")
        assert any("PostgreSQL" in p for p in problems)
        assert any("Issue #13" in p for p in problems)

    def test_production_sqlalchemy_driver_dsn_is_rejected(self):
        """``postgresql+psycopg2://`` 会被 db 层误判为 SQLite 回退，必须拒绝。"""
        from core.config import validate_app_database_dsn

        problems = validate_app_database_dsn(
            dev_mode=False, database_url="postgresql+psycopg2://u:p@h/db"
        )
        assert problems

    def test_production_postgres_dsn_passes(self):
        from core.config import validate_app_database_dsn

        assert (
            validate_app_database_dsn(dev_mode=False, database_url="postgresql://u:p@h:5432/db")
            == []
        )
        assert validate_app_database_dsn(dev_mode=False, database_url="postgres://u:p@h/db") == []
