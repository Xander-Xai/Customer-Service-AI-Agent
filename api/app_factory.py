"""
FastAPI 应用工厂（v3.7 - 安全加固启动检查）
用法: uvicorn api.app_factory:app --host 0.0.0.0 --port 8000
"""
from multi_agent_customer_service import make_graph, session_mgr, cache, metrics, bus, sla_alert_mgr
from api.app import create_app
from config import (
    API_KEY_ENABLED, API_KEY, MONITORING_ADMIN_TOKEN,
    SESSION_TOKEN_SECRET, CORS_ORIGINS,
)
from logger import get_logger

logger = get_logger("app_factory")

graph_app = make_graph()
app = create_app(
    graph_app,
    session_manager=session_mgr,
    response_cache=cache,
    metrics=metrics,
    message_bus=bus,
    sla_alert_mgr=sla_alert_mgr,
)

# v3.7: 启动安全检查
_security_warnings = []

if not API_KEY_ENABLED:
    _security_warnings.append("API_KEY_ENABLED=false，所有端点未认证！生产环境必须启用 API Key 认证。")

if API_KEY_ENABLED and (not API_KEY or API_KEY in ("", "change-me-in-production")):
    _security_warnings.append("API_KEY 未设置或使用默认值，认证将拒绝所有请求或不安全。")

if not MONITORING_ADMIN_TOKEN or MONITORING_ADMIN_TOKEN in ("", "change-me-monitoring-token"):
    _security_warnings.append("MONITORING_ADMIN_TOKEN 未设置或使用默认值，监控端点安全受限。")

if not SESSION_TOKEN_SECRET or SESSION_TOKEN_SECRET in ("", "change-me-session-secret-in-production"):
    _security_warnings.append("SESSION_TOKEN_SECRET 未设置或使用默认值，会话所有权校验将被禁用。")

if CORS_ORIGINS and "*" in CORS_ORIGINS:
    _security_warnings.append("CORS_ORIGINS 包含 *，生产环境请配置具体域名。")

for w in _security_warnings:
    logger.warning(f"⚠️ 安全警告: {w}")

if not _security_warnings:
    logger.info("✅ 安全检查通过，所有安全配置就绪。")
