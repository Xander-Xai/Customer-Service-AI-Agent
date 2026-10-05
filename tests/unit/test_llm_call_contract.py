"""LLM 调用契约回归测试（issue #46）

背景：`QueryRouter._llm_classify` 按 `async_invoke(messages, timeout=LLM_ROUTER_TIMEOUT)`
调用注入的 LLM。这个形态对 `core/protocols.LLMProtocol` 和
`OpenAICompatibleClient` 成立，但开发降级实现 `RuleBasedLLM` 只声明了
`async_invoke(self, messages, tools=None)`，于是**每一次**调用都抛
`TypeError: unexpected keyword argument 'timeout'`，再被 router 的
`except Exception` 吞成 `raw_llm_result="llm_error"`。

关键点不是「多传了一个参数」，而是**兜底路径自己是坏的**：API Key 缺失或无效时
（`core/container.py::_init_llm` 选出 `RuleBasedLLM`，而 `.env.example` 出厂值就是
占位符），LLM 路由不是降级为规则回答，而是降级为「路由失败」。

这些测试把契约钉在 implementation 边界上，而不是依赖某个调用方恰好传了什么：

- 真实客户端与降级实现必须都能绑定**同一组**调用参数（`llm_call_contract_mismatches`
  是纯函数比对，构造不出实例的实现同样能被检查）。
- 真实客户端的 `timeout` 必须真的落到 httpx 层，而不只是签名上「有」这个参数。
- 降级实现必须能吃下 router 的调用形态，返回消费方（router / base_agent）真正读取的属性。
- 接受 `timeout` / `tools` **不得**改变规则引擎的业务回答语义 —— #46 是契约修复，
  不是行为变更。
- 降级模式下 router 必须真的拿到分类结果，而不是 `llm_error`。
"""

import asyncio
import contextlib
import inspect
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from core.config import LLM_ROUTER_TIMEOUT
from core.container import logger as _container_logger
from core.protocols import (
    LLM_CALL_CONTRACT_PARAMS,
    LLMProtocol,
    llm_call_contract_mismatches,
)
from llm.client import OpenAICompatibleClient
from llm.rule_based_llm import RuleBasedLLM

# 两个必须同签名共存于 self.llm 这个槽位的实现。
_IMPLS = (OpenAICompatibleClient, RuleBasedLLM)
_IMPL_IDS = [c.__name__ for c in _IMPLS]

# 覆盖规则引擎全部分支的 (system_prompt, query) 样本
_RULE_CASES = [
    ("你是查询分类专家 product_info", "这个面霜的成分是什么"),
    ("你是账单助手", "我的订单什么时候发货"),
    ("你是技术支持", "精华液怎么用"),
    ("你是投诉处理", "用了之后过敏了"),
    ("", "随便聊聊"),
]


def _messages(system_prompt: str, query: str) -> list:
    """构造 `RuleBasedLLM` 能识别的消息序列。

    `RuleBasedLLM.async_invoke` 按 `type(msg).__name__` 识别 HumanMessage /
    SystemMessage，所以必须用真实的 LangChain 消息类：替身类会静默走进 general
    模板，让断言失去意义。
    """
    return [SystemMessage(content=system_prompt), HumanMessage(content=query)]


