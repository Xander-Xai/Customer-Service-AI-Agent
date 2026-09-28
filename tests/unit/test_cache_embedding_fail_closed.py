"""Focused regression tests for semantic-cache embedding fail-closed behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cache.response_cache import ResponseCache
from rag.embedding_status import EmbeddingDimensionError, EmbeddingUnavailableError


def _cache(embedding_model=None):
    qdrant = MagicMock()
    qdrant.get_collections.return_value = SimpleNamespace(collections=[])
    return ResponseCache(qdrant_client=qdrant, embedding_model=embedding_model), qdrant


def test_missing_provider_does_not_fabricate_vector():
    cache, _ = _cache()
    with pytest.raises(EmbeddingUnavailableError):
        cache._embed_query("query")


def test_provider_exception_is_unavailable():
    model = MagicMock()
    model.encode.side_effect = TimeoutError("provider timeout")
    cache, _ = _cache(model)
    with pytest.raises(EmbeddingUnavailableError):
        cache._embed_query("query")


@pytest.mark.parametrize("vector", [[], [float("nan")] * 1024, [float("inf")] * 1024, [None] * 1024])
def test_invalid_embedding_is_rejected(vector):
    model = MagicMock()
    model.encode.return_value = vector
    cache, _ = _cache(model)
    with pytest.raises(EmbeddingDimensionError):
        cache._embed_query("query")


def test_wrong_dimension_is_rejected():
    model = MagicMock()
    model.encode.return_value = [0.1]
    cache, _ = _cache(model)
    with pytest.raises(EmbeddingDimensionError):
        cache._embed_query("query")


def test_semantic_read_skips_qdrant_when_provider_unavailable():
    cache, qdrant = _cache()
    assert cache._qdrant_get("query", ["shared"], "v1") is None
    qdrant.query_points.assert_not_called()


def test_semantic_write_skips_qdrant_when_provider_unavailable():
    cache, qdrant = _cache()
    policy = SimpleNamespace(
        scope_key="shared",
        scope=SimpleNamespace(value="shared"),
        version="v1",
        sensitivity=SimpleNamespace(value="public"),
    )
    cache._qdrant_set("query", "response", policy, {}, 9999999999)
    qdrant.upsert.assert_not_called()
