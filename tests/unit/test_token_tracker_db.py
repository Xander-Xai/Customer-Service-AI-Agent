"""
Token 追踪器 + 依赖注入 + 数据库改进 测试（v5.1）
"""

import pytest

from core.token_tracker import TokenTracker, TokenUsage, get_token_tracker, init_token_tracker


class TestTokenTracker:
    """TokenTracker 核心逻辑测试"""

    @pytest.mark.asyncio
    async def test_record_single_usage(self):
        """记录单次 token 用量"""
        tracker = TokenTracker()
        await tracker.record(
            TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                agent="product_agent",
                model="qwen2.5-7b",
                latency_ms=200.0,
            )
        )
        assert tracker.total_calls == 1
        assert tracker.total_prompt_tokens == 100
        assert tracker.total_completion_tokens == 50
        assert tracker.agent_tokens["product_agent"] == 150
        assert tracker.model_calls["qwen2.5-7b"] == 1

    @pytest.mark.asyncio
    async def test_record_multiple_uses_accumulates(self):
        """多次记录累加"""
        tracker = TokenTracker()
        for _ in range(5):
            await tracker.record(
                TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                    agent="tech_agent",
                    model="qwen2.5-7b",
                    latency_ms=100.0,
                )
            )
        assert tracker.total_calls == 5
        assert tracker.total_prompt_tokens == 500
        assert tracker.agent_tokens["tech_agent"] == 750

    @pytest.mark.asyncio
    async def test_get_summary(self):
        """get_summary 返回正确摘要"""
        tracker = TokenTracker()
        await tracker.record(
            TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                latency_ms=200.0,
            )
        )
        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["total_tokens"] == 150
        assert summary["avg_latency_ms"] == 200.0
        assert "p50_latency_ms" in summary
        assert "p95_latency_ms" in summary

    @pytest.mark.asyncio
    async def test_get_agent_summary(self):
        """get_agent_summary 按 agent 维度统计"""
        tracker = TokenTracker()
        await tracker.record(TokenUsage(total_tokens=100, agent="product_agent", latency_ms=100))
        await tracker.record(TokenUsage(total_tokens=200, agent="tech_agent", latency_ms=200))
        summary = tracker.get_agent_summary()
        assert "product_agent" in summary
        assert "tech_agent" in summary
        assert summary["product_agent"]["calls"] == 1
        assert summary["tech_agent"]["total_tokens"] == 200

    @pytest.mark.asyncio
    async def test_get_model_summary(self):
        """get_model_summary 按 model 维度统计"""
        tracker = TokenTracker()
        await tracker.record(TokenUsage(total_tokens=100, model="qwen2.5-7b"))
        await tracker.record(TokenUsage(total_tokens=200, model="deepseek-v3"))
        summary = tracker.get_model_summary()
        assert "qwen2.5-7b" in summary
        assert "deepseek-v3" in summary

    @pytest.mark.asyncio
    async def test_percentile_calculation(self):
        """延迟百分位计算"""
        tracker = TokenTracker()
        for i in range(100):
            await tracker.record(TokenUsage(latency_ms=float(i * 10)))
        p50 = tracker._percentile(50)
        p95 = tracker._percentile(95)
        assert p50 < p95
        assert p50 == 500.0

    def test_percentile_empty(self):
        """空数据百分位返回 0"""
        tracker = TokenTracker()
        assert tracker._percentile(95) == 0.0

    def test_summary_empty(self):
        """空数据摘要"""
        tracker = TokenTracker()
        summary = tracker.get_summary()
        assert summary["total_calls"] == 0
        assert summary["total_tokens"] == 0

    def test_global_singleton(self):
        """全局单例初始化"""
        tracker = init_token_tracker()
        assert tracker is not None
        assert get_token_tracker() is tracker


class TestDatabaseRollbackProtection:
    """数据库 rollback 保护测试"""

    def test_get_db_rollback_on_exception(self):
        """get_db 在异常时自动 rollback"""
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        with patch("db.database.SessionLocal", return_value=mock_session):
            from db.database import get_db

            gen = get_db()
            next(gen)

            with pytest.raises(ValueError):
                gen.throw(ValueError("test error"))

            mock_session.rollback.assert_called_once()
            mock_session.close.assert_called_once()

    def test_get_db_normal_close(self):
        """get_db 正常结束时关闭 session"""
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        with patch("db.database.SessionLocal", return_value=mock_session):
            from contextlib import suppress

            from db.database import get_db

            gen = get_db()
            next(gen)
            with suppress(StopIteration):
                gen.send(None)

            mock_session.rollback.assert_not_called()
            mock_session.close.assert_called_once()


class TestConfigurationError:
    """ConfigurationError 测试"""

    def test_configuration_error_is_exception(self):
        from core.config import ConfigurationError

        with pytest.raises(ConfigurationError):
            raise ConfigurationError("test config error")

    def test_configuration_error_message(self):
        from core.config import ConfigurationError

        try:
            raise ConfigurationError("API Key 缺失")
        except ConfigurationError as e:
            assert "API Key" in str(e)
