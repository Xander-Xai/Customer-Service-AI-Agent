"""Regression tests for loop-partitioned ApiEmbedding async client reuse.

httpx.AsyncClient binds its connection pool to the event loop that created it.
A module-level singleton reused across short-lived loops (e.g. repeated
asyncio.run() inside worker threads, as scripts/import_eval_corpus.py does via
asyncio.to_thread) raised RuntimeError: Event loop is closed on every call
after the first, which blocked the 5000-doc eval corpus import.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from rag import api_embedding
from rag.api_embedding import ApiEmbedding


@pytest.fixture(autouse=True)
def _reset_singleton():
    api_embedding._async_client = None
    api_embedding._async_client_loop = None
    yield
    api_embedding._async_client = None
    api_embedding._async_client_loop = None


def _client() -> ApiEmbedding:
    return ApiEmbedding(
        api_key="test-key", model="test-model", base_url="https://example.invalid/v1"
    )


def _stub_post(payload: dict) -> AsyncMock:
    post = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {
                    "data": [
                        {"index": i, "embedding": [0.1, 0.2]} for i in range(len(payload["input"]))
                    ]
                },
            },
        )()
    )
    return post


def test_singleton_reused_within_same_loop():
    async def run():
        return await api_embedding._get_async_client(), await api_embedding._get_async_client()

    first, second = asyncio.run(run())
    assert first is second


def test_client_recreated_when_loop_changes():
    async def grab():
        return await api_embedding._get_async_client()

    first = asyncio.run(grab())
    assert api_embedding._async_client_loop is not None
    second = asyncio.run(grab())

    assert second is not first
    assert api_embedding._async_client is second


def test_encode_survives_repeated_asyncio_run_in_worker_threads():
    api = _client()
    payload = {"model": "test-model", "input": ["a", "b"], "encoding_format": "float"}
    post = _stub_post(payload)

    def work():
        return asyncio.run(api.aencode(["a", "b"]))

    with (
        patch.object(api_embedding, "_get_async_client", wraps=api_embedding._get_async_client),
        patch.object(api_embedding.httpx.AsyncClient, "post", post),
    ):
        for _ in range(3):
            result = asyncio.run(asyncio.to_thread(work))
            assert result.shape == (2, 2)


def test_close_resets_loop_partition():
    async def grab():
        return await api_embedding._get_async_client()

    client = asyncio.run(grab())
    asyncio.run(api_embedding.close_async_client())
    assert api_embedding._async_client is None
    assert api_embedding._async_client_loop is None
    assert client.is_closed


def test_aencode_returns_vectors_sorted_by_index():
    api = _client()
    post = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {
                    "data": [
                        {"index": 1, "embedding": [0.3, 0.4]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ]
                },
            },
        )()
    )
    with (
        patch.object(api_embedding, "_get_async_client", wraps=api_embedding._get_async_client),
        patch.object(api_embedding.httpx.AsyncClient, "post", post),
    ):
        result = asyncio.run(api.aencode(["x", "y"]))
    assert np.allclose(result, np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32))
