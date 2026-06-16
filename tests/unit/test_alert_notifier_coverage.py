"""
测试 alerts/notifier.py — 告警通知器覆盖率补齐
覆盖：_load_config、send_alert、_send_webhook（SSRF防护）、_send_email、get_history、get_config
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch


class TestAlertNotifierLoadConfig:
    @patch.dict(os.environ, {}, clear=False)
    def test_default_config_no_webhooks(self):
        from alerts.notifier import AlertNotifier

        with patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False):
            notifier = AlertNotifier()
            assert notifier.webhooks == []
            assert notifier.email_enabled is False

    @patch.dict(
        os.environ,
        {"ALERT_WEBHOOKS": '[{"name":"test","url":"https://example.com","type":"dingtalk"}]'},
        clear=False,
    )
    def test_webhook_config_loaded(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        assert len(notifier.webhooks) == 1
        assert notifier.webhooks[0]["name"] == "test"

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": "invalid json"}, clear=False)
    def test_webhook_config_invalid_json(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        assert notifier.webhooks == []

    @patch.dict(
        os.environ,
        {
            "SMTP_HOST": "smtp.test.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "user@test.com",
            "SMTP_PASSWORD": "pass123",
            "ALERT_EMAIL_FROM": "alert@test.com",
            "ALERT_EMAIL_TO": "admin@test.com,ops@test.com",
        },
        clear=False,
    )
    def test_email_config_loaded(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        assert notifier.email_enabled is True
        assert notifier.email_config["smtp_host"] == "smtp.test.com"
        assert notifier.email_config["smtp_port"] == 465
        assert len(notifier.email_config["to_addrs"]) == 2

    @patch.dict(os.environ, {"SMTP_HOST": "", "SMTP_USER": "", "ALERT_EMAIL_TO": ""}, clear=False)
    def test_email_disabled_when_incomplete(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        assert notifier.email_enabled is False


class TestSendAlert:
    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_alert_recorded_in_history(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier.send_alert("测试标题", "测试内容", "warning")
        )
        assert len(notifier.alert_history) == 1
        assert notifier.alert_history[0]["title"] == "测试标题"

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_history_capped_at_200(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        notifier.alert_history = [{"title": f"old{j}"} for j in range(205)]
        asyncio.get_event_loop().run_until_complete(notifier.send_alert("new", "content"))
        assert len(notifier.alert_history) <= 200


class TestGetHistory:
    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_returns_limited_history(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        notifier.alert_history = [{"title": f"t{i}"} for i in range(30)]
        result = notifier.get_history(limit=10)
        assert len(result) == 10
        assert result[0]["title"] == "t20"


class TestGetConfig:
    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_returns_sanitized_config(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        config = notifier.get_config()
        assert "webhooks" in config
        assert "email_enabled" in config


class TestSendWebhook:
    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_empty_url_returns(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_ssrf_blocks_localhost(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "http://localhost:8080/alert", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_ssrf_blocks_private_ip(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "http://192.168.1.1/alert", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_ssrf_blocks_metadata_endpoint(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "http://169.254.169.254/metadata", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    def test_ssrf_blocks_ftp_scheme(self):
        from alerts.notifier import AlertNotifier

        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "ftp://example.com/file", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_dingtalk_webhook_success(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.return_value = MagicMock(status_code=200)
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://oapi.dingtalk.com/robot/send", "type": "dingtalk"},
                {"severity": "warning", "title": "告警", "content": "内容"},
            )
        )
        mock_post.assert_called_once()

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_wecom_webhook(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.return_value = MagicMock(status_code=200)
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://qyapi.weixin.qq.com/webhook/send", "type": "wecom"},
                {"severity": "critical", "title": "严重", "content": "问题"},
            )
        )
        mock_post.assert_called_once()

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_feishu_webhook(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.return_value = MagicMock(status_code=200)
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/send", "type": "feishu"},
                {"severity": "info", "title": "信息", "content": "通知"},
            )
        )
        mock_post.assert_called_once()

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_unknown_webhook_type_uses_text(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.return_value = MagicMock(status_code=200)
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://example.com/webhook", "type": "slack"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )
        mock_post.assert_called_once()

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_webhook_non_200_logged(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.return_value = MagicMock(status_code=500)
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://example.com/webhook", "type": "dingtalk", "name": "test"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )

    @patch.dict(os.environ, {"ALERT_WEBHOOKS": ""}, clear=False)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_webhook_exception_caught(self, mock_post):
        from alerts.notifier import AlertNotifier

        mock_post.side_effect = Exception("网络错误")
        notifier = AlertNotifier()
        asyncio.get_event_loop().run_until_complete(
            notifier._send_webhook(
                {"url": "https://example.com/webhook", "type": "dingtalk"},
                {"severity": "warning", "title": "t", "content": "c"},
            )
        )