class TestCallContractSignature:
    """签名层契约：任何实现都不得缺少协议声明的参数（#46 的 TypeError 根因）"""

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_no_contract_mismatch(self, impl):
        assert llm_call_contract_mismatches(impl) == []

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_accepts_protocol_keyword_arguments(self, impl):
        """实现签名必须能绑定 `LLMProtocol` 声明的全部关键字参数"""
        sig = inspect.signature(impl.async_invoke)
        bound = sig.bind(None, messages=[], timeout=LLM_ROUTER_TIMEOUT, tools=None)
        assert bound.arguments["timeout"] == LLM_ROUTER_TIMEOUT
        assert bound.arguments["tools"] is None

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_timeout_is_keyword_callable(self, impl):
        """`timeout` 必须是可用关键字传入的参数名（router 就是这么调的）"""
        params = inspect.signature(impl.async_invoke).parameters
        assert "timeout" in params, f"{impl.__name__}.async_invoke 缺少 timeout 参数"
        assert params["timeout"].default is None

    def test_contract_constant_matches_protocol(self):
        """契约常量本身不得与 Protocol 漂移 —— 它是两端共用的判据"""
        proto = tuple(inspect.signature(LLMProtocol.async_invoke).parameters)
        assert proto == ("self", *LLM_CALL_CONTRACT_PARAMS)

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_signature_shape_matches_protocol(self, impl):
        """与协议的参数名/顺序/默认值一致（不是碰巧能接住某一个调用形态）"""
        proto = inspect.signature(LLMProtocol.async_invoke).parameters
        params = inspect.signature(impl.async_invoke).parameters
        assert list(params) == list(proto), (
            f"{impl.__name__}.async_invoke 参数列表与 LLMProtocol 不一致"
        )
        for name, ref in proto.items():
            assert params[name].default == ref.default, f"{impl.__name__}.{name} 默认值与协议不一致"

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_kwargs_cannot_launder_a_drifted_signature(self, impl):
        """禁止用 `**kwargs` 兜住契约漂移。

        `**kwargs` 会把「调用方传错了参数」变成静默接受，正是 #46 之所以能以
        `TypeError` 形式暴露、而一旦被 kwargs 吞掉就会变成永久静默的原因。
        """
        params = inspect.signature(impl.async_invoke).parameters
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            f"{impl.__name__}.async_invoke 不得用 **kwargs 接受契约外参数"
        )

    @pytest.mark.parametrize("impl", _IMPLS, ids=_IMPL_IDS)
    def test_both_impls_satisfy_runtime_protocol(self, impl):
        """两端都必须满足 LLMProtocol（container 把它们装进同一个 `self.llm` 槽位）"""
        assert issubclass(impl, LLMProtocol)

    def test_missing_method_is_reported_not_raised(self):
        """`llm_call_contract_mismatches` 是纯函数：缺失方法返回描述，不抛异常"""
        assert llm_call_contract_mismatches(object) == ["async_invoke 缺失"]

    def test_detector_rejects_the_pre_issue46_signature(self):
        """检测器自身必须能识别 #46 那个真实漂移签名。

        没有这一条，把 `llm_call_contract_mismatches` 改成永远返回 `[]`
        （或 `if False`）会让上面所有签名测试照常通过 —— 检测器被掏空是
        「测试全绿但回归可重新引入」的典型路径，必须在这里钉住。
        """

        class _PreIssue46LLM:
            async def async_invoke(self, messages, tools=None): ...

        class _KwargsLaunderedLLM:
            async def async_invoke(self, messages, timeout=None, tools=None, **kwargs): ...

        assert llm_call_contract_mismatches(_PreIssue46LLM) != []
        assert llm_call_contract_mismatches(_KwargsLaunderedLLM) != []

    def test_fallback_invoke_mirrors_its_own_async_invoke(self):
        """降级实现的同步入口不得与自己的异步入口漂移（否则 timeout 只在一半路径可用）"""
        async_params = inspect.signature(RuleBasedLLM.async_invoke).parameters
        sync_params = inspect.signature(RuleBasedLLM.invoke).parameters
        assert list(sync_params) == list(async_params), (
            "RuleBasedLLM.invoke 与 async_invoke 参数不一致"
        )


class TestRealClientTimeoutIsEffective:
    """真实客户端：`timeout` 必须落到 httpx，而不只是签名里存在"""

    @staticmethod
    def _mock_http(content: str = "ok") -> AsyncMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        resp.raise_for_status = MagicMock()
        http = AsyncMock()
        http.post = AsyncMock(return_value=resp)
        return http

    async def test_timeout_reaches_http_layer(self):
        client = OpenAICompatibleClient(
            api_key="contract-test-key", base_url="http://contract-test", model="test-model"
        )
        http = self._mock_http()
        with patch.object(client, "_get_async_client", return_value=http):
            await client.async_invoke([HumanMessage(content="hi")], timeout=1.5)

        sent = http.post.call_args.kwargs["timeout"]
        assert isinstance(sent, httpx.Timeout)
        assert sent == httpx.Timeout(1.5)

    async def test_timeout_accepts_positional_argument(self):
        """位置传参也成立 —— 与降级实现保持同一形态"""
        client = OpenAICompatibleClient(
            api_key="contract-test-key", base_url="http://contract-test-2", model="test-model"
        )
        http = self._mock_http()
        with patch.object(client, "_get_async_client", return_value=http):
            await client.async_invoke([HumanMessage(content="hi")], 2.0)

        assert http.post.call_args.kwargs["timeout"] == httpx.Timeout(2.0)


