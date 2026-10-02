"""PR #26-G / #26-H: checkpoint schema-setup race + session-delete checkpoint purge.

Real PostgreSQL integration (gated on ``TEST_DISTRIBUTED_DB_URL``).

G. ``AsyncPostgresSaver.setup()`` used to run unbounded and concurrently from
   every Gunicorn worker. A fresh schema makes those migration passes race. With
   the advisory-lock guard, N concurrent initializers must all succeed.
H. ``DELETE /api/sessions/{id}`` must delete the matching checkpoint thread, so
   re-creating the same session id cannot merge the supposedly-deleted history.

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    pytest tests/integration/runtime/test_checkpoint_setup_and_session_delete.py -q
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not DB_URL, reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL"),
]


def _build_tiny_graph(checkpointer):
    from typing import TypedDict

    from langgraph.graph import StateGraph

    class S(TypedDict, total=False):
        value: int

    graph = StateGraph(S)
    graph.add_node("inc", lambda s: {"value": s.get("value", 0) + 1})
    graph.set_entry_point("inc")
    graph.set_finish_point("inc")
    return graph.compile(checkpointer=checkpointer)


async def _drop_checkpoint_tables(db_url: str) -> None:
    import psycopg

    async with (
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        for table in (
            "checkpoint_writes",
            "checkpoint_blobs",
            "checkpoints",
            "checkpoint_migrations",
        ):
            await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.mark.timeout(240)
def test_concurrent_schema_setup_all_succeed(pg_url):
    """6 concurrent initializers on a fresh schema must all succeed (no migration race)."""
    pytest.importorskip("psycopg")
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    async def scenario():
        await _drop_checkpoint_tables(pg_url)
        runtimes = await asyncio.gather(
            *[
                build_postgres_checkpointer(pg_url, min_size=1, max_size=2, setup_timeout=60.0)
                for _ in range(6)
            ]
        )
        try:
            assert len(runtimes) == 6
            # The schema really exists after the guarded setup.
            assert runtimes[0].pool is not None
        finally:
            for rt in runtimes:
                await close_checkpoint_runtime(rt)

    asyncio.run(scenario())


@pytest.mark.timeout(180)
def test_session_delete_purges_checkpoint_and_recreate_is_clean(pg_url):
    """Delete via the API helper -> checkpoint thread gone -> re-create starts fresh."""
    pytest.importorskip("psycopg")
    from api.routes.sessions import delete_session_checkpoint
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread_id = f"del-{uuid.uuid4().hex}"
    cfg = {"configurable": {"thread_id": thread_id}}

    async def scenario():
        rt = await build_postgres_checkpointer(pg_url, min_size=1, max_size=2, setup_timeout=30.0)
        try:
            graph = _build_tiny_graph(rt.checkpointer)
            result = await graph.ainvoke({"value": 41}, config=cfg)
            assert result["value"] == 42
            assert await rt.checkpointer.aget_tuple(cfg) is not None

            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(graph_app=SimpleNamespace(checkpointer=rt.checkpointer))
                )
            )
            assert await delete_session_checkpoint(request, thread_id) is True
            assert await rt.checkpointer.aget_tuple(cfg) is None

            # Re-using the same thread id must NOT merge the deleted history.
            state = await graph.aget_state(cfg)
            assert state.values == {}
            fresh = await graph.ainvoke({"value": 100}, config=cfg)
            assert fresh["value"] == 101
        finally:
            await close_checkpoint_runtime(rt)

    asyncio.run(scenario())
