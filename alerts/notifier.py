"""
告警通知发送器（v4.0 — Webhook + Email）
支持钉钉 / 企业微信 / 飞书 Webhook，以及 SMTP 邮件通知。
"""

import asyncio
import json
import os
import time
from typing import Any

import httpx

from logger import get_logger

logger = get_logger("alerts.notifier")


class AlertNotifier:
    """告警通知发送器"""

    def __init__(self):
        self.webhooks: list[
            dict[str, str]
        ] = []  # [{"name": "...", "url": "...", "type": "dingtalk|wecom|feishu"}]
        self.email_enabled = False
        self.email_config: dict[str, str] = {}
        self.alert_history: list[dict[str, Any]] = []
        self._load_config()

    def _load_config(self):
        """从环境变量加载配置"""
        # Webhook 配置（JSON 数组格式）
        webhook_raw = os.getenv("ALERT_WEBHOOKS", "")
        if webhook_raw:
            try:
                self.webhooks = json.loads(webhook_raw)
            except json.JSONDecodeError:
                logger.warning(f"ALERT_WEBHOOKS JSON 解析失败: {webhook_raw[:100]}")

        # 邮件配置
        self.email_config = {
            "smtp_host": os.getenv("SMTP_HOST", ""),
            "smtp_port": int(os.getenv("SMTP_PORT", "587")),
            "smtp_user": os.getenv("SMTP_USER", ""),
            "smtp_password": os.getenv("SMTP_PASSWORD", ""),
            "from_addr": os.getenv("ALERT_EMAIL_FROM", ""),
            "to_addrs": [
                a.strip() for a in os.getenv("ALERT_EMAIL_TO", "").split(",") if a.strip()
            ],
        }
        self.email_enabled = bool(
            self.email_config["smtp_host"]
            and self.email_config["smtp_user"]
            and self.email_config["to_addrs"]
        )

        if self.webhooks:
            logger.info(f"告警 Webhook 已配置: {len(self.webhooks)} 个")
        if self.email_enabled:
            logger.info(f"告警邮件已配置: {self.email_config['to_addrs']}")

    async def send_alert(self, title: str, content: str, severity: str = "warning"):
        """发送告警通知（分级路由：warning → 仅 Webhook，critical → Webhook + 邮件）"""
        alert = {
            "title": title,
            "content": content,
            "severity": severity,
            "timestamp": time.time(),
        }
        self.alert_history.append(alert)
        if len(self.alert_history) > 200:
            self.alert_history = self.alert_history[-200:]

        tasks = []
        # Webhook：warning 和 critical 均发送
        for wh in self.webhooks:
            tasks.append(self._send_webhook(wh, alert))

        # 邮件：仅 critical 级别发送
        if severity == "critical" and self.email_enabled:
            tasks.append(self._send_email(alert))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_webhook(self, webhook: dict[str, str], alert: dict[str, Any]):
        """发送 Webhook 通知"""
        import ipaddress
        from urllib.parse import urlparse

        url = webhook.get("url", "")
        wh_type = webhook.get("type", "dingtalk")
        if not url:
            return

        # v4.0 安全修复: SSRF 防护 — 校验 URL 安全性
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("https", "http"):
                logger.warning(f"Webhook URL 拒绝: 非法 scheme {parsed.scheme}")
                return
            hostname = parsed.hostname or ""
            blocked_hosts = {
                "169.254.169.254",
                "metadata.google.internal",
                "localhost",
                "127.0.0.1",
                "0.0.0.0",
            }
            if hostname in blocked_hosts:
                logger.warning(f"Webhook URL 拒绝: 被禁止的主机 {hostname}")
                return
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    logger.warning(f"Webhook URL 拒绝: 内网/回环地址 {hostname}")
                    return
            except ValueError:
                pass  # 非 IP 地址的主机名，允许通过
        except Exception:
            logger.warning(f"Webhook URL 解析失败: {url[:50]}")
            return

        severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(
            alert["severity"], "⚪"
        )
        text = f"{severity_emoji} **{alert['title']}**\n\n{alert['content']}\n\n⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"

        try:
            if wh_type == "dingtalk":
                payload = {
                    "msgtype": "markdown",
                    "markdown": {"title": alert["title"], "text": text},
                }
            elif wh_type == "wecom":
                payload = {"msgtype": "markdown", "markdown": {"content": text}}
            elif wh_type == "feishu":
                payload = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {"title": {"tag": "plain_text", "content": alert["title"]}},
                        "elements": [{"tag": "markdown", "content": alert["content"]}],
                    },
                }
            else:
                payload = {"text": text}

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info(f"Webhook 告警发送成功: {webhook.get('name', wh_type)}")
                else:
                    logger.warning(
                        f"Webhook 告警发送失败 ({resp.status_code}): {webhook.get('name', wh_type)}"
                    )
        except Exception as e:
            logger.warning(f"Webhook 告警发送异常: {e}")

    async def _send_email(self, alert: dict[str, Any]):
        """发送邮件通知"""
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            severity_label = {
                "critical": "【严重】",
                "warning": "【警告】",
                "info": "【信息】",
            }.get(alert["severity"], "")
            msg = MIMEMultipart()
            msg["From"] = self.email_config["from_addr"]
            msg["To"] = ", ".join(self.email_config["to_addrs"])
            msg["Subject"] = f"{severity_label} {alert['title']}"

            body = f"{alert['content']}\n\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            msg.attach(MIMEText(body, "plain", "utf-8"))

            def _send():
                with smtplib.SMTP(
                    self.email_config["smtp_host"], self.email_config["smtp_port"]
                ) as server:
                    server.starttls()
                    server.login(self.email_config["smtp_user"], self.email_config["smtp_password"])
                    server.sendmail(
                        self.email_config["from_addr"],
                        self.email_config["to_addrs"],
                        msg.as_string(),
                    )

            await asyncio.to_thread(_send)
            logger.info("邮件告警发送成功")
        except Exception as e:
            logger.warning(f"邮件告警发送失败: {e}")

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.alert_history[-limit:]

    def get_config(self) -> dict[str, Any]:
        return {
            "webhooks": [
                {
                    "name": w.get("name", ""),
                    "type": w.get("type", ""),
                    "url": w.get("url", "")[:20] + "...",
                }
                for w in self.webhooks
            ],
            "email_enabled": self.email_enabled,
            "email_to": self.email_config.get("to_addrs", []),
        }


# 全局实例
alert_notifier = AlertNotifier()
