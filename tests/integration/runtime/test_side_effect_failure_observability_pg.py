"""F5：``mark_failed`` 的 CAS 拒绝不得被静默吞掉（真实 PostgreSQL）。

``execute_idempotent_operation`` 在工具抛异常时调用 ``store.mark_failed``，但过去
忽略返回值。三种真实拒绝场景（owner 被接管 / 操作已 SUCCEEDED / 租约已过期）下它
返回 ``False``，ledger 仍停在 ``PENDING`` + 旧 owner，而外部调用结果未知——日志里
却只有工具异常，运维看不到「FAILED 记录没写进去」。本文件用真实 PG 钉住：原始异常
照常上抛、拒绝可观测（结构化 error 日志 + 指标）、且不会覆盖既有终态。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from runtime.side_effects import SideEffectStore, execute_idempotent_operation

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)

pytestmark = requires_infra

_FRESH_TTL = 60.0


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def store(pg_engine):
    return SideEffectStore(session_factory=_session_factory(pg_engine))


@pytest.fixture
def ledger_keys(pg_engine):
    keys: list[str] = []

    def _new() -> tuple[str, str]:
        key = f"f5-{uuid.uuid4().hex[:12]}"
        keys.append(key)
        return "staging_refund", key

    yield _new
    if keys:
        with pg_engine.begin() as conn:
            for key in keys:
                conn.execute(
                    text("DELETE FROM tool_side_effects WHERE operation_key = :k"), {"k": key}
                )


def _read(pg_engine, key: str) -> dict:
    with pg_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, claim_owner, result_reference, error_type, "
                "claim_expires_at FROM tool_side_effects WHERE operation_key = :k"
            ),
            {"k": key},
        ).one()
    return {
        "status": row[0],
        "claim_owner": row[1],
        "result_reference": row[2],
        "error_type": row[3],
        "claim_expires_at": row[4],
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _patch_row(pg_engine, key: str, *, status: str, owner: str, lease_seconds: float) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tool_side_effects SET status = :s, claim_owner = :o, "
                "claim_expires_at = :exp, updated_at = :now WHERE operation_key = :k"
            ),
            {
                "s": status,
                "o": owner,
                "exp": _now() + timedelta(seconds=lease_seconds),
                "now": _now(),
                "k": key,
            },
        )


def _run_expect_tool_error(store, tool, key, operation) -> None:
    async def _scenario():
        await execute_idempotent_operation(
            tool_name=tool,
            operation_key=key,
            run_id="run-f5",
            thread_id="T-f5",
            arguments={"amount": 100},
            operation=operation,
            store=store,
            claim_ttl_seconds=_FRESH_TTL,
        )

    with pytest.raises(RuntimeError, match="tool exploded"):
        asyncio.run(_scenario())


def test_owner_lost_failure_record_is_not_silently_dropped(
    pg_engine, store, ledger_keys, monkeypatch
):
    import runtime.side_effects as se
    from runtime import metrics as run_metrics

    rejected: list[int] = []
    logged: list[str] = []
    monkeypatch.setattr(
        run_metrics, "record_side_effect_terminal_write_rejected", lambda: rejected.append(1)
    )
    monkeypatch.setattr(
        se.logger,
        "error",
        lambda msg, *args, **kwargs: logged.append(msg % args if args else msg),
    )

    tool, key = ledger_keys()

    def _operation():
        # 认领被 B 接管（真实 PG 行状态变化），随后工具失败。
        _patch_row(pg_engine, key, status="PENDING", owner="owner-B", lease_seconds=60.0)
        raise RuntimeError("tool exploded")

    _run_expect_tool_error(store, tool, key, _operation)

    row = _read(pg_engine, key)
    assert row["status"] == "PENDING"
    assert row["claim_owner"] == "owner-B", "不得覆盖接管者的认领"
    assert rejected == [1], "CAS 拒绝必须计入可观测指标"
    assert any("FAILED" in message and "CAS" in message for message in logged), logged


def test_already_succeeded_operation_is_not_rewritten_by_late_failure(
    pg_engine, store, ledger_keys, monkeypatch
):
    from runtime import metrics as run_metrics

    rejected: list[int] = []
    monkeypatch.setattr(
        run_metrics, "record_side_effect_terminal_write_rejected", lambda: rejected.append(1)
    )

    tool, key = ledger_keys()

    def _operation():
        _patch_row(pg_engine, key, status="SUCCEEDED", owner="owner-B", lease_seconds=60.0)
        raise RuntimeError("tool exploded")

    _run_expect_tool_error(store, tool, key, _operation)

    row = _read(pg_engine, key)
    assert row["status"] == "SUCCEEDED", "迟到的失败回调不得改写已成功的操作"
    assert rejected == [1]


def test_expired_lease_failure_record_is_rejected_and_observable(
    pg_engine, store, ledger_keys, monkeypatch
):
    from runtime import metrics as run_metrics

    rejected: list[int] = []
    monkeypatch.setattr(
        run_metrics, "record_side_effect_terminal_write_rejected", lambda: rejected.append(1)
    )

    tool, key = ledger_keys()

    def _operation():
        # owner 仍是自己，但租约已过期：终态提交必须拒绝（过期提交不能复活执行权）。
        current_owner = _read(pg_engine, key)["claim_owner"]
        _patch_row(pg_engine, key, status="PENDING", owner=current_owner, lease_seconds=-1.0)
        raise RuntimeError("tool exploded")

    _run_expect_tool_error(store, tool, key, _operation)

    row = _read(pg_engine, key)
    assert row["status"] == "PENDING", "过期租约不得提交 FAILED"
    assert rejected == [1]
