"""
RAG Reranker + Query Rewriter 测试（v7.1）
BM25Reranker 已在 v7.1 移除，生产环境始终使用 ApiReranker。
"""

from dataclasses import FrozenInstanceError

import httpx
import pytest

from rag.query_rewriter import QueryRewriter, create_query_rewriter
from rag.reranker import (
    REASON_INSUFFICIENT_CANDIDATES,
    ApiReranker,
    RerankOutcome,
    RerankReason,
    create_reranker,
)


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


# ============================================================================
# RerankOutcome: per-call truth (v6.3 reranker degradation contract)
#
# These tests never touch the network. They pin the property the whole change
# exists for: a rerank that did not happen can never be reported as one.
# ============================================================================



def _candidates(n: int = 3) -> list[dict]:
    return [{"content": f"doc-{i}", "distance": 0.1 * i} for i in range(n)]


def _resp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload,
                          request=httpx.Request("POST", "http://x/rerank"))


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/rerank")
    resp = httpx.Response(status_code=status, text="boom", request=req)
    return httpx.HTTPStatusError("err", request=req, response=resp)


class TestRerankOutcomeContract:
    def test_rerank_still_returns_a_plain_list(self, monkeypatch):
        """Backward compatibility: old callers get a list, not an outcome.

        传输层必须打桩：``ApiReranker(api_key="k")`` 会认为 provider 可用，
        不打桩就会真的向 ``RERANKER_BASE_URL`` 发一次请求 —— 违背本类的
        "never touch the network" 契约，也让断言结果依赖第三方可达性
        （issue #52：默认 lane 零出网）。
        """
        rr = ApiReranker(api_key="k")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(
            {"results": [{"index": 0, "relevance_score": 0.1},
                         {"index": 1, "relevance_score": 0.9},
                         {"index": 2, "relevance_score": 0.5}]}))
        out = rr.rerank("q", _candidates(), top_k=2)
        assert isinstance(out, list)
        assert len(out) == 2
        assert [r["content"] for r in out] == ["doc-1", "doc-2"]

    def test_outcome_is_frozen(self):
        outcome = RerankOutcome(results=[], applied=False, degraded=True,
                                reason=RerankReason.TIMEOUT)
        with pytest.raises(FrozenInstanceError):
            outcome.applied = True  # type: ignore[misc]

    def test_reason_values_are_bounded(self):
        """Reasons must stay a fixed enum — they reach meta, trace and telemetry."""
        assert {r.value for r in RerankReason} == {
            "", "unavailable", "timeout", "http_error",
            "provider_error", "invalid_response",
        }
        assert REASON_INSUFFICIENT_CANDIDATES == "insufficient_candidates"


class TestRerankSuccess:
    def test_real_rerank_reports_applied(self, monkeypatch):
        rr = ApiReranker(api_key="k")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(
            {"results": [{"index": 0, "relevance_score": 0.1},
                         {"index": 1, "relevance_score": 0.9},
                         {"index": 2, "relevance_score": 0.5}]}))
        out = rr.rerank_with_outcome("q", _candidates(), top_k=3)
        assert out.applied is True
        assert out.degraded is False
        assert out.reason is RerankReason.OK
        assert out.provider_called is True
        assert out.http_status == 200
        assert [r["content"] for r in out.results] == ["doc-1", "doc-2", "doc-0"]

    def test_outcome_never_reads_shared_last_error_status(self, monkeypatch):
        """The outcome is this call's fact, not the instance's last failure.

        Simulates the interleaving that makes `last_error_status` unusable for
        runtime attribution: a *different* request fails in between, and the
        first request's outcome must be unaffected.
        """
        rr = ApiReranker(api_key="k")

        def flaky_post(*_a, **_k):
            # Request A succeeds...
            return _resp({"results": [{"index": 0, "relevance_score": 0.9},
                                      {"index": 1, "relevance_score": 0.2}]})

        monkeypatch.setattr(httpx, "post", flaky_post)
        a = rr.rerank_with_outcome("qA", _candidates(2), top_k=2)
        # ...then request B fails and poisons the shared field.
        rr.last_error_status = 401
        assert a.applied is True and a.degraded is False


class TestRerankUnavailable:
    def test_unavailable_is_degraded_and_calls_no_provider(self):
        rr = ApiReranker(api_key="")
        out = rr.rerank_with_outcome("q", _candidates(3), top_k=2)
        assert out.applied is False
        assert out.degraded is True
        assert out.reason is RerankReason.UNAVAILABLE
        assert out.provider_called is False
        assert out.http_status is None
        assert [r["content"] for r in out.results] == ["doc-0", "doc-1"]

    def test_empty_input_is_neither_applied_nor_degraded(self):
        rr = ApiReranker(api_key="")
        out = rr.rerank_with_outcome("q", [], top_k=3)
        assert out.applied is False
        assert out.degraded is False
        assert out.results == []


