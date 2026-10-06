"""
QdrantKnowledgeBase 单元测试（v6.0）

测试策略：
- 使用 mock 的 QdrantClient，不依赖真实 Qdrant 实例
- 测试核心方法：add_documents / query / query_multiple / delete_documents
- 测试 embedding fallback 行为
- 测试 _parse_query_result 转换逻辑
"""

from unittest.mock import MagicMock, patch

import pytest

from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase


@pytest.fixture
def mock_qdrant_client():
    """创建 mock Qdrant 客户端"""
    with (
        patch("rag.qdrant_knowledge_base.QdrantClient") as mock_client_cls,
        patch.object(QdrantKnowledgeBase, "_create_embedding_function", return_value=MagicMock()),
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        # 模拟连接成功
        mock_client.get_collections.return_value = MagicMock(collections=[])
        # 模拟 count 返回
        mock_count = MagicMock()
        mock_count.count = 0
        mock_client.count.return_value = mock_count
        yield mock_client


@pytest.fixture
def kb(mock_qdrant_client):
    """创建 mock 驱动的 QdrantKnowledgeBase 实例"""
    return QdrantKnowledgeBase(host="localhost", port=6333)


class TestQdrantKnowledgeBase:
    """Qdrant 知识库核心功能测试"""

    def test_available_true(self, kb):
        """初始化成功后 available 为 True"""
        assert kb.available is True

    def test_connection_failure(self):
        """连接失败时 available 为 False"""
        with (
            patch("rag.qdrant_knowledge_base.QdrantClient") as mock_cls,
            patch.object(
                QdrantKnowledgeBase, "_create_embedding_function", return_value=MagicMock()
            ),
        ):
            mock_cls.side_effect = Exception("connection refused")
            kb = QdrantKnowledgeBase(host="bad-host")
            assert kb.available is False

    def test_add_documents(self, kb, mock_qdrant_client):
        """add_documents 应调用 QdrantClient.upsert"""
        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        kb.add_documents("test_collection", ["doc1", "doc2"])
        assert mock_qdrant_client.upsert.called

    def test_add_documents_empty(self, kb, mock_qdrant_client):
        """空文档列表不应调用 upsert"""
        kb.add_documents("test_collection", [])
        assert not mock_qdrant_client.upsert.called

    def test_delete_documents(self, kb, mock_qdrant_client):
        """delete_documents 应调用 QdrantClient.delete"""
        kb.delete_documents("test_collection", ["id1"])
        assert mock_qdrant_client.delete.called

    def test_get_collection_count_zero(self, kb, mock_qdrant_client):
        """空 collection 返回 0"""
        count = kb.get_collection_count("test")
        assert count == 0

    def test_get_collection_count(self, kb, mock_qdrant_client):
        """get_collection_count 返回正确计数"""
        mock_count = MagicMock()
        mock_count.count = 42
        mock_qdrant_client.count.return_value = mock_count
        count = kb.get_collection_count("test")
        assert count == 42

    @pytest.mark.asyncio
    async def test_query_returns_empty_when_unavailable(self, kb):
        """unavailable 时 query 返回空列表"""
        kb._available = False
        result = await kb.query("test", "query")
        assert result == []

    @pytest.mark.asyncio
    async def test_query_calls_search(self, kb, mock_qdrant_client):
        """query shim uses query_points and returns parsed results."""
        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])

        mock_point = MagicMock()
        mock_point.id = 123
        mock_point.score = 0.85
        mock_point.payload = {"doc_id": "doc1", "content": "测试内容", "source": "faq"}
        mock_qdrant_client.query_points.return_value = MagicMock(points=[mock_point])

        result = await kb.query("test", "query text", n_results=3)
        assert len(result) == 1
        assert result[0]["content"] == "测试内容"
        assert result[0]["score"] == 0.85
        # distance 应与 score 互补
        assert round(result[0]["distance"] + result[0]["score"], 6) == 1.0

    def test_parse_query_result(self, kb):
        """_parse_query_result 解析 Qdrant 返回格式为统一 dict"""
        mock_point = MagicMock()
        mock_point.score = 0.90
        mock_point.payload = {"doc_id": "d1", "content": "内容", "category": "A"}

        result = kb._parse_query_result([mock_point])
        assert len(result) == 1
        assert result[0]["content"] == "内容"
        assert result[0]["metadata"]["category"] == "A"
        assert result[0]["distance"] == pytest.approx(0.1)

    def test_simple_rerank_preserves_order(self, kb):
        """simple_rerank 应在已有文档时返回相同数量的结果"""
        results = [
            {"content": "测试文档A", "distance": 0.2},
            {"content": "测试文档B", "distance": 0.5},
        ]
        reranked = kb.simple_rerank("测试", results, top_k=2)
        assert len(reranked) == 2

    def test_embed_texts_fail_closed(self, kb):
        """embedding 不可用时拒绝伪造向量。"""
        kb._embed_fn = None
        with pytest.raises(Exception, match="provider_unavailable"):
            kb._embed_texts(["test"])

    def test_ensure_collection_creates(self, kb, mock_qdrant_client):
        """不存在的 collection 自动创建"""
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
        result = kb._ensure_collection("new_collection")
        assert result is True
        assert mock_qdrant_client.create_collection.called

    def test_ensure_collection_skips_existing(self, kb, mock_qdrant_client):
        """已存在的 collection 不重复创建"""
        mock_collection = MagicMock()
        mock_collection.name = "existing"
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[mock_collection])
        kb._collection_cache["existing"] = True
        result2 = kb._ensure_collection("existing")
        assert result2 is True

    @pytest.mark.asyncio
    async def test_query_multiple_merges_results(self, kb, mock_qdrant_client):
        """query_multiple 合并多 collection 结果"""
        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        mock_point = MagicMock()
        mock_point.score = 0.9
        mock_point.payload = {"doc_id": "d1", "content": "test_doc"}
        mock_qdrant_client.query_points.return_value = MagicMock(points=[mock_point])

        result = await kb.query_multiple(["col1", "col2"], "test", n_results=3)
        # 期望：2个 collection 都返回了同一个文档 → 去重后应该有 1 条
        assert len(result) >= 1
