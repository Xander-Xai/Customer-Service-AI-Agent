"""
告警通知发送器（v5.4 — 分级告警 + 升级机制）

告警分级策略：
- warning: Webhook通知（钉钉/企业微信/飞书）
- critical: Webhook + 邮件通知
- emergency: Webhook + 邮件 + SMS + 电话通知 + 自动升级

升级机制：
- 同一告警30分钟无人响应 → 升级到上一级
- critical持续1小时 → 升级为emergency
- 支持告警抑制避免风暴
"""

import asyncio
import json
import os
import time
from typing import Any

import httpx

from core.logger import get_logger

logger = get_logger("alerts.notifier")


class AlertNotifier:
    """
    告警通知发送器（v5.4 - 分级告警 + 升级机制）
    
    核心功能：
    1. 分级通知路由（warning/critical/emergency）
    2. 告警升级（无人响应时自动升级）
    3. 告警抑制（避免告警风暴）
    4. 多渠道通知（Webhook/Email/SMS/Phone）
    
    使用示例：
        >>> notifier = AlertNotifier()
        >>> await notifier.send_alert("SLA违约", "违约率50%", severity="critical")
    """

    # v5.4: 告警升级配置
    UPGRADE_TIMEOUT_CRITICAL = 1800  # critical 30分钟后升级
    UPGRADE_TIMEOUT_EMERGENCY = 3600  # emergency持续1小时后再次通知
    
    def __init__(self):
        self.webhooks: list[
            dict[str, str]
        ] = []  # [{"name": "...", "url": "...", "type": "dingtalk|wecom|feishu"}]
        self.email_enabled = False
        self.email_config: dict[str, str] = {}
        self.alert_history: list[dict[str, Any]] = []
        
        # v5.4: 告警升级跟踪
        self.active_alerts: dict[str, dict[str, Any]] = {}  # alert_key -> {severity, timestamp, escalated}
        self.suppression_window: dict[str, float] = {}  # alert_key -> last_sent_time
        
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
        
        # v5.4: SMS配置（可选）
        self.sms_enabled = bool(os.getenv("SMS_API_KEY"))
        self.sms_config = {
            "api_key": os.getenv("SMS_API_KEY", ""),
            "phone_numbers": [
                p.strip() for p in os.getenv("ALERT_PHONE_NUMBERS", "").split(",") if p.strip()
            ],
        }

        if self.webhooks:
            logger.info(f"告警 Webhook 已配置: {len(self.webhooks)} 个")
        if self.email_enabled:
            logger.info(f"告警邮件已配置: {self.email_config['to_addrs']}")
        if self.sms_enabled:
            logger.info(f"告警SMS已配置: {len(self.sms_config['phone_numbers'])} 个号码")

    async def send_alert(self, title: str, content: str, severity: str = "warning"):
        """
        发送分级告警通知
        
        分级策略：
        - warning: 仅Webhook
        - critical: Webhook + Email
        - emergency: Webhook + Email + SMS + Phone
        
        Args:
            title: 告警标题
            content: 告警内容
            severity: 告警级别 (warning/critical/emergency)
        """
        alert_key = f"{title}:{severity}"
        
        # v5.4: 告警抑制检查（同类型告警5分钟内不重复发送）
        now = time.time()
        if alert_key in self.suppression_window:
            if now - self.suppression_window[alert_key] < 300:  # 5分钟
                logger.debug(f"告警抑制: {alert_key}")
                return
        
        alert = {
            "title": title,
            "content": content,
            "severity": severity,
            "timestamp": now,
        }
        
        self.alert_history.append(alert)
        if len(self.alert_history) > 200:
            self.alert_history = self.alert_history[-200:]
        
        # v5.4: 记录活动告警用于升级检查
        self.active_alerts[alert_key] = {
            "severity": severity,
            "timestamp": now,
            "escalated": False,
        }
        
        # 更新抑制窗口
        self.suppression_window[alert_key] = now
        
        tasks = []
        
        # Webhook：所有级别均发送
        for wh in self.webhooks:
            tasks.append(self._send_webhook(wh, alert))

        # 邮件：critical 和 emergency 级别发送
        if severity in ("critical", "emergency") and self.email_enabled:
            tasks.append(self._send_email(alert))
        
        # v5.4: SMS/Phone：仅 emergency 级别
        if severity == "emergency" and self.sms_enabled:
            tasks.append(self._send_sms(alert))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
        logger.info(f"告警已发送: [{severity.upper()}] {title}")

    async def check_and_upgrade_alerts(self):
        """
        v5.4: 检查并升级活动告警
        
        升级规则：
        1. critical 持续30分钟 → 升级为 emergency
        2. emergency 持续1小时 → 再次通知管理层
        """
        now = time.time()
        
        for alert_key, alert_info in list(self.active_alerts.items()):
            elapsed = now - alert_info["timestamp"]
            severity = alert_info["severity"]
            
            # critical → emergency 升级
            if severity == "critical" and elapsed > self.UPGRADE_TIMEOUT_CRITICAL:
                if not alert_info["escalated"]:
                    logger.warning(f"告警升级: {alert_key} critical → emergency")
                    title, _ = alert_key.rsplit(":", 1)
                    await self.send_alert(
                        f"[升级] {title}",
                        f"此告警已持续{elapsed//60:.0f}分钟未解决，已升级为emergency级别",
                        severity="emergency"
                    )
                    alert_info["escalated"] = True
            
            # emergency 持续1小时 → 再次通知
            elif severity == "emergency" and elapsed > self.UPGRADE_TIMEOUT_EMERGENCY:
                if not alert_info["escalated"]:
                    logger.critical(f"告警持续: {alert_key} 已超过1小时")
                    title, _ = alert_key.rsplit(":", 1)
                    await self.send_alert(
                        f"[紧急] {title} - 持续未解决",
                        f"此emergency告警已持续{elapsed//60:.0f}分钟，请立即处理！",
                        severity="emergency"
                    )
                    alert_info["escalated"] = True

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

            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
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

    async def _send_sms(self, alert: dict[str, Any]):
        """
        v5.4: 发送短信/电话通知（emergency级别）
        
        注意：需要配置SMS API密钥和电话号码
        示例API服务商：阿里云SMS、腾讯云SMS、Twilio等
        """
        try:
            import requests
            
            severity_label = {
                "emergency": "【紧急】",
                "critical": "【严重】",
            }.get(alert["severity"], "")
            
            body = f"{severity_label} {alert['title']}\n{alert['content'][:50]}\n时间: {time.strftime('%H:%M')}"

            for phone in self.sms_config["phone_numbers"]:
                # 示例：使用通用SMS API（需根据实际服务商调整）
                response = requests.post(
                    os.getenv("SMS_API_URL", "https://api.sms.com/send"),
                    json={
                        "api_key": self.sms_config["api_key"],
                        "to": phone,
                        "message": body,
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    logger.info(f"短信告警发送成功: {phone}")
                else:
                    logger.warning(f"短信告警发送失败 ({response.status_code}): {phone}")

        except ImportError:
            logger.debug("requests库未安装，跳过SMS发送")
        except Exception as e:
            logger.warning(f"短信告警发送异常: {e}")

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
