"""
监控相关路由：健康检查、指标、KPI、缓存统计、告警、熔断器、Prometheus
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""

import os
import re
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.monitoring")


def _require_monitoring_auth(request: Request):
    """监控端点权限检查：admin token / admin/supervisor JWT 可访问，其他 401/403"""
    from fastapi import HTTPException

    # 优先检查 X-Admin-Token（供监控系统使用）
    from api.utils import check_admin_token

    if check_admin_token(request):
        return None  # admin token 通过，无需返回 user 对象

    # 回退到 JWT 认证
    from auth.router import require_auth

    user = require_auth(request)
    if user.role not in ("admin", "supervisor"):
        raise HTTPException(status_code=403, detail="需要管理员或主管权限")
    return user


# Prometheus label 安全正则
_PROM_LABEL_RE = re.compile(r"[^a-zA-Z0-9_]")


@router.get("/api/health")
async def health(request: Request):
    """增强健康检查（各子系统状态、版本、运行信息）
    注意：此端点无需认证，供负载均衡器和监控系统使用。
    详细监控指标请使用 /api/metrics（需要 supervisor+ 权限）。
    """
    state = request.app.state
    now = time.time()
    cached_health = getattr(state, "_cached_health_status", None)
    cached_time = getattr(state, "_cached_health_time", 0)
    if cached_health and (now - cached_time) < 5.0:
        res = dict(cached_health)
        res["timestamp"] = now
        return res

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
        except Exception as e:
            logger.debug(f"[Health] Redis 连接检查失败: {e}")

    # LLM
    llm_api_key = os.environ.get("OPENAI_API_KEY", "")
    _placeholder_prefixes = ("sk-placeholder", "your-", "sk-xxx", "sk-your", "sk-test-placeholder")
    llm_key_valid = bool(llm_api_key) and not any(
        llm_api_key.lower().startswith(p) for p in _placeholder_prefixes
    )
    from core.config import LLM_PROVIDER

    llm_provider = LLM_PROVIDER

    # v6.0: Qdrant 健康检查
    qdrant_ok = False
    try:
        import asyncio
        container = getattr(state, "container", None)
        if container and getattr(container, "knowledge_base", None):
            kb = container.knowledge_base
            qdrant_ok = kb.available
            if qdrant_ok and hasattr(kb, "_client"):
                await asyncio.to_thread(kb._client.get_collections)
        else:
            from qdrant_client import QdrantClient

            from core.config import QDRANT_HOST, QDRANT_PORT

            test_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=3.0)
            await asyncio.to_thread(test_client.get_collections)
            qdrant_ok = True
    except Exception as e:
        logger.debug(f"[Health] Qdrant 连接检查失败: {e}")

    # Database
    db_ok = False
    db_latency_ms = None
    try:
        from sqlalchemy import text as _sql_text

        from db.database import engine

        t0 = time.time()
        def _check_db():
            with engine.connect() as conn:
                conn.execute(_sql_text("SELECT 1"))

        await asyncio.to_thread(_check_db)
        db_latency_ms = round((time.time() - t0) * 1000, 2)
        db_ok = True
    except Exception as e:
        logger.debug(f"[Health] 数据库连接检查失败: {e}")

    from core.config import DEV_MODE, REDIS_URL, VERSION

    uptime_seconds = round(time.time() - getattr(state, "module_load_time", time.time()), 2)
    circuit_state = cb_status["state"]

    # LangGraph Checkpoint 后端 + 连通性（严禁返回 URI/用户名/密码）
    checkpoint_info = {"backend": "disabled", "status": "unavailable"}
    container = getattr(state, "container", None)
    if container is not None:
        try:
            from core.checkpointer import probe_checkpoint_runtime

            checkpoint_info = await probe_checkpoint_runtime(
                getattr(container, "_checkpoint_runtime", None)
            )
        except Exception as e:
            logger.debug(f"[Health] checkpoint 探活失败: {type(e).__name__}")
            checkpoint_info = {"backend": "unknown", "status": "unavailable"}

    overall = "healthy"
    if not db_ok or not llm_key_valid:
        overall = "unhealthy"
    elif (
        checkpoint_info.get("backend") == "postgres"
        and checkpoint_info.get("status") != "healthy"
    ):
        # 生产要求可持久化 checkpoint；后端不可用算不健康
        overall = "unhealthy"
    elif circuit_state == "open" or (not redis_ok and REDIS_URL) or not qdrant_ok:
        overall = "degraded"

    res = {
        "status": overall,
        "version": VERSION,
        "mode": "dev" if DEV_MODE else "prod",
        "timestamp": now,
        "uptime_seconds": uptime_seconds,
        "python_version": sys.version.split()[0],
        "langgraph_checkpoint": checkpoint_info,
        "components": {
            "circuit_breaker": {
                "state": circuit_state,
                "consecutive_failures": cb_status.get("consecutive_failures", 0),
            },
            "redis": {"connected": redis_ok, "latency_ms": redis_latency_ms},
            "llm": {
                "configured": bool(llm_api_key),
                "key_valid": llm_key_valid,
                "provider": llm_provider,
            },
            "qdrant": {"connected": qdrant_ok},
            "database": {"connected": db_ok, "latency_ms": db_latency_ms},
            "langgraph_checkpoint": checkpoint_info,
        },
    }
    state._cached_health_status = res
    state._cached_health_time = now
    return res


@router.get("/api/metrics")
async def metrics_endpoint(request: Request):
    """获取系统性能指标（v5.4: 自动更新业务指标）"""
    _require_monitoring_auth(request)
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    cache = getattr(state, "response_cache", None)

    # v5.4: 更新业务指标Gauge
    if metrics:
        await metrics.update_business_metrics()

    stats = await metrics.get_stats() if metrics else {"error": "metrics not initialized"}
    cache_stats = cache.get_stats() if cache else {}

    # 持久化快照
    persist_fn = getattr(state, "persist_metrics_snapshot", None)
    if persist_fn:
        await persist_fn()

    from core.config import VERSION

    return {"version": VERSION, "metrics": stats, "cache": cache_stats, "timestamp": time.time()}


@router.get("/api/kpi")
async def kpi_endpoint(request: Request):
    _require_monitoring_auth(request)
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    kpi = await metrics.get_kpi_stats() if metrics else {"error": "metrics not initialized"}

    persist_fn = getattr(state, "persist_metrics_snapshot", None)
    if persist_fn:
        await persist_fn()

    from core.config import VERSION

    result = {"version": VERSION, "kpi": kpi, "timestamp": time.time()}

    r = getattr(state, "get_redis_client", lambda: None)()
    if r and metrics:
        try:
            snapshot = metrics.load_snapshot(r)
            if snapshot:
                result["last_snapshot"] = snapshot
        except Exception as e:
            logger.debug(f"[KPI] 快照加载失败: {e}")
    return result


@router.get("/api/cache/stats")
async def cache_stats(request: Request):
    _require_monitoring_auth(request)
    cache = getattr(request.app.state, "response_cache", None)
    return cache.get_stats() if cache else {"error": "cache not initialized"}


@router.post("/api/cache/invalidate")
async def invalidate_cache(request: Request, body: dict):
    """按条件删除缓存（主动失效）

    Body 支持：
    - {"product_id": "SKU_123"} — 按商品 ID 删除
    - {"intent_type": "pricing_stock"} — 按意图类型删除
    - {"product_id": "SKU_123", "intent_type": "pricing_stock"} — 联合条件
    """
    _require_monitoring_auth(request)
    cache = getattr(request.app.state, "response_cache", None)
    if not cache:
        return {"error": "cache not initialized"}

    filter_dict = {}
    if body.get("product_id"):
        filter_dict["product_id"] = body["product_id"]
    if body.get("intent_type"):
        filter_dict["intent_type"] = body["intent_type"]

    if not filter_dict:
        return {"error": "至少提供一个过滤条件 (product_id / intent_type)"}

    try:
        cache.invalidate_by_filter(filter_dict)
        return {"status": "ok", "filter": filter_dict}
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/alerts")
async def list_alerts(request: Request, limit: int = 20):
    _require_monitoring_auth(request)
    sla_mgr = getattr(request.app.state, "sla_alert_mgr", None)
    if sla_mgr:
        return {"alerts": sla_mgr.get_alerts(limit)}
    return {"alerts": []}


@router.get("/api/circuit-breaker")
async def circuit_breaker_status(request: Request):
    _require_monitoring_auth(request)
    cb = getattr(request.app.state, "circuit_breaker", None)
    if cb:
        return cb.get_status()
    return {"state": "unknown"}


@router.get("/metrics/prometheus")
async def prometheus_metrics(request: Request):
    """Prometheus 格式指标输出"""
    _require_monitoring_auth(request)
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    if not metrics:
        return PlainTextResponse("# Metrics not available\n", media_type="text/plain")

    stats = await metrics.get_stats()
    from core.config import VERSION

    lines = [
        "# HELP csai_info Service information",
        "# TYPE csai_info gauge",
        f'csai_info{{version="{VERSION}"}} 1',
        "",
        "# HELP csai_requests_total Total requests",
        "# TYPE csai_requests_total counter",
        f"csai_requests_total {stats.get('total_requests', 0)}",
        "",
        "# HELP csai_errors_total Total errors",
        "# TYPE csai_errors_total counter",
        f"csai_errors_total {stats.get('total_errors', 0)}",
        "",
        "# HELP csai_error_rate_percent Error rate percentage",
        "# TYPE csai_error_rate_percent gauge",
        f"csai_error_rate_percent {stats.get('error_rate', 0)}",
        "",
        "# HELP csai_avg_response_time_seconds Average response time",
        "# TYPE csai_avg_response_time_seconds gauge",
        f"csai_avg_response_time_seconds {stats.get('avg_response_time', 0)}",
        "",
    ]

    # Agent 分布
    agent_counts = stats.get("agent_call_counts", {})
    if agent_counts:
        lines.append("# HELP csai_agent_calls_total Agent call counts")
        lines.append("# TYPE csai_agent_calls_total counter")
        for agent, count in agent_counts.items():
            safe_agent = _PROM_LABEL_RE.sub("_", agent)
            lines.append(f'csai_agent_calls_total{{agent="{safe_agent}"}} {count}')
        lines.append("")

    # SLA
    sla = stats.get("sla", {})
    if sla:
        lines.append("# HELP csai_sla_violation_rate SLA violation rate")
        lines.append("# TYPE csai_sla_violation_rate gauge")
        lines.append(f"csai_sla_violation_rate {sla.get('violation_rate', 0)}")
        lines.append(
            "# HELP csai_sla_window_violation_rate_percent SLA window violation rate percentage"
        )
        lines.append("# TYPE csai_sla_window_violation_rate_percent gauge")
        lines.append(
            f"csai_sla_window_violation_rate_percent {sla.get('window_violation_rate', 0)}"
        )

    # 熔断器
    cb = getattr(state, "circuit_breaker", None)
    if cb:
        cb_status = cb.get_status()
        state_map = {"closed": 0, "open": 1, "half_open": 0.5}
        lines.append("# HELP csai_circuit_breaker_state Circuit breaker state")
        lines.append("# TYPE csai_circuit_breaker_state gauge")
        lines.append(f"csai_circuit_breaker_state {state_map.get(cb_status.get('state', ''), -1)}")
        lines.append("# HELP csai_circuit_breaker_consecutive_failures Consecutive failures count")
        lines.append("# TYPE csai_circuit_breaker_consecutive_failures gauge")
        lines.append(
            f"csai_circuit_breaker_consecutive_failures {cb_status.get('consecutive_failures', 0)}"
        )

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/api/monitoring/quality-trends")
async def quality_trends(request: Request):
    """最近7天质量评分趋势（基于真实查询数据）"""
    _require_monitoring_auth(request)
    metrics = getattr(request.app.state, "metrics", None)
    if not metrics:
        return {"trends": []}
    trends = await metrics.get_quality_trends(days=7)
    return {"trends": trends}


@router.get("/api/monitoring/hot-questions")
async def hot_questions(request: Request):
    """热门问题 TOP10（基于真实查询数据）"""
    _require_monitoring_auth(request)
    metrics = getattr(request.app.state, "metrics", None)
    if not metrics:
        return {"questions": []}
    questions = await metrics.get_hot_questions(limit=10)
    return {"questions": questions}


@router.get("/api/monitoring/satisfaction")
async def satisfaction(request: Request):
    """客户满意度统计（基于真实反馈数据）"""
    _require_monitoring_auth(request)
    metrics = getattr(request.app.state, "metrics", None)
    if not metrics:
        return {"overall_rate": 0, "total": 0, "positive": 0, "negative": 0, "by_category": {}}
    stats = await metrics.get_satisfaction_stats()
    return stats


@router.get("/api/monitoring/token-quota")
async def token_quota_status(request: Request):
    """获取当前用户的 Token Quota 状态（v5.3）"""
    user_id = _get_current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="未认证")

    from core.token_quota import get_quota_manager

    quota_mgr = get_quota_manager()
    return quota_mgr.get_quota_status(str(user_id))


def _get_current_user_id(request: Request) -> str | None:
    """从请求中获取当前用户 ID"""
    # 优先从 JWT payload 获取
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if jwt_payload:
        return str(jwt_payload.get("user_id", jwt_payload.get("sub", "")))
    # 尝试从 header 获取
    user_id = request.headers.get("X-User-ID")
    if user_id:
        return user_id
    return None


@router.get("/api/monitoring/tokens")
async def token_usage(request: Request):
    """LLM Token 用量与延迟统计（v5.1）"""
    _require_monitoring_auth(request)
    from core.token_tracker import get_token_tracker

    tracker = get_token_tracker()
    if not tracker:
        return {"error": "TokenTracker 未初始化"}

    return {
        "global": tracker.get_summary(),
        "by_agent": tracker.get_agent_summary(),
        "by_model": tracker.get_model_summary(),
    }
