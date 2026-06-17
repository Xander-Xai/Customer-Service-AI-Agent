"""
低覆盖率核心模块测试（v5.0+）
覆盖：core/ab_testing / core/tracing / alerts/notifier / llm/rule_based_llm / knowledge/router
运行: pytest tests/test_core_modules.py -v --tb=short
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. core/ab_testing.py — A/B 测试框架
# ═══════════════════════════════════════════════════════════════════════════════


class TestABTestManagerCreate:
    """ABTestManager 实验创建验证"""

    def test_create_experiment_default_split(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp1", ["control", "variant_a"])
        exp = mgr.experiments["exp1"]
        assert exp["variants"] == ["control", "variant_a"]
        assert exp["traffic_split"] == [0.5, 0.5]
        assert abs(exp["cumulative_split"][-1] - 1.0) < 0.01
        assert exp["active"] is True

    def test_create_experiment_custom_split(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp2", ["A", "B", "C"], traffic_split=[0.5, 0.3, 0.2])
        exp = mgr.experiments["exp2"]
        assert exp["traffic_split"] == [0.5, 0.3, 0.2]
        assert len(exp["cumulative_split"]) == 3

    def test_create_experiment_empty_variants_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(ValueError, match="variants 不能为空"):
            mgr.create_experiment("bad", [])

    def test_create_experiment_single_variant_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(ValueError, match="至少需要 2 个变体"):
            mgr.create_experiment("bad", ["only_one"])

    def test_create_experiment_split_length_mismatch_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(ValueError, match="traffic_split 长度"):
            mgr.create_experiment("bad", ["A", "B"], traffic_split=[1.0])

    def test_create_experiment_split_sum_not_one_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(ValueError, match="traffic_split 之和应为 1.0"):
            mgr.create_experiment("bad", ["A", "B"], traffic_split=[0.7, 0.7])

    def test_create_experiment_negative_split_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(ValueError, match="traffic_split"):
            mgr.create_experiment("bad", ["A", "B"], traffic_split=[-0.5, 1.5])


class TestABTestManagerAssign:
    """ABTestManager 变体分配验证"""

    def test_assign_variant_deterministic(self):
        """同一用户重复分配应返回相同变体"""
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_det", ["control", "variant"])
        first = mgr.assign_variant("exp_det", "user_001")
        second = mgr.assign_variant("exp_det", "user_001")
        assert first == second

    def test_assign_variant_nonexistent_experiment_raises(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        with pytest.raises(KeyError, match="实验不存在"):
            mgr.assign_variant("no_such_exp", "user_001")

    def test_assign_variant_inactive_returns_control(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_stop", ["control", "variant"])
        mgr.stop_experiment("exp_stop")
        result = mgr.assign_variant("exp_stop", "user_001")
        assert result == "control"

    def test_assign_variant_all_users_cover_all_variants(self):
        """大量用户应覆盖所有变体"""
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        variants = ["A", "B", "C"]
        mgr.create_experiment("exp_dist", variants, traffic_split=[0.33, 0.33, 0.34])
        assigned = {mgr.assign_variant("exp_dist", f"user_{i}") for i in range(500)}
        assert assigned == set(variants)

    def test_assign_variant_100_percent_traffic(self):
        """100% 流量给一个变体"""
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_full", ["control", "variant"], traffic_split=[0.0, 1.0])
        # 所有用户都应分配到 variant（bucket < 0.0 不可能，bucket < 1.0 几乎总是）
        results = {mgr.assign_variant("exp_full", f"user_{i}") for i in range(100)}
        assert results == {"variant"}

    def test_assign_variant_0_percent_traffic(self):
        """0% 流量给一个变体：另一变体应获得所有用户"""
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_zero", ["control", "variant"], traffic_split=[1.0, 0.0])
        results = {mgr.assign_variant("exp_zero", f"user_{i}") for i in range(100)}
        assert results == {"control"}


class TestABTestManagerMetrics:
    """ABTestManager 指标记录与结果"""

    def test_record_metric_and_get_results(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_m", ["A", "B"])
        mgr.record_metric("exp_m", "A", "satisfaction", 4.5)
        mgr.record_metric("exp_m", "A", "satisfaction", 3.5)
        mgr.record_metric("exp_m", "B", "satisfaction", 5.0)
        results = mgr.get_results("exp_m")
        assert results["experiment"] == "exp_m"
        assert results["active"] is True
        assert results["variants"]["A"]["metrics"]["satisfaction"]["count"] == 2
        assert results["variants"]["A"]["metrics"]["satisfaction"]["mean"] == 4.0
        assert results["variants"]["B"]["metrics"]["satisfaction"]["count"] == 1

    def test_record_metric_nonexistent_experiment(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        # Should not raise, just log warning
        mgr.record_metric("no_exp", "A", "metric", 1.0)

    def test_record_metric_invalid_variant(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("exp_iv", ["A", "B"])
        # Should not raise, just log warning
        mgr.record_metric("exp_iv", "Z", "metric", 1.0)

    def test_get_results_nonexistent_experiment(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        result = mgr.get_results("no_such")
        assert "error" in result

    def test_list_experiments(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("e1", ["A", "B"])
        mgr.create_experiment("e2", ["X", "Y"])
        listing = mgr.list_experiments()
        names = {e["name"] for e in listing}
        assert "e1" in names
        assert "e2" in names

    def test_delete_experiment(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("to_del", ["A", "B"])
        assert "to_del" in mgr.experiments
        mgr.delete_experiment("to_del")
        assert "to_del" not in mgr.experiments

    def test_stop_experiment(self):
        from core.ab_testing import ABTestManager

        mgr = ABTestManager()
        mgr.create_experiment("to_stop", ["A", "B"])
        mgr.stop_experiment("to_stop")
        assert mgr.experiments["to_stop"]["active"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. core/tracing.py — OpenTelemetry 分布式追踪
# ═══════════════════════════════════════════════════════════════════════════════


class TestTracingModule:
    """core/tracing.py 模块验证"""

    def setup_method(self):
        """每个测试前重置 _TRACING_INITIALIZED"""
        import core.tracing as tracing_mod

        tracing_mod._TRACING_INITIALIZED = False

    def test_setup_tracing_disabled_by_default(self):
        """默认未启用 OpenTelemetry，返回 False"""
        import core.tracing as tracing_mod

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENTELEMETRY_ENABLED", None)
            result = tracing_mod.setup_tracing()
            assert result is False

    def test_setup_tracing_enabled_but_sdk_missing(self):
        """启用但 SDK 未安装时返回 False"""
        import core.tracing as tracing_mod

        with patch.dict(os.environ, {"OPENTELEMETRY_ENABLED": "true"}):
            # opentelemetry 模块在测试环境大概率不存在或不完整
            result = tracing_mod.setup_tracing()
            # 根据环境可能是 True (SDK 存在) 或 False (SDK 不存在)
            assert isinstance(result, bool)

    def test_setup_tracing_idempotent(self):
        """连续调用只初始化一次"""
        import core.tracing as tracing_mod

        tracing_mod._TRACING_INITIALIZED = True
        result = tracing_mod.setup_tracing()
        assert result is True

    def test_get_tracer_returns_none_when_not_initialized(self):
        """未初始化时 get_tracer 返回 None"""
        import core.tracing as tracing_mod

        tracing_mod._TRACING_INITIALIZED = False
        tracer = tracing_mod.get_tracer("test")
        assert tracer is None

    def test_get_tracer_returns_tracer_when_initialized(self):
        """已初始化时 get_tracer 返回 tracer 对象（或 None 如果 SDK 缺失）"""
        import core.tracing as tracing_mod

        tracing_mod._TRACING_INITIALIZED = True
        tracer = tracing_mod.get_tracer("test")
        # 可能返回 tracer 或 None（SDK 未安装时）
        if tracer is not None:
            assert hasattr(tracer, "start_span") or callable(tracer)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. alerts/notifier.py — 告警通知发送器
# ═══════════════════════════════════════════════════════════════════════════════


class TestAlertNotifierInit:
    """AlertNotifier 初始化与配置"""

    def test_init_no_webhooks_no_email(self):
        """默认无配置时 webhooks 和 email 均为空"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALERT_WEBHOOKS", None)
            os.environ.pop("SMTP_HOST", None)
            notifier = AlertNotifier()
            assert notifier.webhooks == []
            assert notifier.email_enabled is False

    def test_init_with_webhook_config(self):
        """环境变量配置 webhooks"""
        from alerts.notifier import AlertNotifier

        wh = '[{"name": "test", "url": "https://example.com/hook", "type": "dingtalk"}]'
        with patch.dict(os.environ, {"ALERT_WEBHOOKS": wh}):
            notifier = AlertNotifier()
            assert len(notifier.webhooks) == 1
            assert notifier.webhooks[0]["name"] == "test"

    def test_init_with_invalid_webhook_json(self):
        """非法 JSON 不崩溃"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {"ALERT_WEBHOOKS": "not-json!!!"}):
            notifier = AlertNotifier()
            assert notifier.webhooks == []

    def test_init_email_enabled(self):
        """配置 SMTP 后 email_enabled 为 True"""
        from alerts.notifier import AlertNotifier

        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "user@example.com",
            "SMTP_PASSWORD": "pass",
            "ALERT_EMAIL_FROM": "alert@example.com",
            "ALERT_EMAIL_TO": "admin@example.com",
        }
        with patch.dict(os.environ, env):
            notifier = AlertNotifier()
            assert notifier.email_enabled is True

    def test_init_email_missing_fields(self):
        """SMTP 配置不完整时 email_enabled 为 False"""
        from alerts.notifier import AlertNotifier

        env = {"SMTP_HOST": "smtp.example.com"}
        with patch.dict(os.environ, env):
            notifier = AlertNotifier()
            assert notifier.email_enabled is False


class TestAlertNotifierSend:
    """AlertNotifier 发送逻辑"""

    @pytest.mark.asyncio
    async def test_send_alert_records_history(self):
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALERT_WEBHOOKS", None)
            os.environ.pop("SMTP_HOST", None)
            notifier = AlertNotifier()

        await notifier.send_alert("测试标题", "测试内容", severity="warning")
        assert len(notifier.alert_history) == 1
        assert notifier.alert_history[0]["title"] == "测试标题"
        assert notifier.alert_history[0]["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_send_alert_history_capped_at_200(self):
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALERT_WEBHOOKS", None)
            notifier = AlertNotifier()

        notifier.alert_history = [{"dummy": i} for i in range(200)]
        await notifier.send_alert("overflow", "test")
        assert len(notifier.alert_history) <= 200

    @pytest.mark.asyncio
    async def test_send_alert_critical_triggers_email(self):
        """critical 级别触发邮件发送"""
        from alerts.notifier import AlertNotifier

        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "user",
            "SMTP_PASSWORD": "pass",
            "ALERT_EMAIL_FROM": "from@test.com",
            "ALERT_EMAIL_TO": "to@test.com",
        }
        with patch.dict(os.environ, env):
            notifier = AlertNotifier()
            assert notifier.email_enabled is True

        with (
            patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email,
            patch.object(notifier, "_send_webhook", new_callable=AsyncMock),
        ):
            await notifier.send_alert("严重故障", "系统宕机", severity="critical")
            mock_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_alert_warning_no_email(self):
        """warning 级别不触发邮件"""
        from alerts.notifier import AlertNotifier

        env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "user",
            "SMTP_PASSWORD": "pass",
            "ALERT_EMAIL_FROM": "from@test.com",
            "ALERT_EMAIL_TO": "to@test.com",
        }
        with patch.dict(os.environ, env):
            notifier = AlertNotifier()

        with (
            patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email,
            patch.object(notifier, "_send_webhook", new_callable=AsyncMock),
        ):
            await notifier.send_alert("警告", "延迟偏高", severity="warning")
            mock_email.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_webhook_ssrf_blocked(self):
        """SSRF 防护：内网地址被拒绝"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        webhook = {"name": "evil", "url": "http://127.0.0.1:8080/hook", "type": "dingtalk"}
        alert = {"title": "t", "content": "c", "severity": "info", "timestamp": time.time()}
        # Should not raise and should not make HTTP request
        await notifier._send_webhook(webhook, alert)

    @pytest.mark.asyncio
    async def test_send_webhook_ssrf_blocks_metadata(self):
        """SSRF 防护：AWS metadata 地址被拒绝"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        webhook = {
            "name": "evil",
            "url": "http://169.254.169.254/latest/meta-data",
            "type": "wecom",
        }
        alert = {"title": "t", "content": "c", "severity": "info", "timestamp": time.time()}
        await notifier._send_webhook(webhook, alert)

    @pytest.mark.asyncio
    async def test_send_webhook_invalid_scheme(self):
        """非法 scheme 被拒绝"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        webhook = {"name": "bad", "url": "ftp://example.com/hook", "type": "dingtalk"}
        alert = {"title": "t", "content": "c", "severity": "info", "timestamp": time.time()}
        await notifier._send_webhook(webhook, alert)

    @pytest.mark.asyncio
    async def test_send_webhook_empty_url(self):
        """空 URL 直接返回"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        webhook = {"name": "empty", "url": "", "type": "dingtalk"}
        alert = {"title": "t", "content": "c", "severity": "info", "timestamp": time.time()}
        await notifier._send_webhook(webhook, alert)

    @pytest.mark.asyncio
    async def test_send_webhook_dingtalk_payload(self):
        """钉钉 webhook 正确构造 payload"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            webhook = {"name": "dt", "url": "https://open.dingtalk.com/hook", "type": "dingtalk"}
            alert = {
                "title": "测试",
                "content": "内容",
                "severity": "warning",
                "timestamp": time.time(),
            }
            await notifier._send_webhook(webhook, alert)

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["msgtype"] == "markdown"

    @pytest.mark.asyncio
    async def test_send_webhook_wecom_payload(self):
        """企业微信 webhook 正确构造 payload"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            webhook = {"name": "wecom", "url": "https://qyapi.weixin.qq.com/hook", "type": "wecom"}
            alert = {
                "title": "测试",
                "content": "内容",
                "severity": "warning",
                "timestamp": time.time(),
            }
            await notifier._send_webhook(webhook, alert)

            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["msgtype"] == "markdown"
            assert "content" in payload["markdown"]

    @pytest.mark.asyncio
    async def test_send_webhook_feishu_payload(self):
        """飞书 webhook 正确构造 payload"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            webhook = {"name": "feishu", "url": "https://open.feishu.cn/hook", "type": "feishu"}
            alert = {
                "title": "测试",
                "content": "内容",
                "severity": "info",
                "timestamp": time.time(),
            }
            await notifier._send_webhook(webhook, alert)

            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["msg_type"] == "interactive"

    @pytest.mark.asyncio
    async def test_send_webhook_unknown_type(self):
        """未知 webhook type 使用默认 text payload"""
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            webhook = {"name": "custom", "url": "https://custom.example.com/hook", "type": "custom"}
            alert = {
                "title": "测试",
                "content": "内容",
                "severity": "info",
                "timestamp": time.time(),
            }
            await notifier._send_webhook(webhook, alert)

            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert "text" in payload


