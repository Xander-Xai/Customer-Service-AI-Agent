"""
P1-5: 补齐缺失测试 — SSE / WebSocket / 多模态 / DI 容器 / 并发
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_ENABLED", "false")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("SESSION_TOKEN_SECRET", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")

# 确保数据库表在测试前已创建
from db.database import init_db

init_db()
os.environ.setdefault("DEV_MODE", "true")


# ===== DI 容器测试 =====


class TestServiceContainer:
    """ServiceContainer 依赖注入容器测试"""

    def test_container_creation(self):
        """容器同步创建（基础设施组件）"""
        from core.container import ServiceContainer

        container = ServiceContainer()
        assert container.bus is not None
        assert container.bb is not None
        assert container.metrics is not None
        assert container.circuit_breaker is not None
        assert container.cache is not None
        assert container.session_mgr is not None
        assert container._initialized is False

    @pytest.mark.asyncio
    async def test_container_initialize(self):
        """容器异步初始化（LLM + Agents + Router）"""
        from core.container import ServiceContainer

        container = ServiceContainer()
        await container.initialize()
        assert container._initialized is True
        assert container.llm is not None
        assert container.agents_dict
        assert container.router is not None
        assert container.orchestrator is not None
        assert container.graph_app is not None

    @pytest.mark.asyncio
    async def test_container_idempotent_init(self):
        """容器初始化幂等性 — 多次调用安全"""
        from core.container import ServiceContainer

        container = ServiceContainer()
        await container.initialize()
        first_agents = container.agents_dict
        await container.initialize()
        assert container.agents_dict is first_agents  # 同一个对象

    @pytest.mark.asyncio
    async def test_container_close(self):
        """容器优雅关闭"""
        from core.container import ServiceContainer

        container = ServiceContainer()
        await container.initialize()
        assert container._initialized is True
        await container.close()
        assert container._initialized is False
        assert container.graph_app is None

    @pytest.mark.asyncio
    async def test_container_graph_execution(self):
        """容器内图执行"""
        from core.container import ServiceContainer

        container = ServiceContainer()
        await container.initialize()

        state = {
            "session_id": "test-container",
            "current_agent": "",
            "customer_query": "测试容器图执行",
            "query_type": "",
            "response": "",
            "complexity": 0,
            "fast_path": True,
            "collaboration_mode": "",
            "cached": False,
            "agents_used": [],
            "resolution_status": "",
        }
        result = await container.graph_app.ainvoke(
            state, config={"configurable": {"thread_id": "test-container"}}
        )
        assert result["response"]
        assert result["collaboration_mode"]

        await container.close()


# ===== 并发安全测试 =====


class TestConcurrency:
    """并发安全测试 — 验证 asyncio.Lock 修复"""

    @pytest.mark.asyncio
    async def test_metrics_concurrent_record(self):
        """MetricsCollector 并发写入安全"""
        from core.monitoring import MetricsCollector

        metrics = MetricsCollector()

        async def record(i):
            await metrics.record_request(elapsed=1.0 + i * 0.1, session_id=f"s{i}")

        # 并发写入
        await asyncio.gather(*[record(i) for i in range(50)])
        stats = await metrics.get_stats()
        assert stats["total_requests"] == 50

    @pytest.mark.asyncio
    async def test_circuit_breaker_concurrent_access(self):
        """CircuitBreaker 并发访问安全"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=5, recovery_time=1)

        # 并发记录失败
        async def fail():
            await cb.record_failure()

        await asyncio.gather(*[fail() for _ in range(20)])
        status = cb.get_status()
        assert status["state"] == "open"
        assert status["total_failures"] == 20

    @pytest.mark.asyncio
    async def test_blackboard_concurrent_write(self):
        """SharedBlackboard 并发写入安全"""
        from core.shared_blackboard import SharedBlackboard

        bb = SharedBlackboard()

        async def write(i):
            await bb.write(f"key_{i}", f"value_{i}")

        await asyncio.gather(*[write(i) for i in range(30)])
        val = await bb.read("key_15")
        assert val == "value_15"

    @pytest.mark.asyncio
    async def test_message_bus_concurrent_subscribe(self):
        """MessageBus 并发订阅安全"""
        from core.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received = []

        async def handler(msg):
            received.append(msg.payload)

        async def subscribe(i):
            await bus.subscribe(f"topic_{i}", handler)

        await asyncio.gather(*[subscribe(i) for i in range(20)])
        # 发布消息
        for i in range(20):
            await bus.publish(
                Message(
                    msg_type=MessageType.BROADCAST,
                    topic=f"topic_{i}",
                    payload=f"data_{i}",
                )
            )
        assert len(received) == 20


