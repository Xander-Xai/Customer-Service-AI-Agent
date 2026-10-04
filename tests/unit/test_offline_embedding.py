"""OfflineEmbedding + embedding 工厂 + EMBEDDING_PROVIDER 校验的契约测试（issue #52）。

这些断言在 #52 之前不可能通过：那时既没有 ``EMBEDDING_PROVIDER``，也没有
``rag/offline_embedding.py``，默认 lane 只会构造真实 ``ApiEmbedding``。
"""

from __future__ import annotations

import numpy as np
import pytest

from core.config import validate_embedding_provider
from rag.embedding_factory import create_embedding_model
from rag.embedding_status import validate_embedding_vector
from rag.offline_embedding import OFFLINE_EMBEDDING_DIM, OfflineEmbedding
from rag.qdrant_knowledge_base import _EMBEDDING_DIM


class TestDimContract:
    def test_dim_matches_the_vector_channel_validator(self):
        """维度必须与 ``QdrantKnowledgeBase`` 校验用的常量一致。

        不一致时向量会被 ``validate_embedding_vector`` 拒收，向量通道整体关闭 ——
        也就是「测试又变成靠降级通过」的老问题。
        """
        assert OFFLINE_EMBEDDING_DIM == _EMBEDDING_DIM

    def test_rejects_non_positive_dim(self):
        with pytest.raises(ValueError, match="dim must be positive"):
            OfflineEmbedding(dim=0)


class TestDeterminism:
    def test_same_text_same_vector_across_instances(self):
        a = OfflineEmbedding().encode("烟酰胺精华")
        b = OfflineEmbedding().encode("烟酰胺精华")
        assert np.array_equal(a, b)

    def test_same_text_same_vector_for_batch_member(self):
        m = OfflineEmbedding()
        batched = m.encode(["烟酰胺精华", "视黄醇面霜"])
        assert np.array_equal(batched[0], m.encode("烟酰胺精华"))

    def test_different_text_gives_different_vector(self):
        m = OfflineEmbedding()
        assert not np.allclose(m.encode("烟酰胺精华"), m.encode("水杨酸洁面"))

    def test_empty_string_still_produces_a_valid_vector(self):
        v = OfflineEmbedding().encode("")
        assert v.shape == (_EMBEDDING_DIM,)
        validate_embedding_vector(v.tolist(), _EMBEDDING_DIM)


class TestShapeAndValidity:
    def test_single_text_returns_1d(self):
        v = OfflineEmbedding().encode("hello")
        assert v.shape == (_EMBEDDING_DIM,)
        assert v.dtype == np.float32

    def test_list_returns_2d_preserving_order(self):
        vs = OfflineEmbedding().encode(["a", "b", "c"])
        assert vs.shape == (3, _EMBEDDING_DIM)

    @pytest.mark.parametrize("text", ["烟酰胺", "retinol cream", "水杨酸", "", "x" * 5000])
    def test_vector_passes_the_production_validator(self, text):
        """离线向量必须能通过生产侧的维度 / 有限性校验（不能是坏向量）。"""
        v = OfflineEmbedding().encode(text)
        validate_embedding_vector(v.tolist(), _EMBEDDING_DIM)

    def test_vectors_are_l2_normalized(self):
        v = OfflineEmbedding().encode("烟酰胺精华")
        assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)

    def test_all_components_finite(self):
        v = OfflineEmbedding().encode("烟酰胺精华")
        assert np.all(np.isfinite(v))

    @pytest.mark.asyncio
    async def test_aencode_matches_encode(self):
        m = OfflineEmbedding()
        assert np.array_equal(await m.aencode("烟酰胺"), m.encode("烟酰胺"))


class TestProviderFactory:
    def test_offline_selects_the_offline_embedder(self):
        model = create_embedding_model("offline")
        assert isinstance(model, OfflineEmbedding)
        assert model.is_offline is True

    def test_api_selects_the_http_embedder(self):
        from rag.api_embedding import ApiEmbedding

        model = create_embedding_model("api", api_key="k", model="m", base_url="https://x/v1")
        assert isinstance(model, ApiEmbedding)
        assert not getattr(model, "is_offline", False)

    def test_value_is_case_and_space_insensitive(self):
        assert isinstance(create_embedding_model("  OFFLINE "), OfflineEmbedding)

    @pytest.mark.parametrize("bad", ["", "openai", "sentence-transformers", "ap1", "none"])
    def test_unknown_provider_fails_loudly_instead_of_defaulting_to_api(self, bad):
        """拼错的 provider 绝不能静默变成一次真实 provider 调用。"""
        with pytest.raises(ValueError, match="unknown EMBEDDING_PROVIDER"):
            create_embedding_model(bad)

    def test_factory_does_no_io_for_offline(self, monkeypatch):
        """离线 embedder 的构造与编码都不得触碰 httpx。"""
        import httpx

        def _boom(*a, **kw):
            raise AssertionError("offline provider must not touch httpx")

        monkeypatch.setattr(httpx, "post", _boom)
        monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
        model = create_embedding_model("offline")
        model.encode(["烟酰胺", "视黄醇"])


class TestConfigValidation:
    def test_api_is_valid_in_production(self):
        assert validate_embedding_provider("api", dev_mode=False) == []

    def test_offline_is_valid_in_dev_lane(self):
        assert validate_embedding_provider("offline", dev_mode=True) == []

    def test_offline_is_rejected_in_production(self):
        """生产用离线 embedder = 让线上 RAG 静默退化成哈希向量，必须拒绝启动。"""
        problems = validate_embedding_provider("offline", dev_mode=False)
        assert len(problems) == 1
        assert "production" in problems[0].lower() or "生产" in problems[0]

    def test_unknown_value_is_rejected_in_both_modes(self):
        assert validate_embedding_provider("nope", dev_mode=True)
        assert validate_embedding_provider("nope", dev_mode=False)

    def test_known_values_are_documented(self):
        from core.config import EMBEDDING_PROVIDERS

        assert EMBEDDING_PROVIDERS == ("api", "offline")

    def test_validate_required_config_actually_wires_this_check(self):
        """fail closed 只有在被接进 ``validate_required_config`` 时才成立。

        纯函数正确但忘了接线 = 生产照样能带着 offline embedder 起来。这里按本仓库
        既有的源码契约测试惯例钉住接线（与
        ``tests/unit/test_rag_eval_harness.py`` 的 preflight 源码断言同类）。
        """
        import inspect

        from core import config

        source = inspect.getsource(config.validate_required_config)
        assert "validate_embedding_provider(" in source, (
            "validate_embedding_provider 未接入 validate_required_config："
            "生产会用 EMBEDDING_PROVIDER=offline 静默启动"
        )