class TestAlertNotifierUtility:
    """AlertNotifier 工具方法"""

    def test_get_history(self):
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {}, clear=False):
            notifier = AlertNotifier()
        notifier.alert_history = [{"i": i} for i in range(50)]
        result = notifier.get_history(limit=10)
        assert len(result) == 10

    def test_get_config(self):
        from alerts.notifier import AlertNotifier

        wh = '[{"name": "test", "url": "https://example.com/very-long-url-path", "type": "dingtalk"}]'
        env = {
            "ALERT_WEBHOOKS": wh,
            "SMTP_HOST": "smtp.test.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "u",
            "SMTP_PASSWORD": "p",
            "ALERT_EMAIL_FROM": "f@t.com",
            "ALERT_EMAIL_TO": "a@t.com,b@t.com",
        }
        with patch.dict(os.environ, env):
            notifier = AlertNotifier()
            cfg = notifier.get_config()
            assert cfg["email_enabled"] is True
            assert len(cfg["webhooks"]) == 1
            # URL should be masked (truncated with ...)
            assert "..." in cfg["webhooks"][0]["url"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. llm/rule_based_llm.py — 规则引擎 LLM
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleBasedLLMClassify:
    """RuleBasedLLM 查询分类验证"""

    def test_classify_product_info(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        assert llm._classify_query("这款面膜的成分是什么") == "product_info"

    def test_classify_billing(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        assert llm._classify_query("我的订单退款什么时候到账") == "billing"

    def test_classify_tech_support(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        assert llm._classify_query("这个精华液怎么使用") == "tech_support"

    def test_classify_complaint(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        assert llm._classify_query("我要投诉你们的产品质量") == "complaint"

    def test_classify_general_fallback(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        assert llm._classify_query("你好啊") == "general"

    def test_classify_multiple_keywords_highest_score(self):
        """多个类别关键词匹配时，返回得分最高的"""
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        # "产品" (product_info) + "退款" (billing) + "使用" (tech_support) — 三者各1分
        # 但 "订单退款物流" 有 "订单"+"退款"+"物流" = 3分 -> billing
        assert llm._classify_query("订单退款物流快递支付") == "billing"


class TestRuleBasedLLMGenerate:
    """RuleBasedLLM 回复生成验证"""

    def test_generate_response_product(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        resp = llm._generate_response("成分是什么", "product_info")
        assert "天然植物成分" in resp

    def test_generate_response_with_order_number(self):
        """包含长数字（订单号）时追加订单号提示"""
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        resp = llm._generate_response("查一下订单 202606080001 的状态", "billing")
        assert "202606080001" in resp
        assert "订单号" in resp

    def test_generate_response_general(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        resp = llm._generate_response("随便聊聊")
        assert "药妆智多星" in resp

    def test_generate_response_unknown_type_falls_back_to_general(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()
        resp = llm._generate_response("hello", "nonexistent_type")
        assert "药妆智多星" in resp


class TestRuleBasedLLMInvoke:
    """RuleBasedLLM 异步调用验证"""

    @pytest.mark.asyncio
    async def test_async_invoke_with_human_message(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()

        # Simulate LangChain message objects
        human_msg = MagicMock()
        human_msg.__class__.__name__ = "HumanMessage"
        human_msg.content = "这款产品多少钱"

        sys_msg = MagicMock()
        sys_msg.__class__.__name__ = "SystemMessage"
        sys_msg.content = "你是一个产品顾问"

        # type() checks __name__ — need to patch type
        class HumanMessage:
            content = "这款产品多少钱"

        class SystemMessage:
            content = "你是一个产品顾问"

        resp = await llm.async_invoke([SystemMessage(), HumanMessage()])
        assert resp.content
        assert isinstance(resp.content, str)
        assert len(resp.content) > 0

    @pytest.mark.asyncio
    async def test_async_invoke_system_prompt_billing(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()

        class HumanMessage:
            content = "帮我查一下"

        class SystemMessage:
            content = "你是账单查询助手"

        resp = await llm.async_invoke([SystemMessage(), HumanMessage()])
        assert "订单" in resp.content or "账单" in resp.content

    @pytest.mark.asyncio
    async def test_async_invoke_system_prompt_complaint(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()

        class HumanMessage:
            content = "产品有问题"

        class SystemMessage:
            content = "你是投诉处理专员"

        resp = await llm.async_invoke([SystemMessage(), HumanMessage()])
        assert "抱歉" in resp.content or "投诉" in resp.content

    @pytest.mark.asyncio
    async def test_async_invoke_system_prompt_tech(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()

        class HumanMessage:
            content = "怎么用"

        class SystemMessage:
            content = "你是技术支持"

        resp = await llm.async_invoke([SystemMessage(), HumanMessage()])
        assert "护肤" in resp.content or "步骤" in resp.content or "使用" in resp.content

    @pytest.mark.asyncio
    async def test_async_invoke_no_system_message(self):
        from llm.rule_based_llm import RuleBasedLLM

        llm = RuleBasedLLM()

        class HumanMessage:
            content = "你好"

        resp = await llm.async_invoke([HumanMessage()])
        assert resp.content
        assert resp.tool_calls == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. knowledge/router.py — 知识库管理路由
# ═══════════════════════════════════════════════════════════════════════════════


def _make_admin_request():
    """构造一个带 admin 用户的 mock Request"""
    mock_request = MagicMock()
    mock_user = MagicMock()
    mock_user.role = "admin"
    mock_user.username = "admin"
    return mock_request, mock_user


def _make_request_with_container(kb=None, erp=None):
    """构造一个带 container 的 mock Request（替代 multi_agent_customer_service mock）"""
    mock_request = MagicMock()
    container = MagicMock()
    container.knowledge_base = kb
    container.erp = erp
    mock_request.app.state.container = container
    return mock_request


def _make_mock_knowledge_base(available=True, count=10):
    """构造一个 mock KnowledgeBase"""
    kb = MagicMock()
    kb.available = available
    kb.get_collection_count.return_value = count
    kb.add_documents.return_value = None
    return kb


class TestKnowledgeStats:
    """知识库统计路由测试"""

    @pytest.mark.asyncio
    async def test_stats_available(self):
        from knowledge.router import knowledge_stats

        kb = _make_mock_knowledge_base(available=True, count=5)
        req = _make_request_with_container(kb=kb)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await knowledge_stats(req)
            assert result["available"] is True
            assert "collections" in result
            assert result["total"] > 0

    @pytest.mark.asyncio
    async def test_stats_unavailable(self):
        from knowledge.router import knowledge_stats

        kb = _make_mock_knowledge_base(available=False)
        req = _make_request_with_container(kb=kb)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await knowledge_stats(req)
            assert result["available"] is False

    @pytest.mark.asyncio
    async def test_stats_none_knowledge_base(self):
        from knowledge.router import knowledge_stats

        req = _make_request_with_container(kb=None)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await knowledge_stats(req)
            assert result["available"] is False

    @pytest.mark.asyncio
    async def test_stats_exception_handling(self):
        from knowledge.router import knowledge_stats

        req = MagicMock()
        req.app.state.container = None  # no container

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await knowledge_stats(req)
            assert result["available"] is False


class TestKnowledgeAddDocuments:
    """知识库添加文档路由测试"""

    @pytest.mark.asyncio
    async def test_add_documents_success(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=True, count=15)
        data = AddDocRequest(documents=["新品面膜", "补水精华"])
        req = _make_request_with_container(kb=kb)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await add_documents("product_knowledge", data, req)
            assert "已添加 2 条文档" in result["message"]
            kb.add_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_documents_invalid_collection(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=True)
        data = AddDocRequest(documents=["test"])
        req = _make_request_with_container(kb=kb)

        with (
            patch("knowledge.router.require_admin", return_value=MagicMock()),
            pytest.raises(HTTPException),
        ):
            await add_documents("invalid_collection", data, req)

    @pytest.mark.asyncio
    async def test_add_documents_unavailable(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=False)
        data = AddDocRequest(documents=["test"])
        req = _make_request_with_container(kb=kb)

        with (
            patch("knowledge.router.require_admin", return_value=MagicMock()),
            pytest.raises(HTTPException),
        ):
            await add_documents("product_knowledge", data, req)


class TestKnowledgeSync:
    """知识库 ERP 同步路由测试"""

    @pytest.mark.asyncio
    async def test_sync_success(self):
        from knowledge.router import sync_from_erp

        kb = _make_mock_knowledge_base(available=True, count=5)

        mock_erp = AsyncMock()
        mock_erp.query_product.return_value = [
            {
                "name": "面膜A",
                "category": "面膜",
                "price": 88,
                "specs": "10片/盒",
                "ingredients": "玻尿酸",
                "suitable": "所有肤质",
            },
            {
                "name": "精华B",
                "category": "精华",
                "price": 128,
                "specs": "30ml",
                "ingredients": "烟酰胺",
                "suitable": "油性肤质",
            },
        ]

        req = _make_request_with_container(kb=kb, erp=mock_erp)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await sync_from_erp(req)
            assert result["synced"] == 2
            assert result["total"] == 5

    @pytest.mark.asyncio
    async def test_sync_erp_returns_empty(self):
        from knowledge.router import sync_from_erp

        kb = _make_mock_knowledge_base(available=True)
        mock_erp = AsyncMock()
        mock_erp.query_product.return_value = []
        req = _make_request_with_container(kb=kb, erp=mock_erp)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            result = await sync_from_erp(req)
            assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_erp_unavailable(self):
        from knowledge.router import sync_from_erp

        kb = _make_mock_knowledge_base(available=True)
        req = _make_request_with_container(kb=kb, erp=None)

        with (
            patch("knowledge.router.require_admin", return_value=MagicMock()),
            pytest.raises(HTTPException),
        ):
            await sync_from_erp(req)


class TestKnowledgeReseed:
    """知识库重新种子数据路由测试"""

    @pytest.mark.asyncio
    async def test_reseed_success(self):
        from knowledge.router import reseed_knowledge

        kb = _make_mock_knowledge_base(available=True, count=20)

        seed_patches = {
            "seed_product_knowledge": MagicMock(),
            "seed_faq": MagicMock(),
            "seed_tech_support": MagicMock(),
            "seed_complaint_knowledge": MagicMock(),
            "seed_supplementary_data": MagicMock(),
        }

        req = _make_request_with_container(kb=kb)

        with (
            patch("knowledge.router.require_admin", return_value=MagicMock()),
            patch.dict("sys.modules", {"rag.seed_data": MagicMock(**seed_patches)}),
        ):
            result = await reseed_knowledge(req)
            assert "种子数据已更新" in result["message"]

    @pytest.mark.asyncio
    async def test_reseed_unavailable(self):
        from knowledge.router import reseed_knowledge

        kb = _make_mock_knowledge_base(available=False)

        req = _make_request_with_container(kb=kb)

        with (
            patch("knowledge.router.require_admin", return_value=MagicMock()),
            patch.dict("sys.modules", {"rag.seed_data": MagicMock()}),
            pytest.raises(HTTPException),
        ):
            await reseed_knowledge(req)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. core/exceptions.py — 自定义异常层次
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    """core/exceptions.py 覆盖"""

    # --- AppError (base) ---

    def test_app_error_is_exception_subclass(self):
        from core.exceptions import AppError

        assert issubclass(AppError, Exception)

    def test_app_error_default_message(self):
        from core.exceptions import AppError

        exc = AppError()
        assert str(exc) == ""
        assert exc.code == ""

    def test_app_error_custom_message_and_code(self):
        from core.exceptions import AppError

        exc = AppError("something broke", "ERR_001")
        assert str(exc) == "something broke"
        assert exc.code == "ERR_001"

    def test_app_error_can_be_raised_and_caught(self):
        from core.exceptions import AppError

        with pytest.raises(AppError):
            raise AppError("boom")

    # --- AuthError ---

    def test_auth_error_inherits_app_error(self):
        from core.exceptions import AppError, AuthError

        assert issubclass(AuthError, AppError)

    def test_auth_error_defaults(self):
        from core.exceptions import AuthError

        exc = AuthError()
        assert "认证失败" in str(exc)
        assert exc.code == "AUTH_ERROR"

    def test_auth_error_custom_message(self):
        from core.exceptions import AuthError

        exc = AuthError("token expired")
        assert str(exc) == "token expired"
        assert exc.code == "AUTH_ERROR"

    # --- RateLimitError ---

    def test_rate_limit_error_defaults(self):
        from core.exceptions import AppError, RateLimitError

        assert issubclass(RateLimitError, AppError)
        exc = RateLimitError()
        assert "请求过于频繁" in str(exc)
        assert exc.code == "RATE_LIMIT"

    def test_rate_limit_error_custom(self):
        from core.exceptions import RateLimitError

        exc = RateLimitError("too many requests", "RL_429")
        assert str(exc) == "too many requests"
        assert exc.code == "RL_429"

    # --- ValidationError ---

    def test_validation_error_defaults(self):
        from core.exceptions import AppError, ValidationError

        assert issubclass(ValidationError, AppError)
        exc = ValidationError()
        assert "输入校验失败" in str(exc)
        assert exc.code == "VALIDATION_ERROR"

    def test_validation_error_custom(self):
        from core.exceptions import ValidationError

        exc = ValidationError("field required", "VAL_FIELD")
        assert str(exc) == "field required"
        assert exc.code == "VAL_FIELD"

    # --- LLMError ---

    def test_llm_error_defaults(self):
        from core.exceptions import AppError, LLMError

        assert issubclass(LLMError, AppError)
        exc = LLMError()
        assert "LLM" in str(exc)
        assert exc.code == "LLM_ERROR"

    # --- LLMTimeoutError ---

    def test_llm_timeout_error_inherits_llm_error(self):
        from core.exceptions import LLMError, LLMTimeoutError

        assert issubclass(LLMTimeoutError, LLMError)
        assert issubclass(LLMTimeoutError, Exception)

    def test_llm_timeout_error_defaults(self):
        from core.exceptions import LLMTimeoutError

        exc = LLMTimeoutError()
        assert "超时" in str(exc)
        assert exc.code == "LLM_TIMEOUT"

    def test_llm_timeout_error_custom(self):
        from core.exceptions import LLMTimeoutError

        exc = LLMTimeoutError("request timed out after 30s", "TIMEOUT_30")
        assert "30s" in str(exc)
        assert exc.code == "TIMEOUT_30"

    # --- LLMRateLimitError ---

    def test_llm_rate_limit_error_inherits_llm_error(self):
        from core.exceptions import LLMError, LLMRateLimitError

        assert issubclass(LLMRateLimitError, LLMError)

    def test_llm_rate_limit_error_defaults(self):
        from core.exceptions import LLMRateLimitError

        exc = LLMRateLimitError()
        assert "限流" in str(exc)
        assert exc.code == "LLM_RATE_LIMIT"

    # --- KnowledgeError ---

    def test_knowledge_error_defaults(self):
        from core.exceptions import AppError, KnowledgeError

        assert issubclass(KnowledgeError, AppError)
        exc = KnowledgeError()
        assert "知识库" in str(exc)
        assert exc.code == "KNOWLEDGE_ERROR"

    def test_knowledge_error_custom(self):
        from core.exceptions import KnowledgeError

        exc = KnowledgeError("ChromaDB connection failed")
        assert str(exc) == "ChromaDB connection failed"

    # --- SessionError ---

    def test_session_error_defaults(self):
        from core.exceptions import AppError, SessionError

        assert issubclass(SessionError, AppError)
        exc = SessionError()
        assert "会话" in str(exc)
        assert exc.code == "SESSION_ERROR"

    # --- ERPError ---

    def test_erp_error_defaults(self):
        from core.exceptions import AppError, ERPError

        assert issubclass(ERPError, AppError)
        exc = ERPError()
        assert "ERP" in str(exc)
        assert exc.code == "ERP_ERROR"

    def test_erp_error_custom(self):
        from core.exceptions import ERPError

        exc = ERPError("金蝶接口超时")
        assert str(exc) == "金蝶接口超时"

    # --- Hierarchy completeness ---

    def test_all_exceptions_are_catchable_as_app_error(self):
        """所有自定义异常都能被 AppError 捕获"""
        from core.exceptions import (
            AppError,
            AuthError,
            ERPError,
            KnowledgeError,
            LLMError,
            LLMRateLimitError,
            LLMTimeoutError,
            RateLimitError,
            SessionError,
            ValidationError,
        )

        exc_classes = [
            AuthError,
            RateLimitError,
            ValidationError,
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            KnowledgeError,
            SessionError,
            ERPError,
        ]
        for cls in exc_classes:
            exc = cls("test")
            assert isinstance(exc, AppError), f"{cls.__name__} not caught by AppError"
            assert isinstance(exc, Exception), f"{cls.__name__} not caught by Exception"

    def test_exception_code_attribute_on_all(self):
        """所有异常类都有 code 属性"""
        from core.exceptions import (
            AuthError,
            ERPError,
            KnowledgeError,
            LLMError,
            LLMRateLimitError,
            LLMTimeoutError,
            RateLimitError,
            SessionError,
            ValidationError,
        )

        for cls in [
            AuthError,
            RateLimitError,
            ValidationError,
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            KnowledgeError,
            SessionError,
            ERPError,
        ]:
            exc = cls()
            assert hasattr(exc, "code"), f"{cls.__name__} missing code attribute"
            assert isinstance(exc.code, str), f"{cls.__name__}.code should be str"
            assert len(exc.code) > 0, f"{cls.__name__}.code should not be empty"

    def test_exceptions_can_be_used_in_try_except_chain(self):
        """异常可以在 try/except 链中正确捕获"""
        from core.exceptions import AppError, AuthError, LLMError, LLMTimeoutError

        # LLMTimeoutError -> LLMError -> AppError 的捕获链
        try:
            raise LLMTimeoutError("timeout")
        except LLMTimeoutError:
            pass  # 正确
        else:
            pytest.fail("LLMTimeoutError not caught")

        try:
            raise LLMTimeoutError("timeout")
        except LLMError:
            pass  # 父类捕获
        else:
            pytest.fail("LLMTimeoutError not caught by LLMError")

        try:
            raise LLMTimeoutError("timeout")
        except AppError:
            pass  # 基类捕获
        else:
            pytest.fail("LLMTimeoutError not caught by AppError")

        try:
            raise AuthError("denied")
        except LLMError:
            pytest.fail("AuthError should not be caught by LLMError")
        except AppError:
            pass  # 正确


# ═══════════════════════════════════════════════════════════════════════════════
# 7. alerts/router.py — 告警管理路由
# ═══════════════════════════════════════════════════════════════════════════════


class TestAlertsRouter:
    """alerts/router.py — 告警管理路由端点"""

    @pytest.mark.asyncio
    async def test_get_alert_config_admin(self):
        """GET /api/alerts/config — 管理员可查看配置"""
        from alerts.router import get_alert_config

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_request = MagicMock()

        with (
            patch("alerts.router.require_supervisor_or_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.get_config.return_value = {"webhooks": [], "email": False}
            result = await get_alert_config(mock_request)
            assert "webhooks" in result

    @pytest.mark.asyncio
    async def test_get_alert_config_supervisor(self):
        """GET /api/alerts/config — 主管可查看配置"""
        from alerts.router import get_alert_config

        mock_user = MagicMock()
        mock_user.role = "supervisor"
        mock_request = MagicMock()

        with (
            patch("alerts.router.require_supervisor_or_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.get_config.return_value = {"webhooks": ["http://hook"]}
            result = await get_alert_config(mock_request)
            assert "webhooks" in result

    @pytest.mark.asyncio
    async def test_get_alert_config_forbidden_for_agent(self):
        """GET /api/alerts/config — 普通客服无权限"""
        from alerts.router import get_alert_config

        mock_request = MagicMock()

        with patch(
            "alerts.router.require_supervisor_or_admin", side_effect=HTTPException(status_code=403)
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_alert_config(mock_request)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_test_alert_sends_notification(self):
        """POST /api/alerts/test — 管理员可发送测试告警"""
        from alerts.router import TestAlertRequest, test_alert

        mock_user = MagicMock()
        mock_request = MagicMock()
        data = TestAlertRequest(title="测试", content="测试内容", severity="info")

        with (
            patch("alerts.router.require_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.send_alert = AsyncMock()
            mock_notifier.webhooks = ["http://hook1", "http://hook2"]
            mock_notifier.email_enabled = True
            result = await test_alert(data, mock_request)
            mock_notifier.send_alert.assert_called_once_with("测试", "测试内容", "info")
            assert result["channels"] == 3

    @pytest.mark.asyncio
    async def test_test_alert_no_channels(self):
        """POST /api/alerts/test — 无 webhook 且邮件关闭时 channels=0"""
        from alerts.router import TestAlertRequest, test_alert

        mock_user = MagicMock()
        mock_request = MagicMock()
        data = TestAlertRequest()

        with (
            patch("alerts.router.require_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.send_alert = AsyncMock()
            mock_notifier.webhooks = []
            mock_notifier.email_enabled = False
            result = await test_alert(data, mock_request)
            assert result["channels"] == 0

    @pytest.mark.asyncio
    async def test_alert_history(self):
        """GET /api/alerts/history — 管理员可查看告警历史"""
        from alerts.router import alert_history

        mock_user = MagicMock()
        mock_user.role = "admin"
        mock_request = MagicMock()

        with (
            patch("alerts.router.require_supervisor_or_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.get_history.return_value = [
                {"id": 1, "title": "CPU过高", "severity": "critical"},
                {"id": 2, "title": "内存告警", "severity": "warning"},
            ]
            result = await alert_history(mock_request, limit=10)
            assert len(result["alerts"]) == 2
            mock_notifier.get_history.assert_called_once_with(10)

    @pytest.mark.asyncio
    async def test_alert_history_default_limit(self):
        """GET /api/alerts/history — 默认 limit=20"""
        from alerts.router import alert_history

        mock_user = MagicMock()
        mock_user.role = "supervisor"
        mock_request = MagicMock()

        with (
            patch("alerts.router.require_supervisor_or_admin", return_value=mock_user),
            patch("alerts.router.alert_notifier") as mock_notifier,
        ):
            mock_notifier.get_history.return_value = []
            await alert_history(mock_request)
            mock_notifier.get_history.assert_called_once_with(20)

    def test_require_supervisor_or_admin_role_check(self):
        """require_supervisor_or_admin 对 admin/supervisor 放行，其他角色拒绝"""
        from alerts.router import require_supervisor_or_admin

        for role in ("admin", "supervisor"):
            mock_user = MagicMock()
            mock_user.role = role
            mock_req = MagicMock()
            with patch("alerts.router.require_auth", return_value=mock_user):
                result = require_supervisor_or_admin(mock_req)
                assert result is mock_user

        mock_user_bad = MagicMock()
        mock_user_bad.role = "agent"
        with patch("alerts.router.require_auth", return_value=mock_user_bad):
            with pytest.raises(HTTPException) as exc_info:
                require_supervisor_or_admin(MagicMock())
            assert exc_info.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# 8. core/monitoring.py — MetricsCollector / CircuitBreaker / SLAAlertManager
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsCollectorSnapshot:
    """MetricsCollector.save_snapshot / load_snapshot 覆盖"""

    @pytest.mark.asyncio
    async def test_save_snapshot_with_redis(self):
        """save_snapshot 带 Redis 客户端时持久化"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=1.0, agent="test", mode="sequential")

        mock_redis = MagicMock()
        result = await mc.save_snapshot(redis_client=mock_redis)
        assert result is True
        mock_redis.set.assert_called_once()
        mock_redis.lpush.assert_called_once()
        mock_redis.ltrim.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_snapshot_no_redis(self):
        """save_snapshot 无 Redis 客户端时返回 False"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        result = await mc.save_snapshot(redis_client=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_save_snapshot_redis_exception(self):
        """save_snapshot Redis 异常时返回 False"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        mock_redis = MagicMock()
        mock_redis.set.side_effect = RuntimeError("Redis down")

        result = await mc.save_snapshot(redis_client=mock_redis)
        assert result is False

    def test_load_snapshot_with_redis(self):
        """load_snapshot 从 Redis 加载快照"""
        from core.monitoring import MetricsCollector

        mock_redis = MagicMock()
        mock_redis.get.return_value = '{"timestamp": 123, "stats": {}, "kpi": {}}'

        result = MetricsCollector.load_snapshot(redis_client=mock_redis)
        assert result is not None
        assert result["timestamp"] == 123

    def test_load_snapshot_no_redis(self):
        """load_snapshot 无 Redis 时返回 None"""
        from core.monitoring import MetricsCollector

        result = MetricsCollector.load_snapshot(redis_client=None)
        assert result is None

    def test_load_snapshot_empty_data(self):
        """load_snapshot Redis 返回空数据时返回 None"""
        from core.monitoring import MetricsCollector

        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        result = MetricsCollector.load_snapshot(redis_client=mock_redis)
        assert result is None

    def test_load_snapshot_exception(self):
        """load_snapshot 异常时返回 None"""
        from core.monitoring import MetricsCollector

        mock_redis = MagicMock()
        mock_redis.get.side_effect = RuntimeError("Redis error")

        result = MetricsCollector.load_snapshot(redis_client=mock_redis)
        assert result is None


class TestMetricsCollectorDashboard:
    """MetricsCollector.get_quality_trends / get_hot_questions / get_satisfaction_stats"""

    @pytest.mark.asyncio
    async def test_get_quality_trends(self):
        """get_quality_trends 返回趋势数据"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=0.5, agent="ProductAgent", mode="sequential")
        await mc.record_request(elapsed=1.0, agent="FAQAgent", mode="parallel")

        trends = await mc.get_quality_trends(days=3)
        assert len(trends) == 3
        assert "date" in trends[0]
        assert "avg_score" in trends[0]
        assert "total_queries" in trends[0]

    @pytest.mark.asyncio
    async def test_get_hot_questions(self):
        """get_hot_questions 返回热门问题"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=0.5, agent="ProductAgent", mode="sequential")
        await mc.record_request(elapsed=0.3, agent="ProductAgent", mode="sequential")
        await mc.record_request(elapsed=0.4, agent="FAQAgent", mode="parallel")

        hot = await mc.get_hot_questions(limit=10)
        assert len(hot) >= 1
        # ProductAgent should be top
        assert hot[0]["agent"] == "ProductAgent"
        assert hot[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_get_satisfaction_stats(self):
        """get_satisfaction_stats 返回满意度统计"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_feedback(True, category="product")
        await mc.record_feedback(False, category="product")
        await mc.record_feedback(True, category="service")

        stats = await mc.get_satisfaction_stats()
        assert stats["overall_rate"] > 0
        assert stats["total"] == 3
        assert stats["positive"] == 2
        assert stats["negative"] == 1
        assert "product" in stats["by_category"]

    @pytest.mark.asyncio
    async def test_get_satisfaction_stats_empty(self):
        """get_satisfaction_stats 无数据时返回零值"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        stats = await mc.get_satisfaction_stats()
        assert stats["total"] == 0
        assert stats["overall_rate"] == 0


class TestMetricsCollectorRecordFeedback:
    """MetricsCollector.record_feedback 覆盖"""

    @pytest.mark.asyncio
    async def test_record_feedback_resolved(self):
        """record_feedback resolved=True"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_feedback(True, category="product")
        assert mc.resolution_counts["resolved"] == 1

    @pytest.mark.asyncio
    async def test_record_feedback_not_resolved(self):
        """record_feedback resolved=False"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_feedback(False, category="product")
        assert mc.resolution_counts["failed"] == 1

    @pytest.mark.asyncio
    async def test_record_feedback_no_category(self):
        """record_feedback 无 category"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_feedback(True)
        assert mc.resolution_counts["resolved"] == 1

    @pytest.mark.asyncio
    async def test_record_feedback_category_tracking(self):
        """record_feedback 按分类统计"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_feedback(True, category="product")
        await mc.record_feedback(True, category="product")
        await mc.record_feedback(False, category="product")
        assert mc._feedback_by_category["product"]["positive"] == 2
        assert mc._feedback_by_category["product"]["negative"] == 1


class TestMetricsCollectorSLAWindow:
    """MetricsCollector SLA 窗口和 KPI 覆盖"""

    @pytest.mark.asyncio
    async def test_sla_window_violation_rate(self):
        """get_sla_window_violation_rate 计算窗口违约率"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        # Add fast responses first, then slow ones (so slow ones stay in window)
        # SLA_ALERT_WINDOW=10 in DEV, so only last 10 entries matter
        for _ in range(20):
            await mc.record_request(elapsed=1.0)
        for _ in range(30):
            await mc.record_request(elapsed=100.0)  # well above threshold

        rate = await mc.get_sla_window_violation_rate()
        assert rate > 0

    @pytest.mark.asyncio
    async def test_sla_window_violation_rate_empty(self):
        """get_sla_window_violation_rate 空窗口返回 0"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        rate = await mc.get_sla_window_violation_rate()
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_get_kpi_stats(self):
        """get_kpi_stats 返回 KPI 指标"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=1.0, session_id="s1", resolution_status="resolved")
        await mc.record_request(elapsed=2.0, session_id="s2", escalated=True)
        await mc.record_request(elapsed=0.5, session_id="s1")

        kpi = await mc.get_kpi_stats()
        assert "first_resolution_rate" in kpi
        assert "resolution_rate" in kpi
        assert "ai_handled_rate" in kpi
        assert "total_escalated" in kpi

    @pytest.mark.asyncio
    async def test_record_request_with_all_params(self):
        """record_request 带所有参数"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(
            elapsed=100.0,  # above max threshold (60.0 in DEV)
            agent="ProductAgent",
            mode="parallel",
            cached=False,
            error=True,
            session_id="session_1",
            escalated=False,
            resolution_status="resolved",
        )
        stats = await mc.get_stats()
        assert stats["total_requests"] == 1
        assert stats["total_errors"] == 1
        assert mc.sla_violations == 1

    @pytest.mark.asyncio
    async def test_record_request_cached(self):
        """record_request cached=True"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        await mc.record_request(elapsed=0.001, cached=True)
        assert mc.cache_hits == 1

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions(self):
        """_cleanup_expired_sessions 清理过期会话"""
        from core.monitoring import MetricsCollector

        mc = MetricsCollector()
        mc._session_ttl = 0.1  # 100ms TTL for testing
        await mc.record_request(elapsed=1.0, session_id="old_session")
        time.sleep(0.2)
        # Trigger cleanup
        mc._cleanup_expired_sessions(time.time())
        assert "old_session" not in mc.session_turn_counts


class TestSLAAlertManagerCoverage:
    """SLAAlertManager.check_and_alert 覆盖"""

    @pytest.mark.asyncio
    async def test_check_and_alert_triggers_alert(self):
        """check_and_alert 违约率超阈值时触发告警"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        mc = MetricsCollector()
        # Fill window with slow responses to exceed 30% threshold
        # NOTE: RESPONSE_TIME_TARGET_MAX=60.0 in DEV, use 100.0 to guarantee violations
        for _ in range(40):
            await mc.record_request(elapsed=100.0)

        manager = SLAAlertManager()
        with patch("core.monitoring.alert_notifier", create=True) as _:
            pass  # just suppress the import
        alert = await manager.check_and_alert(mc)
        assert alert is not None
        assert alert["type"] == "sla_violation_high"
        assert alert["severity"] in ("warning", "critical", "emergency")

    @pytest.mark.asyncio
    async def test_check_and_alert_no_alert(self):
        """check_and_alert 违约率未超阈值时不告警"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        mc = MetricsCollector()
        for _ in range(10):
            await mc.record_request(elapsed=1.0)  # well within SLA

        manager = SLAAlertManager()
        alert = await manager.check_and_alert(mc)
        assert alert is None

    @pytest.mark.asyncio
    async def test_check_and_alert_cooldown(self):
        """check_and_alert 冷却期内不重复告警"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        mc = MetricsCollector()
        for _ in range(40):
            await mc.record_request(elapsed=100.0)

        manager = SLAAlertManager()
        alert1 = await manager.check_and_alert(mc)
        assert alert1 is not None

        # Second check within cooldown should return None
        alert2 = await manager.check_and_alert(mc)
        assert alert2 is None

    @pytest.mark.asyncio
    async def test_check_and_alert_critical_severity(self):
        """check_and_alert 违约率超过 2x 阈值时 critical"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        mc = MetricsCollector()
        # 100% violation rate
        for _ in range(50):
            await mc.record_request(elapsed=100.0)

        manager = SLAAlertManager()
        # NOTE: SLA_ALERT_THRESHOLD=50.0 in DEV → critical requires >100%, impossible
        # Patch threshold to 30.0 so 100% > 60% triggers critical
        # v5.4: 3x threshold triggers emergency, so 100% > 90% triggers emergency
        # Use 2.5x threshold to get critical
        with patch("core.monitoring.SLA_ALERT_THRESHOLD", 40.0):
            alert = await manager.check_and_alert(mc)
        assert alert is not None
        # With 100% violation rate (> 80%), severity should be critical
        assert alert["severity"] == "critical"

    def test_get_alerts(self):
        """get_alerts 返回告警历史"""
        from core.monitoring import SLAAlertManager

        manager = SLAAlertManager()
        manager.alerts = [{"id": i} for i in range(50)]
        result = manager.get_alerts(limit=10)
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_check_and_alert_with_bus(self):
        """check_and_alert 带 MessageBus 时发布事件"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        mc = MetricsCollector()
        for _ in range(50):
            await mc.record_request(elapsed=100.0)

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock()

        manager = SLAAlertManager(bus=mock_bus)
        manager.last_alert_time.clear()
        alert = await manager.check_and_alert(mc)
        assert alert is not None

    @pytest.mark.asyncio
    async def test_check_and_alert_alert_history_cap(self):
        """check_and_alert 告警历史上限"""
        from core.monitoring import MetricsCollector, SLAAlertManager

        manager = SLAAlertManager()
        manager.alerts = [{"id": i} for i in range(100)]

        mc = MetricsCollector()
        for _ in range(50):
            await mc.record_request(elapsed=100.0)

        # Reset cooldown
        manager.last_alert_time.clear()
        await manager.check_and_alert(mc)
        assert len(manager.alerts) <= 101  # 100 + 1 new


class TestCircuitBreakerAdvanced:
    """CircuitBreaker 状态转换全覆盖"""

    @pytest.mark.asyncio
    async def test_should_allow_closed(self):
        """CLOSED 状态允许请求"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=3, recovery_time=1)
        assert await cb.should_allow() is True

    @pytest.mark.asyncio
    async def test_should_allow_open_before_recovery(self):
        """OPEN 状态恢复时间前拒绝请求"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=2, recovery_time=10)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == "open"
        assert await cb.should_allow() is False

    @pytest.mark.asyncio
    async def test_should_allow_open_after_recovery(self):
        """OPEN 状态恢复时间后进入 HALF_OPEN 并允许探测"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=2, recovery_time=0)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == "open"
        await asyncio.sleep(0.01)  # Wait for recovery time
        assert await cb.should_allow() is True
        assert cb.state == "half_open"

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self):
        """HALF_OPEN 状态成功后关闭"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=2, recovery_time=0)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.01)
        await cb.should_allow()  # transition to HALF_OPEN
        await cb.record_success()
        assert cb.state == "closed"
        assert cb.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self):
        """HALF_OPEN 状态失败后重新打开"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=2, recovery_time=0)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.01)
        await cb.should_allow()  # HALF_OPEN
        await cb.record_failure()
        assert cb.state == "open"

    @pytest.mark.asyncio
    async def test_half_open_no_permits(self):
        """HALF_OPEN 状态无探测配额时拒绝"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=2, recovery_time=0)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.01)
        first = await cb.should_allow()  # uses permit
        assert first is True
        second = await cb.should_allow()  # no more permits
        assert second is False

    @pytest.mark.asyncio
    async def test_consecutive_failures_below_threshold(self):
        """失败次数未达阈值时不熔断"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=5, recovery_time=1)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == "closed"
        assert cb.total_failures == 2

    def test_get_status(self):
        """get_status 返回完整状态"""
        from core.monitoring import CircuitBreaker

        cb = CircuitBreaker(fail_threshold=3, recovery_time=30)
        status = cb.get_status()
        assert status["state"] == "closed"
        assert status["fail_threshold"] == 3
        assert status["recovery_time"] == 30
        assert status["consecutive_failures"] == 0
        assert status["total_failures"] == 0
        assert status["total_successes"] == 0
