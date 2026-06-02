"""
FastAPI 应用工厂（v3.2 - Docker / uvicorn 入口）
用法: uvicorn api.app_factory:app --host 0.0.0.0 --port 8000
"""
from multi_agent_customer_service import make_graph, session_mgr, cache, metrics, bus, sla_alert_mgr
from api.app import create_app

graph_app = make_graph()
app = create_app(
    graph_app,
    session_manager=session_mgr,
    response_cache=cache,
    metrics=metrics,
    message_bus=bus,
    sla_alert_mgr=sla_alert_mgr,
)
