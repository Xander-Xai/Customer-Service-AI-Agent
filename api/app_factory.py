"""
FastAPI 应用工厂（v3.4 - 安全启动检查）
用法: uvicorn api.app_factory:app --host 0.0.0.0 --port 8000
"""
from multi_agent_customer_service import make_graph, session_mgr, cache, metrics, bus, sla_alert_mgr
from api.app import create_app
from config import API_KEY_ENABLED
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

# v3.4: 启动安全检查
if not API_KEY_ENABLED:
    logger.warning("⚠️ API_KEY_ENABLED=false，所有端点未认证！生产环境请启用 API Key 认证。")
