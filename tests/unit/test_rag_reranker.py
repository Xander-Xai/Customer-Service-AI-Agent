"""
RAG Reranker + Query Rewriter 测试（v7.1）
BM25Reranker 已在 v7.1 移除，生产环境始终使用 ApiReranker。
"""

from rag.query_rewriter import QueryRewriter, create_query_rewriter
from rag.reranker import ApiReranker, create_reranker


class TestApiReranker:
    """API Reranker 测试"""

    def test_init_without_api_key(self):
        """API Key 未配置时 available=False"""
        reranker = ApiReranker(api_key="")
        assert reranker.available is False

    def test_rerank_fallback_when_unavailable(self):
        """不可用时返回原始结果"""
        reranker = ApiReranker(api_key="")
        results = [{"content": "测试", "distance": 0.5}]
        result = reranker.rerank("查询", results, top_k=3)
        assert result == results


class TestRerankerFactory:
    """Reranker 工厂函数测试"""

    def test_create_reranker_returns_api(self):
        """工厂函数返回 ApiReranker 实例"""
        reranker = create_reranker()
        assert isinstance(reranker, ApiReranker)

    def test_create_reranker_has_rerank_method(self):
        """工厂函数返回的实例有 rerank 方法"""
        reranker = create_reranker()
        assert hasattr(reranker, "rerank")


class TestQueryRewriter:
    """查询改写器测试"""

    def test_expand_query_with_synonym(self):
        """同义词扩展"""
        rewriter = QueryRewriter()
        expanded = rewriter.expand_query("烟酰胺有什么功效")
        assert "维生素B3" in expanded or "VB3" in expanded

    def test_expand_query_no_match(self):
        """无匹配时返回原始查询"""
        rewriter = QueryRewriter()
        query = "今天天气怎么样"
        expanded = rewriter.expand_query(query)
        assert expanded == query

    def test_expand_query_multiple_keywords(self):
        """多关键词扩展"""
        rewriter = QueryRewriter()
        expanded = rewriter.expand_query("烟酰胺精华美白效果好吗")
        # 应该包含烟酰胺和美白的同义词
        assert "维生素B3" in expanded or "VB3" in expanded
        assert "提亮" in expanded or "亮肤" in expanded

    def test_split_multi_question(self):
        """多问题拆分"""
        rewriter = QueryRewriter()
        questions = rewriter.split_multi_question("烟酰胺好用吗？敏感肌可以用吗？")
        assert len(questions) == 2

    def test_split_single_question(self):
        """单问题不拆分"""
        rewriter = QueryRewriter()
        questions = rewriter.split_multi_question("烟酰胺好用吗")
        assert len(questions) == 1
        assert questions[0] == "烟酰胺好用吗"

    def test_rewrite_for_collection(self):
        """针对 collection 改写查询"""
        rewriter = QueryRewriter()
        rewritten = rewriter.rewrite_for_collection("烟酰胺功效", "product_knowledge")
        assert rewritten.startswith("产品知识：")

    def test_rewrite_for_unknown_collection(self):
        """未知 collection 不加前缀"""
        rewriter = QueryRewriter()
        rewritten = rewriter.rewrite_for_collection("查询", "unknown_collection")
        assert rewritten == "查询"

    def test_custom_synonym_map(self):
        """自定义同义词映射"""
        custom_map = {"测试": ["test", "testing"]}
        rewriter = QueryRewriter(synonym_map=custom_map)
        expanded = rewriter.expand_query("这是一个测试")
        assert "test" in expanded

    def test_factory_function(self):
        """工厂函数创建实例"""
        rewriter = create_query_rewriter()
        assert isinstance(rewriter, QueryRewriter)


class TestKnowledgeBaseRerankerIntegration:
    """知识库 + Reranker 集成测试（ApiReranker）"""

    def test_apply_reranker_with_results(self):
        """_apply_reranker 调用 ApiReranker 处理结果"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        # 使用不可用的 ApiReranker（无 API Key），应返回原始排序
        kb._reranker = ApiReranker(api_key="")

        results = [
            {"content": "无关文档", "distance": 0.3},
            {"content": "烟酰胺有美白功效", "distance": 0.8},
        ]
        reranked = kb._apply_reranker("烟酰胺功效", results, top_k=2)
        assert len(reranked) == 2

    def test_apply_reranker_empty_results(self):
        """空结果不处理"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        kb._reranker = ApiReranker(api_key="")

        result = kb._apply_reranker("查询", [], top_k=3)
        assert result == []

    def test_apply_reranker_single_result(self):
        """单个结果不重排"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        kb._reranker = ApiReranker(api_key="")

        results = [{"content": "唯一结果", "distance": 0.5}]
        result = kb._apply_reranker("查询", results, top_k=3)
        assert len(result) == 1
