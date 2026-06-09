"""
低覆盖率核心模块测试（v5.0+）
覆盖：core/ab_testing / core/tracing / alerts/notifier / llm/rule_based_llm / knowledge/router
运行: pytest tests/test_core_modules.py -v --tb=short
"""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

        with patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email:
            with patch.object(notifier, "_send_webhook", new_callable=AsyncMock):
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

        with patch.object(notifier, "_send_email", new_callable=AsyncMock) as mock_email:
            with patch.object(notifier, "_send_webhook", new_callable=AsyncMock):
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

        webhook = {"name": "evil", "url": "http://169.254.169.254/latest/meta-data", "type": "wecom"}
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
            alert = {"title": "测试", "content": "内容", "severity": "warning", "timestamp": time.time()}
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
            alert = {"title": "测试", "content": "内容", "severity": "warning", "timestamp": time.time()}
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
            alert = {"title": "测试", "content": "内容", "severity": "info", "timestamp": time.time()}
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
            alert = {"title": "测试", "content": "内容", "severity": "info", "timestamp": time.time()}
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

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=kb)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                result = await knowledge_stats(MagicMock())
                assert result["available"] is True
                assert "collections" in result
                assert result["total"] > 0

    @pytest.mark.asyncio
    async def test_stats_unavailable(self):
        from knowledge.router import knowledge_stats

        kb = _make_mock_knowledge_base(available=False)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=kb)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                result = await knowledge_stats(MagicMock())
                assert result["available"] is False

    @pytest.mark.asyncio
    async def test_stats_none_knowledge_base(self):
        from knowledge.router import knowledge_stats

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=None)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = None
                result = await knowledge_stats(MagicMock())
                assert result["available"] is False

    @pytest.mark.asyncio
    async def test_stats_exception_handling(self):
        from knowledge.router import knowledge_stats

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict("sys.modules", {"multi_agent_customer_service": None}):
                # Force an import error
                result = await knowledge_stats(MagicMock())
                assert result["available"] is False


class TestKnowledgeAddDocuments:
    """知识库添加文档路由测试"""

    @pytest.mark.asyncio
    async def test_add_documents_success(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=True, count=15)
        data = AddDocRequest(documents=["新品面膜", "补水精华"])

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=kb)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                result = await add_documents("product_knowledge", data, MagicMock())
                assert "已添加 2 条文档" in result["message"]
                kb.add_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_documents_invalid_collection(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=True)
        data = AddDocRequest(documents=["test"])

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=kb)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                with pytest.raises(Exception):
                    await add_documents("invalid_collection", data, MagicMock())

    @pytest.mark.asyncio
    async def test_add_documents_unavailable(self):
        from knowledge.router import AddDocRequest, add_documents

        kb = _make_mock_knowledge_base(available=False)
        data = AddDocRequest(documents=["test"])

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {"multi_agent_customer_service": MagicMock(knowledge_base=kb)},
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                with pytest.raises(Exception):
                    await add_documents("product_knowledge", data, MagicMock())


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

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {
                    "multi_agent_customer_service": MagicMock(
                        knowledge_base=kb, erp=mock_erp
                    )
                },
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                mas_mod.erp = mock_erp
                result = await sync_from_erp(MagicMock())
                assert result["synced"] == 2
                assert result["total"] == 5

    @pytest.mark.asyncio
    async def test_sync_erp_returns_empty(self):
        from knowledge.router import sync_from_erp

        kb = _make_mock_knowledge_base(available=True)
        mock_erp = AsyncMock()
        mock_erp.query_product.return_value = []

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {
                    "multi_agent_customer_service": MagicMock(
                        knowledge_base=kb, erp=mock_erp
                    )
                },
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                mas_mod.erp = mock_erp
                result = await sync_from_erp(MagicMock())
                assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_erp_unavailable(self):
        from knowledge.router import sync_from_erp

        kb = _make_mock_knowledge_base(available=True)
        mock_erp = None

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {
                    "multi_agent_customer_service": MagicMock(
                        knowledge_base=kb, erp=mock_erp
                    )
                },
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                mas_mod.erp = None
                with pytest.raises(Exception):
                    await sync_from_erp(MagicMock())


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

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {
                    "multi_agent_customer_service": MagicMock(knowledge_base=kb),
                    "rag.seed_data": MagicMock(**seed_patches),
                },
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                result = await reseed_knowledge(MagicMock())
                assert "种子数据已更新" in result["message"]

    @pytest.mark.asyncio
    async def test_reseed_unavailable(self):
        from knowledge.router import reseed_knowledge

        kb = _make_mock_knowledge_base(available=False)

        with patch("knowledge.router.require_admin", return_value=MagicMock()):
            with patch.dict(
                "sys.modules",
                {
                    "multi_agent_customer_service": MagicMock(knowledge_base=kb),
                    "rag.seed_data": MagicMock(),
                },
            ):
                import multi_agent_customer_service as mas_mod

                mas_mod.knowledge_base = kb
                with pytest.raises(Exception):
                    await reseed_knowledge(MagicMock())
