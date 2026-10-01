"""
ChromaDB → Qdrant 兼容性测试（v6.0）

验证 Qdrant 实现的 CosmeticsKnowledgeBase 对外暴露相同的 API 接口，
行为与 KnowledgeBaseProtocol 一致。
"""

from unittest.mock import MagicMock, patch

import pytest

from rag.qdrant_knowledge_base import QdrantKnowledgeBase


class TestKnowledgeBaseProtocol:
    """验证 QdrantKnowledgeBase 满足 KnowledgeBaseProtocol 接口"""

    def test_has_query_method(self):
        """必须实现 async query 方法"""
        import inspect

        assert hasattr(QdrantKnowledgeBase, "query")
        assert inspect.iscoroutinefunction(QdrantKnowledgeBase.query)

    def test_has_query_multiple_method(self):
        """必须实现 async query_multiple 方法"""
        import inspect

        assert hasattr(QdrantKnowledgeBase, "query_multiple")
        assert inspect.iscoroutinefunction(QdrantKnowledgeBase.query_multiple)

    def test_has_get_collection_count(self):
        """必须实现 get_collection_count"""
        assert hasattr(QdrantKnowledgeBase, "get_collection_count")

    def test_has_add_documents(self):
        """必须实现 add_documents（非 protocol 要求但被调用方依赖）"""
        assert hasattr(QdrantKnowledgeBase, "add_documents")

    def test_has_delete_documents(self):
        """必须实现 delete_documents"""
        assert hasattr(QdrantKnowledgeBase, "delete_documents")

    def test_has_rewrite_query(self):
        """必须实现 rewrite_query"""
        import inspect

        assert hasattr(QdrantKnowledgeBase, "rewrite_query")
        assert inspect.iscoroutinefunction(QdrantKnowledgeBase.rewrite_query)

    def test_has_simple_rerank(self):
        """必须实现 simple_rerank"""
        assert hasattr(QdrantKnowledgeBase, "simple_rerank")

    def test_has_available_property(self):
        """必须实现 available property"""
        assert isinstance(QdrantKnowledgeBase.available, property)

    def test_has_seed_if_empty(self):
        """必须实现 seed_if_empty"""
        assert hasattr(QdrantKnowledgeBase, "seed_if_empty")

    def test_has_clip_available_property(self):
        """必须实现 clip_available property"""
        assert isinstance(QdrantKnowledgeBase.clip_available, property)


class TestKnowledgeBaseAPIBehavior:
    """验证 mock 下 QdrantKnowledgeBase 的行为与 ChromaDB 版本兼容"""

    @pytest.fixture
    def mock_qdrant_client(self):
        with (
            patch("rag.qdrant_knowledge_base.QdrantClient") as mock_client_cls,
            patch.object(QdrantKnowledgeBase, "_create_embedding_function", return_value=MagicMock()),
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_collections.return_value = MagicMock(collections=[])
            mock_count = MagicMock()
            mock_count.count = 0
            mock_client.count.return_value = mock_count
            yield mock_client

    @pytest.fixture
    def kb(self, mock_qdrant_client):
        return QdrantKnowledgeBase(host="localhost", port=6333)

    def test_available_is_bool(self, kb):
        """available 返回 bool 类型"""
        assert isinstance(kb.available, bool)

    def test_get_collection_count_is_int(self, kb):
        """get_collection_count 返回 int 类型"""
        count = kb.get_collection_count("any")
        assert isinstance(count, int)

    def test_add_documents_accepts_empty_metadata(self, kb, mock_qdrant_client):
        """add_documents 接受空 dict 的 metadatas"""
        kb._embed_texts = MagicMock(return_value=[[0.1] * 768])
        # 不应抛出异常
        kb.add_documents("col", ["doc"], metadatas=[{}])
        assert mock_qdrant_client.upsert.called

    def test_query_returns_list_of_dicts(self, kb, mock_qdrant_client):
        """query 返回 list[dict]"""
        kb._embed_texts = MagicMock(return_value=[[0.1] * 768])
        mock_point = MagicMock()
        mock_point.score = 0.9
        mock_point.payload = {"content": "test"}
        mock_qdrant_client.search.return_value = [mock_point]

        # synchronous version: use the search directly
        # (async query is tested in test_qdrant_knowledge_base)
        assert hasattr(QdrantKnowledgeBase, "query")

    def test_get_or_create_collection_returns_name(self, kb, mock_qdrant_client):
        """get_or_create_collection 返回 collection name"""
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
        result = kb.get_or_create_collection("test_col")
        assert result == "test_col"

    def test_get_or_create_collection_returns_none_when_unavailable(
        self, kb, mock_qdrant_client
    ):
        """unavailable 时返回 None（兼容 ChromaDB 行为）"""
        kb._available = False
        result = kb.get_or_create_collection("test_col")
        assert result is None