class TestRuleBasedLLMCallContract:
    """降级实现：必须能吃下 router 的调用形态并返回响应对象"""

    async def test_async_invoke_accepts_timeout_keyword(self):
        """#46 的直接回归：这一行此前抛 TypeError"""
        resp = await RuleBasedLLM().async_invoke(
            _messages("你是查询分类专家", "你好"), timeout=LLM_ROUTER_TIMEOUT
        )
        assert isinstance(resp.content, str) and resp.content
        assert resp.tool_calls == []

    async def test_async_invoke_accepts_tools_keyword(self):
        resp = await RuleBasedLLM().async_invoke(
            _messages("你是查询分类专家", "你好"),
            timeout=None,
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
        assert isinstance(resp.content, str) and resp.content

    def test_sync_invoke_accepts_timeout_keyword(self):
        """同步入口：同步上下文中调用（async 上下文里它按设计抛错，见下）"""
        resp = RuleBasedLLM().invoke(
            _messages("你是查询分类专家", "你好"), timeout=LLM_ROUTER_TIMEOUT
        )
        assert isinstance(resp.content, str) and resp.content

    def test_sync_invoke_bridges_to_the_same_answer(self):
        """同步入口只是异步实现的桥：同一输入必须得到同一答案。

        `tools` 特意取非空值：`invoke` 内部把参数转发给 `async_invoke`，一旦
        转发写成位置参数（`self.async_invoke(messages, tools)`），`tools` 就会
        被绑定到 `timeout` 槽位上 —— 而两边都是 None 时这种错绑完全不可见。
        """
        messages = _messages("你是账单助手", "我想查一下订单")
        tools = [{"type": "function", "function": {"name": "lookup_order"}}]
        via_sync = RuleBasedLLM().invoke(messages, timeout=LLM_ROUTER_TIMEOUT, tools=tools)
        via_async = asyncio.run(
            RuleBasedLLM().async_invoke(messages, timeout=LLM_ROUTER_TIMEOUT, tools=tools)
        )
        assert via_sync.content == via_async.content

    def test_sync_invoke_forwards_timeout_and_tools_by_keyword(self):
        """转发必须按关键字且值正确。

        若写成位置参数，`tools` 会落进 `timeout` 槽位、`timeout` 被丢弃，而规则
        引擎两个参数都不用，**回答完全不变** —— 也就是这种错绑在行为上不可见，
        只能在转发点直接观测。
        """
        captured: dict = {}
        llm = RuleBasedLLM()
        original = llm.async_invoke

        async def spy(messages, timeout=None, tools=None):
            captured.update(timeout=timeout, tools=tools)
            return await original(messages, timeout=timeout, tools=tools)

        tools = [{"type": "function", "function": {"name": "lookup_order"}}]
        llm.async_invoke = spy  # type: ignore[method-assign]
        llm.invoke(_messages("你是账单助手", "我想查一下订单"), timeout=1.5, tools=tools)

        assert captured["timeout"] == 1.5
        assert captured["tools"] == tools

    async def test_sync_invoke_inside_async_context_still_refuses(self):
        """既有语义不变：异步上下文中调用同步入口必须报错，不得被本次修复静默改变"""
        with pytest.raises(RuntimeError):
            RuleBasedLLM().invoke(_messages("你是查询分类专家", "你好"))

    async def test_response_exposes_attributes_consumers_read(self):
        """响应对象必须暴露消费方读取的属性（router / base_agent 读 .content 与 .tool_calls）"""
        resp = await RuleBasedLLM().async_invoke(
            _messages("你是查询分类专家", "你好"), timeout=LLM_ROUTER_TIMEOUT
        )
        assert isinstance(resp.content, str) and resp.content
        assert isinstance(resp.tool_calls, list)

    async def test_stream_yields_same_content_as_non_stream(self):
        """`async_invoke_stream`（LLMProtocol 声明的流式方法）必须产出与
        `async_invoke` 相同的内容。

        此前 `RuleBasedLLM` 根本没有这个方法，降级 + 流式请求会抛
        `AttributeError` 并被 `base_agent` 的 `except Exception` 吞成
        `fallback_response` —— 与 #46 同源的「兜底路径自己是坏的」。
        """
        messages = _messages("你是账单助手", "我的订单什么时候发货")
        llm = RuleBasedLLM()
        chunks = [c async for c in llm.async_invoke_stream(messages, timeout=LLM_ROUTER_TIMEOUT)]
        assert "".join(chunks) == (await llm.async_invoke(messages)).content

    def test_stream_signature_matches_protocol(self):
        proto = inspect.signature(LLMProtocol.async_invoke_stream).parameters
        params = inspect.signature(RuleBasedLLM.async_invoke_stream).parameters
        assert list(params) == list(proto)


class TestRuleBasedLLMSemanticsUnchanged:
    """接受 timeout 不得改变规则引擎的业务回答语义（#46 是契约修复，不是行为变更）"""

    @pytest.mark.parametrize("system_prompt,query", _RULE_CASES, ids=lambda v: v[:12])
    async def test_timeout_does_not_change_answer(self, system_prompt, query):
        messages = _messages(system_prompt, query)
        llm = RuleBasedLLM()
        baseline = await llm.async_invoke(messages)
        for timeout in (None, 0.0, 1.5, LLM_ROUTER_TIMEOUT):
            assert (
                await llm.async_invoke(messages, timeout=timeout)
            ).content == baseline.content, f"timeout={timeout} 改变了规则引擎的回答内容"

    @pytest.mark.parametrize("system_prompt,query", _RULE_CASES, ids=lambda v: v[:12])
    async def test_tools_does_not_change_answer(self, system_prompt, query):
        """`tools` 同样只是被接受：规则引擎不发起 Function Calling"""
        messages = _messages(system_prompt, query)
        llm = RuleBasedLLM()
        baseline = await llm.async_invoke(messages)
        with_tools = await llm.async_invoke(messages, tools=[{"type": "function", "function": {}}])
        assert with_tools.content == baseline.content
        assert with_tools.tool_calls == []

    @pytest.mark.parametrize(
        "system_prompt,query,expected",
        [
            ("你是账单助手", "我的订单什么时候发货", "订单/账单"),
            ("你是技术支持", "精华液怎么用", "使用指导"),
            ("你是投诉处理", "用了之后过敏了", "不好的体验"),
            ("", "随便聊聊", "客服助手"),
        ],
    )
    async def test_rule_templates_still_selected(self, system_prompt, query, expected):
        """按 system_prompt 选模板的既有行为未被签名变更影响"""
        resp = await RuleBasedLLM().async_invoke(
            _messages(system_prompt, query), timeout=LLM_ROUTER_TIMEOUT
        )
        assert expected in resp.content

    async def test_timeout_is_not_a_deadline(self):
        """降级实现没有 I/O 与 await 点：`timeout` 只是被接受，不构成打断。

        真正的超时语义只属于 OpenAICompatibleClient（见 TestRealClientTimeoutIsEffective）。
        """
        started = time.monotonic()
        resp = await RuleBasedLLM().async_invoke(
            _messages("你是查询分类专家", "你好"), timeout=0.001
        )
        assert resp.content
        assert time.monotonic() - started < 1.0

    async def test_order_number_extraction_still_works(self):
        """回归保护：既有业务行为（长数字当作订单号追加）不受签名变更影响"""
        resp = await RuleBasedLLM().async_invoke(
            _messages("你是账单助手", "订单号 1234567890123 怎么处理"), timeout=LLM_ROUTER_TIMEOUT
        )
        assert "1234567890123" in resp.content


class TestQueryRouterOnFallbackLLM:
    """#46 的可观测后果：fallback 模式下 router 必须真的拿到分类结果，而不是 llm_error"""

    async def test_route_with_rule_based_llm_is_not_llm_error(self):
        from router.query_router import INTENT_AGENT_MAP, QueryRouter

        router = QueryRouter(llm=RuleBasedLLM())
        # 该 query 命中不到高置信规则模式，会真正走 LLM 分支
        result = await router.route("那个东西怎么弄啊")

        assert result.raw_llm_result != "llm_error", "fallback LLM 仍走的是 TypeError 兜底分支"
        assert result.raw_llm_result, "fallback LLM 必须返回非空结果"
        assert result.query_type in INTENT_AGENT_MAP
        # llm_error 兜底置信度是 0.1；真正拿到 fallback 结果必然高于它
        assert result.confidence > 0.1

    async def test_llm_classify_returns_dict_with_fallback_payload(self):
        from router.query_router import QueryRouter

        out = await QueryRouter(llm=RuleBasedLLM())._llm_classify("你好")
        assert isinstance(out, dict)
        assert set(out) >= {"query_type", "confidence", "raw"}
        assert out["raw"] != "llm_error"

    async def test_llm_classify_does_not_raise(self):
        """直接调用（不经 route 的编排）时也必须返回，而不是抛 TypeError"""
        from router.query_router import QueryRouter

        out = await QueryRouter(llm=RuleBasedLLM())._llm_classify("我想了解这个产品的成分")
        assert out["raw"] not in ("llm_error", "no_llm")

    async def test_router_threads_configured_timeout_to_the_client(self):
        """`timeout` 必须真的从 router 传到 client（#46 要求 timeout 有路可走）"""
        from router.query_router import QueryRouter

        recorded: dict = {}

        class _RecordingLLM:
            async def async_invoke(self, messages, timeout=None, tools=None):
                recorded["timeout"] = timeout
                recorded["tools"] = tools
                return MagicMock(content='{"query_type": "product_info", "confidence": 0.9}')

        result = await QueryRouter(llm=_RecordingLLM()).route("随便聊聊")

        assert recorded["timeout"] == LLM_ROUTER_TIMEOUT
        assert result.raw_llm_result != "llm_error"


class TestContainerFallbackIsRouterCompatible:
    """端到端边界：容器在 DEV_MODE 下选出的降级 LLM 必须能被 router 直接使用"""

    @staticmethod
    async def _dev_fallback_llm(monkeypatch):
        import core.config as config
        from core.container import ServiceContainer

        monkeypatch.setattr(config, "DEV_MODE", True, raising=False)
        monkeypatch.setattr(config, "OPENAI_API_KEY", "test-mock-key", raising=False)

        container = ServiceContainer()
        await container._init_llm()
        assert isinstance(container.llm, RuleBasedLLM)
        return container.llm

    async def test_fallback_llm_binds_router_call_shape(self, monkeypatch):
        """容器选出的降级实现必须能绑定 router 实际使用的调用形态"""
        llm = await self._dev_fallback_llm(monkeypatch)
        # 绑定方法上 inspect.signature 已不含 self，直接按调用方形态绑定
        bound = inspect.signature(llm.async_invoke).bind([], timeout=LLM_ROUTER_TIMEOUT)
        assert bound.arguments["timeout"] == LLM_ROUTER_TIMEOUT

    async def test_fallback_llm_routes_without_error(self, monkeypatch):
        from router.query_router import QueryRouter

        llm = await self._dev_fallback_llm(monkeypatch)
        result = await QueryRouter(llm=llm).route("那个东西怎么弄啊")
        assert result.raw_llm_result != "llm_error"


class TestDegradationWarningNamesRealProvider:
    """#46 的次要问题：降级告警文案漂移（写死供应商名 / 指向不入库的文件行号）"""

    @staticmethod
    @contextlib.contextmanager
    def _capture_container_warnings():
        """直接挂 handler 抓 ``core.container`` 的告警。

        ``core.logger.get_logger`` 设了 ``propagate=False``，所以 pytest 的
        ``caplog``（root handler）看不到这些 record。
        """
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Handler()
        handler.setLevel(logging.WARNING)
        _container_logger.addHandler(handler)
        try:
            yield records
        finally:
            _container_logger.removeHandler(handler)

    @staticmethod
    async def _dev_fallback_warning_text(monkeypatch) -> str:
        import core.config as config
        from core.container import ServiceContainer

        monkeypatch.setattr(config, "DEV_MODE", True, raising=False)
        monkeypatch.setattr(config, "OPENAI_API_KEY", "test-mock-key", raising=False)
        monkeypatch.setattr(config, "LLM_PROVIDER", "siliconflow", raising=False)

        container = ServiceContainer()
        with TestDegradationWarningNamesRealProvider._capture_container_warnings() as records:
            await container._init_llm()
        return "\n".join(r.getMessage() for r in records)

    async def test_warning_reports_configured_provider(self, monkeypatch):
        """告警必须说出**实际配置的** provider，而不是写死某个供应商。

        曾经写的是「DeepSeek API Key 未配置」，而默认 ``LLM_PROVIDER`` 是
        siliconflow —— 默认配置下这条告警直接说错了供应商。
        """
        text = await self._dev_fallback_warning_text(monkeypatch)
        assert "siliconflow" in text
        assert "DeepSeek" not in text

    async def test_warning_points_at_env_var_not_file_line(self, monkeypatch):
        """告警必须指向环境变量名，不得指向某个 gitignore 文件的具体行号"""
        text = await self._dev_fallback_warning_text(monkeypatch)
        assert "OPENAI_API_KEY" in text
        assert ".env.dev" not in text
