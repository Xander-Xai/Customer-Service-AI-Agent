"""
LLM 调用契约回归测试（issue #46）

背景：`QueryRouter._llm_classify` 以 `async_invoke(messages, timeout=LLM_ROUTER_TIMEOUT)`
调用注入的 LLM。`OpenAICompatibleClient` 接受该 `timeout`，而 DEV_MODE 降级实现
`RuleBasedLLM.async_invoke` 只接受 `(messages, tools)`，于是**每次**调用都抛
`TypeError`，被 router 的 `except Exception` 吞成 `raw_llm_result="llm_error"`。

这些测试把"LLM 调用契约"钉死在 implementation 边界上，而不是靠某个调用方
恰好传了什么参数：

- 每个 LLM 实现都必须接受 `core/protocols.py::LLMProtocol.async_invoke` 声明的
  全部关键字参数（`timeout` / `tools`），签名可绑定同一组调用参数。
- 真实客户端的 `timeout` 必须真的传到 httpx 层（不能只是签名上"有"这个参数）。
- DEV_MODE 降级实现也必须能吃下 router 的调用形态，并返回可解析的响应。
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain_core.messages import HumanMessage

from core.config import LLM_ROUTER_TIMEOUT
from core.protocols import LLMProtocol
from llm.client import OpenAICompatibleClient
from llm.rule_based_llm import RuleBasedLLM

# 协议声明的调用形态：所有实现都必须能用这一组参数被调用（关键字绑定）
_PROTOCOL_KWARGS = {"messages": [], "timeout": LLM_ROUTER_TIMEOUT, "tools": None}

_IMPLS = (OpenAICompatibleClient, RuleBasedLLM)


class TestAsyncInvokeSignature:
    """签名层契约：任何实现都不得缺少协议声明的参数"""

    @pytest.mark.parametrize("impl", _IMPLS, ids=lambda c: c.__name__)
    def test_accepts_protocol_keyword_arguments(self, impl):
        """实现签名必须能绑定 LLMProtocol 声明的调用参数（回归 #46 的 TypeError）"""
        sig = inspect.signature(impl.async_invoke)
        bound = sig.bind(None, **_PROTOCOL_KWARGS)  # None = self 占位
        assert bound.arguments["timeout"] == LLM_ROUTER_TIMEOUT
        assert bound.arguments["tools"] is None

    @pytest.mark.parametrize("impl", _IMPLS, ids=lambda c: c.__name__)
    def test_timeout_is_keyword_callable(self, impl):
        """`timeout` 必须是可用关键字传入的参数名（router 就是这么调的）"""
        params = inspect.signature(impl.async_invoke).parameters
        assert "timeout" in params, f"{impl.__name__}.async_invoke 缺少 timeout 参数"
        assert params["timeout"].default is None

    def test_signature_shape_matches_protocol(self):
        """实现与协议的参数名/顺序一致（不是碰巧能接住某一个调用形态）"""
        proto = inspect.signature(LLMProtocol.async_invoke).parameters
        for impl in _IMPLS:
            params = inspect.signature(impl.async_invoke).parameters
            assert list(params) == list(proto), (
                f"{impl.__name__}.async_invoke 参数列表与 LLMProtocol 不一致"
            )
            for name, p in proto.items():
                assert params[name].default == p.default, (
                    f"{impl.__name__}.{name} 默认值与协议不一致"
                )


