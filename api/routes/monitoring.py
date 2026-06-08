"""
监控相关路由：健康检查、指标、KPI、缓存统计、告警、熔断器、Prometheus
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""
import os
import sys
import time
import re

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from logger import get_logger

router = APIRouter()
logger = get_logger("api.monitoring")

# Prometheus label 安全正则
_PROM_LABEL_RE = re.compile(r'[^a-zA-Z0-9_]')


@router.get("/api/health")
async def health(request: Request):
    """增强健康检查（各子系统状态、版本、运行信息）"""
    state = request.app.state
    cb = getattr(state, "circuit_breaker", None)
    cb_status = cb.get_status() if cb else {"state": "unknown", "consecutive_failures": 0}

    # Redis
    redis_ok = False
    redis_latency_ms = None
    r = getattr(state, "get_redis_client", lambda: None)()
    if r:
        try:
            t0 = time.time()
            r.ping()
            redis_latency_ms = round((time.time() - t0) * 1000, 2)
            redis_ok = True
        except Exception:
            pass

    # LLM
    llm_api_key = os.environ.get("OPENAI_API_KEY", "")
    _placeholder_prefixes = ("sk-placeholder", "your-", "sk-xxx", "sk-your", "sk-test-placeholder")
    llm_key_valid = bool(llm_api_key) and not any(llm_api_key.lower().startswith(p) for p in _placeholder_prefixes)
    from config import LLM_PROVIDER
    llm_provider = LLM_PROVIDER

    # ChromaDB
    chromadb_ok = False
    try:
        import chromadb
        client = chromadb.Client()
        client.heartbeat()
        chromadb_ok = True
    except Exception:
        pass

    # Database
    db_ok = False
    db_latency_ms = None
    try:
        from db.database import engine
        from sqlalchemy import text as _sql_text
        t0 = time.time()
        with engine.connect() as conn:
            conn.execute(_sql_text("SELECT 1"))
        db_latency_ms = round((time.time() - t0) * 1000, 2)
        db_ok = True
    except Exception:
        pass

    from config import VERSION, DEV_MODE, REDIS_URL
    uptime_seconds = round(time.time() - getattr(state, "module_load_time", time.time()), 2)
    circuit_state = cb_status["state"]

    overall = "healthy"
    if not db_ok or not llm_key_valid:
        overall = "unhealthy"
    elif circuit_state == "open" or (not redis_ok and REDIS_URL) or not chromadb_ok:
        overall = "degraded"

    return {
        "status": overall,
        "version": VERSION,
        "mode": "dev" if DEV_MODE else "prod",
        "timestamp": time.time(),
        "uptime_seconds": uptime_seconds,
        "python_version": sys.version.split()[0],
        "components": {
            "circuit_breaker": {"state": circuit_state, "consecutive_failures": cb_status.get("consecutive_failures", 0)},
            "redis": {"connected": redis_ok, "latency_ms": redis_latency_ms},
            "llm": {"configured": bool(llm_api_key), "key_valid": llm_key_valid, "provider": llm_provider},
            "chromadb": {"connected": chromadb_ok},
            "database": {"connected": db_ok, "latency_ms": db_latency_ms},
        },
    }


@router.get("/api/metrics")
async def metrics_endpoint(request: Request):
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    cache = getattr(state, "response_cache", None)
    stats = await metrics.get_stats() if metrics else {"error": "metrics not initialized"}
    cache_stats = cache.get_stats() if cache else {}

    # 持久化快照
    persist_fn = getattr(state, "persist_metrics_snapshot", None)
    if persist_fn:
        await persist_fn()

    from config import VERSION
    return {"version": VERSION, "metrics": stats, "cache": cache_stats, "timestamp": time.time()}


@router.get("/api/kpi")
async def kpi_endpoint(request: Request):
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    kpi = await metrics.get_kpi_stats() if metrics else {"error": "metrics not initialized"}

    persist_fn = getattr(state, "persist_metrics_snapshot", None)
    if persist_fn:
        await persist_fn()

    from config import VERSION
    result = {"version": VERSION, "kpi": kpi, "timestamp": time.time()}

    r = getattr(state, "get_redis_client", lambda: None)()
    if r and metrics:
        try:
            snapshot = metrics.load_snapshot(r)
            if snapshot:
                result["last_snapshot"] = snapshot
        except Exception:
            pass
    return result


@router.get("/api/cache/stats")
async def cache_stats(request: Request):
    cache = getattr(request.app.state, "response_cache", None)
    return cache.get_stats() if cache else {"error": "cache not initialized"}


@router.get("/api/alerts")
async def list_alerts(request: Request, limit: int = 20):
    sla_mgr = getattr(request.app.state, "sla_alert_mgr", None)
    if sla_mgr:
        return {"alerts": sla_mgr.get_alerts(limit)}
    return {"alerts": []}


@router.get("/api/circuit-breaker")
async def circuit_breaker_status(request: Request):
    cb = getattr(request.app.state, "circuit_breaker", None)
    if cb:
        return cb.get_status()
    return {"state": "unknown"}


@router.get("/metrics/prometheus")
async def prometheus_metrics(request: Request):
    """Prometheus 格式指标输出"""
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    if not metrics:
        return PlainTextResponse("# Metrics not available\n", media_type="text/plain")

    stats = await metrics.get_stats()
    from config import VERSION
    lines = [
        f'# HELP csai_info Service information',
        f'# TYPE csai_info gauge',
        f'csai_info{{version="{VERSION}"}} 1',
        '',
        f'# HELP csai_requests_total Total requests',
        f'# TYPE csai_requests_total counter',
        f'csai_requests_total {stats.get("total_requests", 0)}',
        '',
        f'# HELP csai_errors_total Total errors',
        f'# TYPE csai_errors_total counter',
        f'csai_errors_total {stats.get("total_errors", 0)}',
        '',
        f'# HELP csai_avg_response_time_seconds Average response time',
        f'# TYPE csai_avg_response_time_seconds gauge',
        f'csai_avg_response_time_seconds {stats.get("avg_response_time", 0)}',
        '',
    ]

    # Agent 分布
    agent_counts = stats.get("agent_call_counts", {})
    if agent_counts:
        lines.append('# HELP csai_agent_calls_total Agent call counts')
        lines.append('# TYPE csai_agent_calls_total counter')
        for agent, count in agent_counts.items():
            safe_agent = _PROM_LABEL_RE.sub('_', agent)
            lines.append(f'csai_agent_calls_total{{agent="{safe_agent}"}} {count}')
        lines.append('')

    # SLA
    sla = stats.get("sla", {})
    if sla:
        lines.append('# HELP csai_sla_violation_rate SLA violation rate')
        lines.append('# TYPE csai_sla_violation_rate gauge')
        lines.append(f'csai_sla_violation_rate {sla.get("violation_rate", 0)}')

    # 熔断器
    cb = getattr(state, "circuit_breaker", None)
    if cb:
        cb_status = cb.get_status()
        state_map = {"closed": 0, "open": 1, "half_open": 0.5}
        lines.append('# HELP csai_circuit_breaker_state Circuit breaker state')
        lines.append('# TYPE csai_circuit_breaker_state gauge')
        lines.append(f'csai_circuit_breaker_state {state_map.get(cb_status.get("state", ""), -1)}')

    return PlainTextResponse('\n'.join(lines) + '\n', media_type="text/plain")


@router.get("/api/monitoring/quality-trends")
async def quality_trends():
    """最近7天质量评分趋势（初始模拟数据，后续由 metrics collector 累积）"""
    from datetime import date, timedelta
    today = date.today()
    trends = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        trends.append({
            "date": d.isoformat(),
            "avg_score": round(70 + (7 - i) * 1.8, 1),
            "total_queries": 40 + i * 5,
        })
    return {"trends": trends}


@router.get("/api/monitoring/hot-questions")
async def hot_questions():
    """热门问题 TOP10（初始模拟数据，后续由 metrics collector 累积）"""
    return {
        "questions": [
            {"query": "精华液成分有哪些", "count": 23, "category": "product_info"},
            {"query": "如何退货退款", "count": 18, "category": "billing"},
            {"query": "面膜适合什么肤质", "count": 15, "category": "product_info"},
            {"query": "订单物流查询", "count": 14, "category": "order"},
            {"query": "会员积分怎么用", "count": 12, "category": "membership"},
            {"query": "防晒霜SPF怎么选", "count": 11, "category": "product_info"},
            {"query": "过敏了怎么办", "count": 10, "category": "complaint"},
            {"query": "活动优惠有哪些", "count": 9, "category": "promotion"},
            {"query": "产品保质期多久", "count": 8, "category": "product_info"},
            {"query": "怎么修改收货地址", "count": 7, "category": "order"},
        ]
    }


@router.get("/api/monitoring/satisfaction")
async def satisfaction():
    """客户满意度统计（初始模拟数据，后续由 metrics collector 累积）"""
    return {
        "overall_rate": 0.85,
        "total": 120,
        "positive": 102,
        "negative": 18,
        "by_category": {
            "product_info": {"rate": 0.92, "count": 45},
            "billing": {"rate": 0.78, "count": 28},
            "order": {"rate": 0.88, "count": 25},
            "complaint": {"rate": 0.65, "count": 12},
            "membership": {"rate": 0.90, "count": 10},
        },
    }