class TestRerankProviderFailures:
    def test_timeout(self, monkeypatch):
        rr = ApiReranker(api_key="k")

        def boom(*_a, **_k):
            raise httpx.TimeoutException("slow")

        monkeypatch.setattr(httpx, "post", boom)
        out = rr.rerank_with_outcome("q", _candidates(3), top_k=3)
        assert out.applied is False
        assert out.degraded is True
        assert out.reason is RerankReason.TIMEOUT
        assert out.provider_called is True
        assert [r["content"] for r in out.results] == ["doc-0", "doc-1", "doc-2"]

    @pytest.mark.parametrize("status", [401, 429, 500])
    def test_http_error_carries_status_only(self, monkeypatch, status):
        rr = ApiReranker(api_key="k")

        def boom(*_a, **_k):
            raise _http_error(status)

        monkeypatch.setattr(httpx, "post", boom)
        out = rr.rerank_with_outcome("q", _candidates(3), top_k=2)
        assert out.applied is False
        assert out.degraded is True
        assert out.reason is RerankReason.HTTP_ERROR
        assert out.http_status == status
        assert [r["content"] for r in out.results] == ["doc-0", "doc-1"]

    def test_generic_provider_error(self, monkeypatch):
        rr = ApiReranker(api_key="k")

        def boom(*_a, **_k):
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr(httpx, "post", boom)
        out = rr.rerank_with_outcome("q", _candidates(3), top_k=2)
        assert out.applied is False
        assert out.degraded is True
        assert out.reason is RerankReason.PROVIDER_ERROR
        assert out.http_status is None

    def test_provider_failure_reason_never_contains_the_exception_text(self, monkeypatch):
        """Bounded enum only: the message could echo the request payload."""
        rr = ApiReranker(api_key="k")

        def boom(*_a, **_k):
            raise RuntimeError("SECRET-QUERY-TEXT leaked into exception")

        monkeypatch.setattr(httpx, "post", boom)
        out = rr.rerank_with_outcome("q", _candidates(2), top_k=2)
        assert "SECRET-QUERY-TEXT" not in out.reason_value
        assert out.reason_value == "provider_error"


class TestRerankInvalidResponse:
    """HTTP 200 is not proof of reranking."""

    @pytest.mark.parametrize(
        "payload",
        [
            {},                                    # no results key
            {"results": []},                       # empty results
            {"results": "nope"},                   # wrong type
            {"results": [{"relevance_score": 0.5}]},           # no index
            {"results": [{"index": 0}]},                        # no score
            {"results": [{"index": 0, "relevance_score": "high"}]},  # non-numeric
            {"results": [{"index": 99, "relevance_score": 0.9}]},    # out of range
            {"results": [{"index": -1, "relevance_score": 0.9}]},    # negative
            [1, 2, 3],                             # not an object
        ],
    )
    def test_malformed_200_is_never_reported_as_applied(self, monkeypatch, payload):
        rr = ApiReranker(api_key="k")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(payload))
        out = rr.rerank_with_outcome("q", _candidates(3), top_k=3)
        assert out.applied is False
        assert out.degraded is True
        assert out.reason is RerankReason.INVALID_RESPONSE
        assert [r["content"] for r in out.results] == ["doc-0", "doc-1", "doc-2"]

    def test_partial_valid_response_still_counts_as_applied(self, monkeypatch):
        """One usable score is enough to prove the candidates were reordered."""
        rr = ApiReranker(api_key="k")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(
            {"results": [{"index": 0, "relevance_score": 0.2},
                         {"index": 1, "relevance_score": 0.8},
                         {"index": 7, "relevance_score": 0.99}]}))
        out = rr.rerank_with_outcome("q", _candidates(2), top_k=2)
        assert out.applied is True
        assert [r["content"] for r in out.results] == ["doc-1", "doc-0"]

    def test_invalid_response_does_not_leak_payload_into_reason(self, monkeypatch):
        rr = ApiReranker(api_key="k")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(
            {"results": [{"index": 0, "relevance_score": 0.1}],
             "error": "SENSITIVE-PROVIDER-BODY"}))
        out = rr.rerank_with_outcome("q", _candidates(1), top_k=1)
        assert "SENSITIVE-PROVIDER-BODY" not in out.reason_value