class TestRealClientTimeoutIsEffective:
    """真实客户端：`timeout` 必须落到 httpx，而不只是签名里存在"""

    async def test_timeout_reaches_http_layer(self):
        client = OpenAICompatibleClient(
            api_key="test-key", base_url="http://contract-test", model="test-model"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            await client.async_invoke([HumanMessage(content="hi")], timeout=1.5)

        sent_timeout = mock_http.post.call_args.kwargs["timeout"]
        assert isinstance(sent_timeout, httpx.Timeout)
        assert sent_timeout == httpx.Timeout(1.5)

    async def test_timeout_accepts_router_timeout_positionally(self):
        """位置传参（real client 已支持）——与降级实现保持同一形态"""
        client = OpenAICompatibleClient(
            api_key="test-key", base_url="http://contract-test-2", model="test-model"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch.object(client, "_get_async_client", return_value=mock_http):
            await client.async_invoke([HumanMessage(content="hi")], 2.0)

        assert mock_http.post.call_args.kwargs["timeout"] == httpx.Timeout(2.0)


class TestRuleBasedLLMCallContract:
    """降级实现：必须能吃下 router 的调用形态并返回响应对象"""

    async def test_async_invoke_accepts_timeout_keyword(self):
        llm = RuleBasedLLM()
        resp = await llm.async_invoke(
            [SystemMsg("你是查询分类专家"), HumanMsg("你好")], timeout=LLM_ROUTER_TIMEOUT
        )
        assert isinstance(resp.content, str)
        assert resp.content
        assert resp.tool_calls == []

    async def test_async_invoke_accepts_tools_keyword(self):
        llm = RuleBasedLLM()
        resp = await llm.async_invoke(
            [HumanMsg("你好")],
            timeout=None,
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
        assert isinstance(resp.content, str)

    def test_sync_invoke_accepts_timeout_keyword(self):
        llm = RuleBasedLLM()
        resp = llm.invoke([HumanMsg("你好")], timeout=LLM_ROUTER_TIMEOUT)
        assert isinstance(resp.content, str)

    async def test_response_exposes_consumed_attributes(self):
        """响应对象必须暴露消费方读取的属性（router/base_agent 读 .content）"""
        llm = RuleBasedLLM()
        resp = await llm.async_invoke([HumanMsg("你好")], timeout=1.0)
        assert isinstance(resp.content, str) and resp.content
        assert isinstance(resp.tool_calls, list)


class TestQueryRouterOnFallbackLLM:
    """回归 #46 的可观测后果：fallback 模式下 router 必须真的拿到分类结果"""

    async def test_router_route_with_rule_based_llm(self):
        """不命中规则捷径的 query：必须走完 LLM 分支且不再是 llm_error"""
        from router.query_router import INTENT_AGENT_MAP, QueryRouter

        router = QueryRouter(llm=RuleBasedLLM())
        result = await router.route("那个东西怎么弄啊")

        assert result.raw_llm_result != "llm_error"
        assert result.raw_llm_result, "fallback LLM 必须返回非空结果"
        assert result.query_type in INTENT_AGENT_MAP
        assert result.confidence > 0.1, "llm_error 兜底置信度是 0.1，说明没走到 fallback 结果"

    async def test_llm_classify_returns_dict_from_fallback(self):
        """_llm_classify 契约：永远返回 dict，且 raw 携带 fallback 输出"""
        from router.query_router import QueryRouter

        router = QueryRouter(llm=RuleBasedLLM())
        out = await router._llm_classify("你好")
        assert isinstance(out, dict)
        assert set(out) >= {"query_type", "confidence", "raw"}
        assert out["raw"] != "llm_error"


class TestFallbackInitWarning:
    """DEV_MODE 降级初始化的告警文案必须指向真实 provider（不得硬编码厂商名/行号）"""

    async def test_warning_names_the_configured_provider(self, monkeypatch):
        import logging

        import core.config as config
        from core.container import ServiceContainer

        monkeypatch.setattr(config, "DEV_MODE", True, raising=False)
        monkeypatch.setattr(config, "OPENAI_API_KEY", "test-mock-key", raising=False)
        monkeypatch.setattr(config, "LLM_PROVIDER", "siliconflow", raising=False)

        container = ServiceContainer()
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        target = logging.getLogger("core.container")
        handler = Capture()
        target.addHandler(handler)
        try:
            await container._init_llm()
        finally:
            target.removeHandler(handler)

        assert isinstance(container.llm, RuleBasedLLM)
        warning_text = "\n".join(records)
        assert "siliconflow" in warning_text, "降级告警必须包含实际 LLM_PROVIDER"
        assert "DeepSeek" not in warning_text, "降级告警不得硬编码其它厂商名"
        assert ".env.dev" not in warning_text, "告警不得声称某个文件的具体行号"

    async def test_fallback_llm_is_router_compatible(self, monkeypatch):
        """容器选出的降级 LLM 必须能被 router 直接使用（#46 的端到端边界）"""
        import core.config as config
        from core.container import ServiceContainer
        from router.query_router import QueryRouter

        monkeypatch.setattr(config, "DEV_MODE", True, raising=False)
        monkeypatch.setattr(config, "OPENAI_API_KEY", "test-mock-key", raising=False)

        container = ServiceContainer()
        await container._init_llm()
        assert isinstance(container.llm, RuleBasedLLM)

        router = QueryRouter(llm=container.llm)
        result = await router.route("那个东西怎么弄啊")
        assert result.raw_llm_result != "llm_error"


class SystemMsg:
    """最小 SystemMessage 替身（RuleBasedLLM 按类名识别消息类型）"""

    def __init__(self, content: str):
        self.content = content


class HumanMsg:
    """最小 HumanMessage 替身"""

    def __init__(self, content: str):
        self.content = content