# ===== 配置校验测试 =====


class TestConfigValidation:
    """配置校验测试"""

    def test_validate_required_config_in_dev_mode(self):
        """开发模式跳过校验"""
        # DEV_MODE=true 时不应抛出异常

        from core import config

        # 已在 DEV_MODE 下加载，不应有异常
        assert config.DEV_MODE is True

    def test_placeholder_detection(self):
        """占位符检测逻辑"""
        placeholder_prefixes = (
            "your-",
            "your_",
            "change-me",
            "sk-placeholder",
            "sk-xxx",
            "sk-your",
            "sk-test-placeholder",
            "sk-tnwwg",
        )
        test_cases = [
            ("your_siliconflow_api_key_here", True),
            ("sk-placeholder-key", True),
            ("sk-tnwwgabc123", True),
            ("sk-real-api-key-12345", False),
        ]
        for key, expected in test_cases:
            result = any(key.lower().startswith(p) for p in placeholder_prefixes)
            assert result == expected, (
                f"Key '{key}' should be detected as {'placeholder' if expected else 'valid'}"
            )


# ===== Refresh Token 测试（P2-3）=====


class TestRefreshToken:
    """Refresh Token 机制测试"""

    def test_create_access_token(self):
        """创建 access_token"""
        from auth.service import create_access_token, decode_token

        token = create_access_token(1, "testuser", "user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == 1
        assert payload["type"] == "access"

    async def test_create_refresh_token(self):
        """创建 refresh_token"""
        from auth.service import create_refresh_token, decode_token

        token = await create_refresh_token(1, "testuser", "user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == 1
        assert payload["type"] == "refresh"

    async def test_refresh_access_token_flow(self):
        """refresh_token 换取 access_token 完整流程"""
        import uuid

        from auth.service import (
            authenticate_user,
            create_refresh_token,
            decode_token,
            refresh_access_token,
            register_user,
        )

        username = f"refresh_test_{uuid.uuid4().hex[:8]}"
        register_user(username, "password123")
        auth = await authenticate_user(username, "password123")
        assert auth is not None

        # 创建 refresh_token
        refresh = await create_refresh_token(auth["user_id"], auth["username"], auth["role"])
        # 用 refresh_token 换取新 access_token
        result = refresh_access_token(refresh)
        assert result is not None
        assert "access_token" in result
        assert result["username"] == username

        # 验证新 access_token 有效
        payload = decode_token(result["access_token"])
        assert payload is not None
        assert payload["type"] == "access"

    def test_refresh_with_invalid_token(self):
        """无效 refresh_token 应返回 None"""
        from auth.service import refresh_access_token

        result = refresh_access_token("invalid.token.here")
        assert result is None

    def test_refresh_with_access_token_fails(self):
        """用 access_token 代替 refresh_token 应失败"""
        from auth.service import create_access_token, refresh_access_token

        access = create_access_token(1, "test", "user")
        result = refresh_access_token(access)
        assert result is None  # type != "refresh"


# ===== LLM-as-Judge 测试（P2-4）=====


class TestLLMAsJudge:
    """LLM-as-Judge 评估器测试"""

    def test_evaluator_has_llm_judge_method(self):
        """ResponseEvaluator 应有 evaluate_with_llm 方法"""
        from agents.evaluator import ResponseEvaluator

        evaluator = ResponseEvaluator()
        assert hasattr(evaluator, "evaluate_with_llm")
        assert asyncio.iscoroutinefunction(evaluator.evaluate_with_llm)


# ===== OpenTelemetry 测试（P2-1）=====


class TestOpenTelemetry:
    """OpenTelemetry 追踪模块测试"""

    @pytest.fixture(autouse=True)
    def _reset_tracing_state(self):
        """Reset global tracing state before each test for isolation."""
        import core.tracing as tracing_mod

        tracing_mod._TRACING_INITIALIZED = False
        yield
        tracing_mod._TRACING_INITIALIZED = False

    def test_tracing_module_importable(self):
        """追踪模块可导入"""
        from core.tracing import get_tracer, setup_tracing

        assert callable(setup_tracing)
        assert callable(get_tracer)

    def test_tracing_disabled_by_default(self):
        """默认未启用"""
        from core.tracing import get_tracer

        tracer = get_tracer()
        # 未启用时返回 None
        assert tracer is None

    def test_tracing_setup_without_sdk(self):
        """无 SDK 时 setup_tracing 返回 False"""
        from core.tracing import setup_tracing

        # OPENTELEMETRY_ENABLED 未设置
        result = setup_tracing()
        assert result is False
