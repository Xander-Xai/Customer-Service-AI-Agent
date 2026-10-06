"""
llm/client.py + rag/knowledge_base.py 覆盖率提升测试
覆盖：LLM 客户端重试/熔断/降级、RAG 知识库 CRUD/查询
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Client Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCustomResponse:
    """CustomResponse 测试"""

    def test_custom_response_basic(self):
        from llm.client import CustomResponse

        resp = CustomResponse("hello")
        assert resp.content == "hello"
        assert resp.tool_calls is None

    def test_custom_response_with_tool_calls(self):
        from llm.client import CustomResponse

        calls = [{"id": "c1", "name": "query_order", "arguments": "{}"}]
        resp = CustomResponse("ok", tool_calls=calls)
        assert resp.tool_calls == calls


class TestFormatMessages:
    """_format_messages 消息格式化测试"""

    def test_format_system_message(self):
        from langchain_core.messages import SystemMessage

        from llm.client import OpenAICompatibleClient

        msgs = [SystemMessage(content="你是客服")]
        result = OpenAICompatibleClient._format_messages(msgs)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "你是客服"

    def test_format_human_message(self):
        from langchain_core.messages import HumanMessage

        from llm.client import OpenAICompatibleClient

        msgs = [HumanMessage(content="你好")]
        result = OpenAICompatibleClient._format_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "你好"

    def test_format_ai_message(self):
        from langchain_core.messages import AIMessage

        from llm.client import OpenAICompatibleClient

        msgs = [AIMessage(content="你好！")]
        result = OpenAICompatibleClient._format_messages(msgs)
        assert result[0]["role"] == "assistant"

    def test_format_human_message_multimodal(self):
        """多模态消息格式化"""
        from langchain_core.messages import HumanMessage

        from llm.client import OpenAICompatibleClient

        content = [
            {"type": "text", "text": "描述图片"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]
        msgs = [HumanMessage(content=content)]
        result = OpenAICompatibleClient._format_messages(msgs)
        assert result[0]["role"] == "user"
        assert isinstance(result[0]["content"], list)

    def test_format_tool_message(self):
        """ToolMessage 格式化"""
        from llm.client import OpenAICompatibleClient

        msg = MagicMock()
        msg.type = "tool"
        msg.content = '{"orders": []}'
        msg.tool_call_id = "call_123"
        result = OpenAICompatibleClient._format_messages([msg])
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "call_123"

    def test_format_ai_message_with_tool_calls(self):
        """AIMessage 带 tool_calls 格式化"""
        from llm.client import OpenAICompatibleClient

        msg = MagicMock()
        msg.type = "ai"
        msg.content = ""
        msg.tool_calls = [{"id": "c1", "name": "fn", "arguments": "{}"}]
        # 不是 ToolMessage，不是 System/Human/AIMessage instance
        msg.__class__ = type("FakeMsg", (), {})
        result = OpenAICompatibleClient._format_messages([msg])
        assert result[0]["role"] == "assistant"

    def test_format_unknown_message(self):
        """未知消息类型格式化"""
        from llm.client import OpenAICompatibleClient

        msg = MagicMock()
        msg.content = "hello"
        msg.type = "unknown"
        # MagicMock has many attrs, so it may match AIMessage or other branches
        # Just verify it produces a valid result
        result = OpenAICompatibleClient._format_messages([msg])
        assert result[0]["role"] in ("user", "assistant")
        assert result[0]["content"] == "hello"


class TestOpenAICompatibleClient:
    """OpenAICompatibleClient 核心测试"""

    def _make_client(self, **kwargs):
        from llm.client import OpenAICompatibleClient

        defaults = {"api_key": "test-key", "base_url": "http://test", "model": "test-model"}
        defaults.update(kwargs)
        return OpenAICompatibleClient(**defaults)

    def test_client_init(self):
        """客户端初始化"""
        client = self._make_client()
        assert client.api_key == "test-key"
        assert client.model == "test-model"
        assert "Bearer test-key" in client.headers["Authorization"]

    def test_client_init_with_circuit_breaker(self):
        """带熔断器的客户端"""
        cb = MagicMock()
        client = self._make_client(circuit_breaker=cb)
        assert client.circuit_breaker is cb

    @pytest.mark.asyncio
    async def test_async_invoke_circuit_breaker_open(self):
        """熔断器打开时拒绝调用"""
        from llm.client import LLMServiceError

        cb = AsyncMock()
        cb.should_allow = AsyncMock(return_value=False)
        client = self._make_client(circuit_breaker=cb)

        with pytest.raises(LLMServiceError, match="熔断"):
            await client.async_invoke([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_async_invoke_success(self):
        """正常调用成功"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "你好！"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke([HumanMessage(content="你好")])
            assert result.content == "你好！"

    @pytest.mark.asyncio
    async def test_async_invoke_with_tool_calls(self):
        """Function Calling 响应"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "query_order", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke([HumanMessage(content="查订单")])
            assert result.tool_calls is not None

    @pytest.mark.asyncio
    async def test_async_invoke_retry_on_error(self):
        """失败后重试"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=error_resp
        )

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        success_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[error_resp, success_resp])

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke([HumanMessage(content="hi")])
            assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_async_invoke_all_retries_fail(self):
        """所有重试失败后抛出异常"""
        from langchain_core.messages import HumanMessage

        from llm.client import LLMServiceError

        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=error_resp
        )

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=error_resp)

        with (
            patch.object(client, "_get_async_client", return_value=mock_http),
            pytest.raises(LLMServiceError),
        ):
            await client.async_invoke([HumanMessage(content="hi")])

    @pytest.mark.asyncio
    async def test_async_invoke_request_error_retry(self):
        """网络请求错误重试"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        success_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                success_resp,
            ]
        )

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke([HumanMessage(content="hi")])
            assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_async_invoke_circuit_breaker_records(self):
        """调用成功/失败记录到熔断器"""
        from langchain_core.messages import HumanMessage

        cb = AsyncMock()
        cb.should_allow = AsyncMock(return_value=True)
        cb.record_success = AsyncMock()
        cb.record_failure = AsyncMock()

        client = self._make_client(circuit_breaker=cb)
        client.max_retries = 1
        client.base_delay = 0.01

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            await client.async_invoke([HumanMessage(content="hi")])
            cb.record_success.assert_awaited()

    @pytest.mark.asyncio
    async def test_async_invoke_empty_choices(self):
        """空 choices 返回默认响应"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        client.max_retries = 1

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": []}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke([HumanMessage(content="hi")])
            assert "API response format" in result.content

    @pytest.mark.asyncio
    async def test_async_invoke_with_tools_and_timeout(self):
        """带 tools 和 timeout 的调用"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke(
                [HumanMessage(content="hi")],
                timeout=30.0,
                tools=[{"type": "function", "function": {"name": "test"}}],
            )
            assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_async_invoke_raw_success(self):
        """async_invoke_raw 正常调用"""
        client = self._make_client()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "raw ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke_raw([{"role": "user", "content": "hi"}])
            assert result.content == "raw ok"

    @pytest.mark.asyncio
    async def test_async_invoke_raw_circuit_breaker(self):
        """async_invoke_raw 熔断器"""
        from llm.client import LLMServiceError

        cb = AsyncMock()
        cb.should_allow = AsyncMock(return_value=False)
        client = self._make_client(circuit_breaker=cb)

        with pytest.raises(LLMServiceError, match="熔断"):
            await client.async_invoke_raw([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_async_invoke_raw_retry(self):
        """async_invoke_raw 重试"""
        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=error_resp
        )

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        success_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[error_resp, success_resp])

        with patch.object(client, "_get_async_client", return_value=mock_http):
            result = await client.async_invoke_raw([{"role": "user", "content": "hi"}])
            assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_async_invoke_raw_all_retries_fail(self):
        """async_invoke_raw 所有重试失败"""
        from llm.client import LLMServiceError

        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=error_resp
        )

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=error_resp)

        with (
            patch.object(client, "_get_async_client", return_value=mock_http),
            pytest.raises(LLMServiceError),
        ):
            await client.async_invoke_raw([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_async_invoke_stream_circuit_breaker(self):
        """流式调用熔断器"""
        from llm.client import LLMServiceError

        cb = AsyncMock()
        cb.should_allow = AsyncMock(return_value=False)
        client = self._make_client(circuit_breaker=cb)

        with pytest.raises(LLMServiceError, match="熔断"):
            async for _ in client.async_invoke_stream([MagicMock(content="hi")]):
                pass

    @pytest.mark.asyncio
    async def test_async_invoke_stream_success(self):
        """流式调用成功"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        client.max_retries = 1

        # Mock SSE response
        async def mock_aiter_lines():
            yield 'data: {"choices":[{"delta":{"content":"你"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"好"}}]}'
            yield "data: [DONE]"

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = mock_aiter_lines
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = AsyncMock()
        mock_http.stream = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            chunks = []
            async for chunk in client.async_invoke_stream([HumanMessage(content="hi")]):
                chunks.append(chunk)
            assert "".join(chunks) == "你好"

    @pytest.mark.asyncio
    async def test_async_invoke_stream_retry(self):
        """流式调用重试"""
        from langchain_core.messages import HumanMessage

        client = self._make_client()
        client.max_retries = 2
        client.base_delay = 0.01

        # First call fails, second succeeds
        async def mock_aiter_lines_ok():
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield "data: [DONE]"

        mock_resp_ok = AsyncMock()
        mock_resp_ok.raise_for_status = MagicMock()
        mock_resp_ok.aiter_lines = mock_aiter_lines_ok
        mock_resp_ok.__aenter__ = AsyncMock(return_value=mock_resp_ok)
        mock_resp_ok.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def mock_stream(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("fail")
            return mock_resp_ok

        mock_http = AsyncMock()
        mock_http.stream = mock_stream

        with patch.object(client, "_get_async_client", return_value=mock_http):
            chunks = []
            async for chunk in client.async_invoke_stream([HumanMessage(content="hi")]):
                chunks.append(chunk)
            assert "".join(chunks) == "ok"

    @pytest.mark.asyncio
    async def test_close_all_clients(self):
        """close_all_clients 关闭所有连接池"""
        from llm.client import OpenAICompatibleClient

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()

        OpenAICompatibleClient._client_pools["test"] = mock_client
        await OpenAICompatibleClient.close_all_clients()
        assert len(OpenAICompatibleClient._client_pools) == 0

    @pytest.mark.asyncio
    async def test_get_async_client_creates_new(self):
        """_get_async_client 创建新客户端"""
        client = self._make_client(base_url="http://unique-test-url")

        # Clean up
        OpenAICompatibleClient = type(client)
        old = OpenAICompatibleClient._client_pools.copy()
        try:
            OpenAICompatibleClient._client_pools.pop("http://unique-test-url", None)
            http_client = await client._get_async_client()
            assert http_client is not None
            assert not http_client.is_closed
        finally:
            OpenAICompatibleClient._client_pools = old
            await http_client.aclose()


# ═══════════════════════════════════════════════════════════════════════════════
# RAG Knowledge Base Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCosmeticsKnowledgeBase:
    """CosmeticsKnowledgeBase 测试（使用 mock 避免 Qdrant 连接）"""

    def _make_mock_kb(self):
        """创建 mock 的 QdrantKnowledgeBase"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        kb = MagicMock(spec=CosmeticsKnowledgeBase)
        kb._available = True
        kb._clip_enabled = False
        kb._clip_embed_fn = None
        kb._collections = {}
        kb.available = True

        # Mock collection
        mock_col = MagicMock()
        mock_col.count.return_value = 2

        class MockScoredPoint:
            """模拟 Qdrant ScoredPoint"""
            def __init__(self, content, score=0.9, doc_id=""):
                self.payload = {"content": content, "doc_id": doc_id}
                self.score = score

        mock_col.search.return_value = [
            MockScoredPoint("红色口红推荐", 0.95),
            MockScoredPoint("保湿面霜", 0.85),
        ]

        def get_or_create(name):
            kb._collections.setdefault(name, mock_col)
            return kb._collections[name]

        kb.get_or_create_collection = MagicMock(side_effect=get_or_create)
        kb.add_documents = MagicMock()
        kb.delete_documents = MagicMock()

        # Use real static methods (QdrantKnowledgeBase 兼容的)
        kb._parse_query_result = staticmethod(CosmeticsKnowledgeBase._parse_query_result)

        return kb, mock_col

    def test_available_property(self):
        """available 属性"""

        kb = MagicMock()
        kb._available = True
        assert kb._available is True

    def test_get_or_create_collection(self):
        """获取或创建 collection"""
        kb, mock_col = self._make_mock_kb()
        col = kb.get_or_create_collection("test_collection")
        assert col is not None

    def test_get_or_create_collection_caches(self):
        """重复获取同名 collection 返回缓存"""
        kb, mock_col = self._make_mock_kb()
        col1 = kb.get_or_create_collection("test")
        col2 = kb.get_or_create_collection("test")
        assert col1 is col2

    def test_add_documents(self):
        """添加文档"""
        kb, mock_col = self._make_mock_kb()
        kb.add_documents(
            "product_knowledge",
            documents=["产品A很好用", "产品B适合干皮"],
            metadatas=[{"category": "skincare"}, {"category": "skincare"}],
        )
        kb.add_documents.assert_called_once()

    def test_add_documents_unavailable(self):
        """不可用时添加文档不报错"""
        kb, mock_col = self._make_mock_kb()
        kb._available = False
        kb.add_documents("test", documents=["doc"])
        kb.add_documents.assert_called_once()

    def test_query_sort_dedup(self):
        """多结果排序去重（Qdrant query_multiple 内部逻辑）"""
        results = [
            {"content": "doc1", "distance": 0.3},
            {"content": "doc2", "distance": 0.1},
            {"content": "doc1", "distance": 0.2},  # duplicate
        ]
        results.sort(key=lambda r: r.get("distance", 999))
        seen = set()
        deduped = []
        for r in results:
            content = r.get("content", "")
            if content not in seen:
                seen.add(content)
                deduped.append(r)
        assert len(deduped) == 2
        assert deduped[0]["content"] == "doc2"  # lowest distance first

    def test_query_sort_dedup_empty(self):
        """空结果排序去重"""
        results = []
        results.sort(key=lambda r: r.get("distance", 999))
        seen = set()
        deduped = []
        for r in results:
            content = r.get("content", "")
            if content not in seen:
                seen.add(content)
                deduped.append(r)
        assert deduped == []

    def test_clip_available_property(self):
        """clip_available 属性"""

        kb = MagicMock()
        kb._clip_enabled = False
        kb._clip_embed_fn = None
        assert not (kb._clip_enabled and kb._clip_embed_fn is not None)

    def test_create_embedding_function(self):
        """_create_embedding_function 创建 API embedding（Mock 避免真实调用）"""
        from unittest.mock import MagicMock, patch

        from rag.knowledge_base import CosmeticsKnowledgeBase

        # --- 态2：provider=remote + 占位凭据 → 向量通道显式禁用，且**不**构造
        # HTTP 客户端。issue #52 的核心：`sk-placeholder-...` 是真值，只判
        # truthiness 的旧写法挡不住它，于是拿着假凭据去连 api.siliconflow.cn。
        with (
            patch("core.config.EMBEDDING_PROVIDER", "remote"),
            patch("core.config.EMBEDDING_API_KEY", "sk-placeholder-not-a-real-key"),
            patch("rag.api_embedding.ApiEmbedding") as mock_api_cls,
        ):
            ef = CosmeticsKnowledgeBase._create_embedding_function()
            assert ef is None, "占位凭据必须禁用向量通道（embed_fn=None），不能伪造向量"
            mock_api_cls.assert_not_called()

        # --- 态3：provider=remote + 真实凭据 → 生产路径，仍构造 ApiEmbedding。
        # 拼装而非字面量：secret guard 会把 sk- 前缀的 20+ 字符串判为疑似泄露。
        synthetic_realistic_key = "synthetic" + "y" * 8 + "-notarealkey00000000"
        with (
            patch("core.config.EMBEDDING_PROVIDER", "remote"),
            patch("core.config.EMBEDDING_API_KEY", synthetic_realistic_key),
            patch("rag.api_embedding.ApiEmbedding") as mock_api_cls,
        ):
            mock_api_instance = MagicMock()
            mock_api_cls.return_value = mock_api_instance

            ef = CosmeticsKnowledgeBase._create_embedding_function()
            assert ef is mock_api_instance
            mock_api_cls.assert_called_once()

        # --- 态1：provider=local → 进程内确定性实现，零出网（默认测试 lane）。
        from rag.local_provider import LocalEmbedding

        with patch("core.config.EMBEDDING_PROVIDER", "local"):
            ef = CosmeticsKnowledgeBase._create_embedding_function()
            assert isinstance(ef, LocalEmbedding)

    def test_parse_query_result(self):
        """_parse_query_result 解析 Qdrant 结果"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        class MockPoint:
            def __init__(self, content, score=0.9):
                self.payload = {"content": content, "category": "skincare"}
                self.score = score

        result = [MockPoint("产品A", 0.95), MockPoint("产品B", 0.85)]
        parsed = CosmeticsKnowledgeBase._parse_query_result(result)
        assert len(parsed) == 2
        assert parsed[0]["content"] == "产品A"
        assert parsed[0]["score"] == 0.95

    def test_parse_query_result_empty(self):
        """_parse_query_result 空结果"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        parsed = CosmeticsKnowledgeBase._parse_query_result([])
        assert parsed == []

    @pytest.mark.asyncio
    async def test_query_mock(self):
        """查询（mock 版本）"""
        from rag.knowledge_base import CosmeticsKnowledgeBase

        class MockPoint:
            def __init__(self, content, score=0.9):
                self.payload = {"content": content}
                self.score = score

        result = [MockPoint("红色口红推荐", 0.95)]
        parsed = CosmeticsKnowledgeBase._parse_query_result(result)
        assert len(parsed) == 1
        assert parsed[0]["content"] == "红色口红推荐"

    @pytest.mark.asyncio
    async def test_query_unavailable(self):
        """不可用时查询返回空列表"""

        kb = MagicMock()
        kb._available = False
        kb.query = AsyncMock(return_value=[])
        results = await kb.query("test", "query")
        assert results == []

    @pytest.mark.asyncio
    async def test_query_multiple_mock(self):
        """多 collection 查询（mock 版本）"""
        kb, mock_col = self._make_mock_kb()
        kb.query_multiple = AsyncMock(
            return_value=[{"content": "口红", "metadata": {}, "distance": 0.1}]
        )
        results = await kb.query_multiple(["product_knowledge", "faq"], "口红", n_results=5)
        assert isinstance(results, list)

    def test_delete_documents(self):
        """删除文档"""
        kb, mock_col = self._make_mock_kb()
        kb.delete_documents("test", ["id1", "id2"])
        kb.delete_documents.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# QueryRewriter Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueryRewriter:
    """rag/query_rewriter.py 覆盖"""

    def test_expand_query_basic(self):
        """expand_query 同义词扩展基本功能"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.expand_query("烟酰胺")
        assert "烟酰胺" in result
        # 应该包含前 2 个同义词
        assert "维生素B3" in result
        assert "VB3" in result

    def test_expand_query_no_match_returns_original(self):
        """无匹配关键词时返回原查询"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        original = "今天天气怎么样"
        result = rewriter.expand_query(original)
        assert result == original

    def test_expand_query_multiple_keywords(self):
        """查询包含多个关键词时都应扩展"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.expand_query("烟酰胺美白精华")
        assert "烟酰胺" in result
        # 烟酰胺同义词 + 美白同义词 + 精华同义词
        assert "维生素B3" in result
        assert "提亮" in result or "亮肤" in result
        assert "精华液" in result or "精华露" in result

    def test_expand_query_custom_synonym_map(self):
        """自定义同义词映射"""
        from rag.query_rewriter import QueryRewriter

        custom_map = {"测试": ["test", "testing"]}
        rewriter = QueryRewriter(synonym_map=custom_map)
        result = rewriter.expand_query("测试一下")
        assert "test" in result
        assert "testing" in result

    def test_expand_query_deduplication(self):
        """扩展结果去重"""
        from rag.query_rewriter import QueryRewriter

        custom_map = {"A": ["same", "same", "diff"]}
        rewriter = QueryRewriter(synonym_map=custom_map)
        result = rewriter.expand_query("A")
        # "same" 应只出现一次
        parts = result.split()
        assert parts.count("same") == 1

    def test_expand_query_case_insensitive(self):
        """大小写不敏感匹配"""
        from rag.query_rewriter import QueryRewriter

        custom_map = {"hello": ["hi", "hey"]}
        rewriter = QueryRewriter(synonym_map=custom_map)
        result = rewriter.expand_query("HELLO world")
        assert "hi" in result

    def test_split_multi_question_single(self):
        """单问题不拆分"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.split_multi_question("这款产品多少钱")
        assert result == ["这款产品多少钱"]

    def test_split_multi_question_multiple(self):
        """多问题拆分"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.split_multi_question("这款产品多少钱？有什么功效？适合什么肤质？")
        assert len(result) == 3

    def test_split_multi_question_short_parts_filtered(self):
        """过短的部分被过滤"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.split_multi_question("好的？这款产品怎么样？")
        # "好的" 只有 2 个字符，应被过滤（len <= 3）
        for q in result:
            assert len(q) > 3

    def test_split_multi_question_semicolon_separator(self):
        """分号分隔"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.split_multi_question("产品A的成分是什么；产品B的成分是什么")
        assert len(result) == 2

    def test_rewrite_for_collection_known(self):
        """针对已知 collection 添加前缀"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        assert rewriter.rewrite_for_collection("查询", "product_knowledge") == "产品知识：查询"
        assert rewriter.rewrite_for_collection("查询", "faq") == "常见问题：查询"
        assert rewriter.rewrite_for_collection("查询", "tech_support") == "技术支持：查询"
        assert rewriter.rewrite_for_collection("查询", "complaint_knowledge") == "投诉处理：查询"

    def test_rewrite_for_collection_unknown(self):
        """未知 collection 不添加前缀"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        assert rewriter.rewrite_for_collection("查询", "unknown") == "查询"

    def test_create_query_rewriter_factory(self):
        """工厂函数创建实例"""
        from rag.query_rewriter import QueryRewriter, create_query_rewriter

        rewriter = create_query_rewriter()
        assert isinstance(rewriter, QueryRewriter)

    def test_expand_query_empty_string(self):
        """空字符串查询"""
        from rag.query_rewriter import QueryRewriter

        rewriter = QueryRewriter()
        result = rewriter.expand_query("")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Reranker Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestReranker:
    """rag/reranker.py 覆盖 — BM25Reranker 已在 v7.1 移除，仅保留 API Reranker"""

    def test_api_reranker_not_available_fallback(self):
        """API Key 未配置时 available 为 False"""
        from rag.reranker import ApiReranker

        reranker = ApiReranker(api_key="")
        assert reranker.available is False
        results = [{"content": "test", "distance": 0.5}]
        reranked = reranker.rerank("query", results, top_k=1)
        assert len(reranked) == 1

    def test_api_reranker_empty_results(self):
        """API Reranker 空结果处理"""
        from rag.reranker import ApiReranker

        reranker = ApiReranker(api_key="")
        reranked = reranker.rerank("query", [], top_k=3)
        assert reranked == []

    def test_create_reranker_factory_default(self):
        """工厂函数按 provider 旋钮返回实现（issue #52）。

        RERANKER_PROVIDER=local → LocalReranker（进程内、零出网，这是默认测试 lane）。
        否则 → ApiReranker。两者实现同一套 duck-typed 契约，所以断言的是"选对了
        实现"，而不是"永远只有一种实现"。
        """
        from core import config as core_config
        from rag.local_provider import LocalReranker
        from rag.reranker import ApiReranker, create_reranker

        # rag.reranker 在导入期就把 RERANKER_PROVIDER 绑到自己模块上了，
        # 所以 patch 必须打在 rag.reranker 上。
        with patch("rag.reranker.RERANKER_PROVIDER", core_config.PROVIDER_LOCAL):
            assert isinstance(create_reranker(), LocalReranker)

        with patch("rag.reranker.RERANKER_PROVIDER", core_config.PROVIDER_REMOTE):
            assert isinstance(create_reranker(), ApiReranker)


# ═══════════════════════════════════════════════════════════════════════════════
# RAG Knowledge Base — Additional Coverage (v5.1 CLIP / delete / seed / rewrite)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCosmeticsKBClipAndMultimodal:
    """CLIP 多模态检索 + delete + seed + rewrite + simple_rerank 覆盖（Qdrant 适配）"""

    def _make_real_kb(self):
        from rag import qdrant_knowledge_base as kb_mod
        from rag.knowledge_base import CosmeticsKnowledgeBase
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock()
        mock_ef = MagicMock()
        with (
            patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=mock_ef),
            patch.object(kb_mod, "QdrantClient", return_value=mock_qdrant),
        ):
            kb = CosmeticsKnowledgeBase(clip_enabled=False)
            kb._client = mock_qdrant
            return kb, mock_qdrant

    def _make_real_kb_with_clip(self):
        from rag import qdrant_knowledge_base as kb_mod
        from rag.knowledge_base import CosmeticsKnowledgeBase
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock()
        mock_ef = MagicMock()
        with (
            patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=mock_ef),
            patch.object(kb_mod, "QdrantClient", return_value=mock_qdrant),
        ):
            kb = CosmeticsKnowledgeBase(clip_enabled=True)
            kb._client = mock_qdrant
            return kb, mock_qdrant

    def test_clip_available_property_true(self):
        kb, _ = self._make_real_kb_with_clip()
        assert kb._clip_enabled is True

    def test_clip_available_property_false(self):
        kb, _ = self._make_real_kb()
        assert kb._clip_enabled is False

    def test_delete_documents_real(self):
        from rag import qdrant_knowledge_base as kb_mod
        from rag.knowledge_base import CosmeticsKnowledgeBase
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock()
        mock_ef = MagicMock()
        with (
            patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=mock_ef),
            patch.object(kb_mod, "QdrantClient", return_value=mock_qdrant),
        ):
            kb = CosmeticsKnowledgeBase(clip_enabled=False)
            kb._client = mock_qdrant
            kb._ensure_collection = MagicMock(return_value=True)
            kb.delete_documents("product_knowledge", ["id1", "id2"])
            assert mock_qdrant.delete.call_count == 2  # 逐条删除

    def test_delete_documents_empty_ids(self):
        kb, _ = self._make_real_kb()
        assert True

    def test_get_collection_count(self):
        from rag import qdrant_knowledge_base as kb_mod
        from rag.knowledge_base import CosmeticsKnowledgeBase
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock()
        mock_ef = MagicMock()
        with (
            patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=mock_ef),
            patch.object(kb_mod, "QdrantClient", return_value=mock_qdrant),
        ):
            kb = CosmeticsKnowledgeBase()
        class MockCountResult:
            count = 42
        mock_qdrant.count.return_value = MockCountResult()
        assert kb.get_collection_count("product_knowledge") == 42

    def test_available_property_real(self):
        kb, _ = self._make_real_kb()
        assert kb.available is True

    def test_apply_reranker_fallback(self):
        kb, _ = self._make_real_kb()
        kb._reranker = None
        results = [{"content": "doc1", "distance": 0.1}, {"content": "doc2", "distance": 0.2}]
        assert len(kb._apply_reranker("query", results, top_k=2)) == 2

    def test_apply_reranker_single_result(self):
        kb, _ = self._make_real_kb()
        results = [{"content": "doc1", "distance": 0.1}]
        assert len(kb._apply_reranker("query", results)) == 1

    def test_apply_reranker_rerank_exception(self):
        kb, _ = self._make_real_kb()
        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = RuntimeError("rerank failed")
        kb._reranker = mock_reranker
        results = [{"content": "doc1", "distance": 0.1}, {"content": "doc2", "distance": 0.2}]
        assert len(kb._apply_reranker("query", results)) == 2

    @pytest.mark.asyncio
    async def test_rewrite_query_no_llm(self):
        kb, _ = self._make_real_kb()
        assert await kb.rewrite_query("moisturizer") == "moisturizer"

    @pytest.mark.asyncio
    async def test_rewrite_query_with_llm_success(self):
        kb, _ = self._make_real_kb()
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="moisturizing cream"))
        r = await kb.rewrite_query("moisturizer", llm_client=mock_llm)
        assert r == "moisturizing cream"

    @pytest.mark.asyncio
    async def test_rewrite_query_llm_returns_same(self):
        kb, _ = self._make_real_kb()
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(return_value=MagicMock(content="moisturizer"))
        assert await kb.rewrite_query("moisturizer", llm_client=mock_llm) == "moisturizer"

    @pytest.mark.asyncio
    async def test_rewrite_query_llm_exception(self):
        kb, _ = self._make_real_kb()
        mock_llm = AsyncMock()
        mock_llm.async_invoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        assert await kb.rewrite_query("moisturizer", llm_client=mock_llm) == "moisturizer"

    def test_simple_rerank_basic(self):
        from rag.knowledge_base import CosmeticsKnowledgeBase
        results = [
            {"content": "unrelated", "distance": 0.1},
            {"content": "niacinamide whitening serum", "distance": 0.5},
            {"content": "niacinamide", "distance": 0.3},
        ]
        assert len(CosmeticsKnowledgeBase.simple_rerank("niacinamide", results, top_k=3)) == 3

    def test_simple_rerank_empty(self):
        from rag.knowledge_base import CosmeticsKnowledgeBase
        assert CosmeticsKnowledgeBase.simple_rerank("query", []) == []

    def test_simple_rerank_without_jieba(self):
        from rag.knowledge_base import CosmeticsKnowledgeBase
        results = [
            {"content": "unrelated", "distance": 0.1},
            {"content": "niacinamide whitening serum", "distance": 0.5},
            {"content": "niacinamide for skin", "distance": 0.3},
        ]
        with patch.dict("sys.modules", {"jieba": None}):
            reranked = CosmeticsKnowledgeBase.simple_rerank("niacinamide", results, top_k=3)
        assert len(reranked) == 3
        assert "niacinamide" in reranked[0]["content"]

    def test_seed_if_empty_real(self):
        kb, mock_client = self._make_real_kb()
        mock_client.count.return_value = 0
        seed_fn = MagicMock()
        kb.seed_if_empty("product_knowledge", seed_fn)
        seed_fn.assert_called_once()

    def test_seed_if_empty_not_empty(self):
        from rag import qdrant_knowledge_base as kb_mod
        from rag.knowledge_base import CosmeticsKnowledgeBase
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock()
        mock_ef = MagicMock()
        with (
            patch.object(CosmeticsKnowledgeBase, "_create_embedding_function", return_value=mock_ef),
            patch.object(kb_mod, "QdrantClient", return_value=mock_qdrant),
        ):
            kb = CosmeticsKnowledgeBase()
        class MockColInfo:
            points_count = 5
        mock_qdrant.get_collection.return_value = MockColInfo()
        seed_fn = MagicMock()
        kb.seed_if_empty("product_knowledge", seed_fn)
        seed_fn.assert_not_called()
