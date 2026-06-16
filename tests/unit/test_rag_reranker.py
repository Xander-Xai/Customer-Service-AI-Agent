"""
RAG Reranker + Query Rewriter 测试（v5.1）
"""

from rag.query_rewriter import QueryRewriter, create_query_rewriter
from rag.reranker import BM25Reranker, CrossEncoderReranker, create_reranker


class TestBM25Reranker:
    """BM25 Reranker 测试"""

    def test_rerank_empty_results(self):
        """空结果返回空列表"""
        reranker = BM25Reranker()
        result = reranker.rerank("测试查询", [], top_k=3)
        assert result == []

    def test_rerank_single_result(self):
        """单个结果直接返回"""
        reranker = BM25Reranker()
        results = [{"content": "烟酰胺有美白功效", "distance": 0.5}]
        result = reranker.rerank("烟酰胺功效", results, top_k=3)
        assert len(result) == 1
        assert "rerank_score" in result[0]

    def test_rerank_relevant_doc_ranked_higher(self):
        """相关文档排名更高"""
        reranker = BM25Reranker()
        results = [
            {"content": "这是一个无关的文档内容", "distance": 0.3},
            {"content": "烟酰胺有美白提亮功效，适合油性肌肤", "distance": 0.8},
            {"content": "玻尿酸有保湿锁水功效", "distance": 0.5},
        ]
        result = reranker.rerank("烟酰胺功效", results, top_k=3)
        # 烟酰胺相关文档应该排在第一
        assert "烟酰胺" in result[0]["content"]

    def test_rerank_respects_top_k(self):
        """top_k 限制返回数量"""
        reranker = BM25Reranker()
        results = [{"content": f"文档{i}", "distance": float(i)} for i in range(10)]
        result = reranker.rerank("测试", results, top_k=3)
        assert len(result) == 3

    def test_tokenize_chinese(self):
        """中文分词"""
        tokens = BM25Reranker._tokenize("烟酰胺精华好用吗")
        # jieba 正确将"烟酰胺"作为整体词汇（niacinamide），不会拆成单字
        assert "烟酰胺" in tokens
        assert "精华" in tokens
        assert "好用" in tokens

    def test_tokenize_mixed(self):
        """中英文混合分词"""
        tokens = BM25Reranker._tokenize("VC精华 vitamin c")
        assert "vc" in tokens
        assert "vitamin" in tokens
        # jieba 将"精华"作为整体词汇
        assert "精华" in tokens


class TestCrossEncoderReranker:
    """CrossEncoder Reranker 测试"""

    def test_init_without_sentence_transformers(self):
        """sentence-transformers 不可用时 graceful 降级"""
        # CrossEncoderReranker 在没有 sentence-transformers 时应设置 available=False
        reranker = CrossEncoderReranker(model_name="nonexistent-model-12345")
        assert reranker.available is False

    def test_rerank_fallback_when_unavailable(self):
        """不可用时返回原始结果"""
        reranker = CrossEncoderReranker(model_name="nonexistent-model")
        results = [{"content": "测试", "distance": 0.5}]
        result = reranker.rerank("查询", results, top_k=3)
        assert result == results


class TestRerankerFactory:
    """Reranker 工厂函数测试"""

    def test_create_reranker_returns_bm25_fallback(self):
        """sentence-transformers 不可用时返回 BM25"""
        reranker = create_reranker(prefer_cross_encoder=False)
        assert isinstance(reranker, BM25Reranker)

    def test_create_reranker_returns_type(self):
        """工厂函数返回 reranker 实例"""
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
    """知识库 + Reranker 集成测试"""

    def test_apply_reranker_with_results(self):
        """_apply_reranker 对结果排序"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        kb._reranker = BM25Reranker()

        results = [
            {"content": "无关文档", "distance": 0.3},
            {"content": "烟酰胺有美白功效", "distance": 0.8},
        ]
        reranked = kb._apply_reranker("烟酰胺功效", results, top_k=2)
        assert len(reranked) == 2
        assert "rerank_score" in reranked[0]

    def test_apply_reranker_empty_results(self):
        """空结果不处理"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        kb._reranker = BM25Reranker()

        result = kb._apply_reranker("查询", [], top_k=3)
        assert result == []

    def test_apply_reranker_single_result(self):
        """单个结果不重排"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = CosmeticsKnowledgeBase.__new__(CosmeticsKnowledgeBase)
        kb._reranker = BM25Reranker()

        results = [{"content": "唯一结果", "distance": 0.5}]
        result = kb._apply_reranker("查询", results, top_k=3)
        assert len(result) == 1
