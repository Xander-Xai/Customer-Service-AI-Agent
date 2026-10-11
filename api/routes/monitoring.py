"""
监控相关路由：健康检查、指标、KPI、缓存统计、告警、熔断器、Prometheus
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""

import re
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

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
    #
    # key 是否可用由 core.config.evaluate_llm_api_key 唯一判定，与
    # core/container.py 的运行时选择读同一个函数（issue #51）。此前这里内联了一套
    # 更弱的规则（只有前缀黑名单、没有长度下限，且前缀表与运行时那份不一致），
    # 于是会出现 key_valid=true 与「进程正在跑 RuleBasedLLM」同时成立 ——
    # 而 /api/health 既是 README 的验证入口也是 compose 给 app 的 healthcheck。
    #
    # 本路径**不做任何 provider 调用**：它是无鉴权端点，不能因为确认 key 就去打
    # 计费接口。key 是否真被接受由启动期 ServiceContainer._check_llm_health 用一次
    # 真实调用确认，其结论只体现在 implementation / degraded 上。
    from core.config import (
        AGENT_EXECUTION_MODE,
        AGENT_INLINE_COMPAT_ENDPOINTS,
        AGENT_QUEUED_RUN_ENDPOINTS,
        LLM_PROVIDER,
        evaluate_llm_api_key,
    )

    llm_key_status = evaluate_llm_api_key()
    llm_key_valid = llm_key_status.usable
    llm_provider = LLM_PROVIDER

    # 「实际在跑哪个实现」是运行时事实，只能从容器里读，不能重算一遍 —— 重算就是
    # 第二套逻辑，正是本 issue 要消除的东西。
    llm_implementation = "unknown"
    llm_degraded = False
    container = getattr(state, "container", None)
    active_llm = getattr(container, "llm", None)
    if active_llm is not None:
        llm_implementation = type(active_llm).__name__
        from llm.rule_based_llm import RuleBasedLLM

        # 降级 = 活跃实现不是真实 provider 客户端。RuleBasedLLM 是模板兜底，
        # 它能回答请求但不代表 LLM 能力可用，必须在健康面可见。
        llm_degraded = isinstance(active_llm, RuleBasedLLM)

    # v6.0: Qdrant 健康检查
    qdrant_ok = False
    try:
        import asyncio

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
        checkpoint_info.get("backend") == "postgres" and checkpoint_info.get("status") != "healthy"
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
        # 自述执行模式，避免"所有请求都经过 Worker"的误读：
        # 快路径是兼容性 inline 路径，只有 /api/runs 受 AGENT_EXECUTION_MODE 控制。
        "agent_execution": {
            "mode": AGENT_EXECUTION_MODE,
            "durable_async_runs": AGENT_EXECUTION_MODE == "queued",
            "inline_compat_endpoints": list(AGENT_INLINE_COMPAT_ENDPOINTS),
            "queued_run_endpoints": list(AGENT_QUEUED_RUN_ENDPOINTS),
        },
        "components": {
            "circuit_breaker": {
                "state": circuit_state,
                "consecutive_failures": cb_status.get("consecutive_failures", 0),
            },
            "redis": {"connected": redis_ok, "latency_ms": redis_latency_ms},
            "llm": {
                "configured": llm_key_status.configured,
                # key_usable: 与运行时同一个判定的结论（issue #51）
                "key_usable": llm_key_valid,
                # key_valid: 既有字段，保留以免破坏 compose healthcheck / 既有消费者；
                # 与 key_usable 同源同值，不再是独立实现。
                "key_valid": llm_key_valid,
                "key_reason": llm_key_status.reason,
                "key_length": llm_key_status.length,
                "provider": llm_provider,
                # 实际活跃实现与降级状态：回答"进程现在到底在用什么"
                "implementation": llm_implementation,
                "degraded": llm_degraded,
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


def _registry_exposition() -> bytes:
    """序列化完整 Prometheus 暴露内容（Prometheus 文本格式）。

    抽成模块级函数（而不是写在路由体内）是为了让
    ``tests/unit/test_metrics_exposure_contract.py`` 断言的**就是路由真正返回的
    那份字节**，而不是另写一套遍历注册表的实现 —— 那样测试就成了第二套实现，
    恰好会漏掉「路由返回了什么」这个真正的断点。

    委托给 ``core.metrics_exposition``，因为暴露面必须同时正确处理两种进程拓扑：

    - 单进程（默认）：直接序列化默认 ``REGISTRY``；
    - 多进程（``PROMETHEUS_MULTIPROC_DIR``，生产 ``app`` / ``worker`` **两个容器**
      的必需配置）：用 ``MultiProcessCollector`` 聚合 worker 进程的样本。

    第二种不是可选优化。不开它，``agent_run_dead_letter_total`` 在抓取侧**恒为 0**
    —— 递增发生在 worker 进程，Prometheus 只抓 app 容器 —— 告警仍然不会触发，
    而且这次是「指标存在但恒零」，比「指标不存在」更难被发现。

    失败时返回一条可读的注释而不是抛异常：监控端点不能反过来把请求搞 500。
    ``prometheus_client`` 缺失时 ``core.monitoring`` 本就把指标降级为 no-op，
    此时显式声明「没有可暴露的指标」比返回一个看似正常的空 body 更诚实。
    """
    from core.metrics_exposition import exposition_text, multiprocess_enabled

    if not multiprocess_enabled():
        try:
            import prometheus_client  # noqa: F401  (import 可用性探针)
        except ImportError:
            logger.warning("[Prometheus] prometheus_client 未安装，无可暴露指标")
            return b"# prometheus_client is not installed; no metrics are exposed\n"
    try:
        return exposition_text()
    except Exception as exc:  # noqa: BLE001 - 监控端点不得反过来打断请求
        logger.error("[Prometheus] 生成暴露内容失败: %s", type(exc).__name__, exc_info=True)
        return b"# failed to generate Prometheus exposition\n"


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_registry(request: Request):
    """**标准** Prometheus 抓取端点：序列化 ``prometheus_client`` 的全量 ``REGISTRY``。

    为什么必须有这个端点（历史断链，P0-1）
    --------------------------------------
    ``core/monitoring.py`` / ``runtime`` / ``cache`` / ``tools.mcp_adapter`` 在
    ``REGISTRY`` 上注册了约 70 个指标（``agent_run_dead_letter_total``、
    ``agent_run_retry_total``、全部 HITL / tool-result / MCP 指标）。在它之前，
    全仓唯一的 Prometheus 输出是下面那个 ``/metrics/prometheus``，而它**手工拼接
    10 行 ``csai_*`` 文本**、从不触碰 ``REGISTRY``。结果是：注册的约 70 个指标没有
    任何一条进入 Prometheus，``monitoring/alert_rules.yml`` 里
    ``increase(agent_run_dead_letter_total[5m]) > 0`` 引用的是一个**永远不存在的时间
    序列**，DLQ 告警因此**不可能触发**（有崩溃恢复、有 DLQ、有重放、就是没人被通知）。

    这里用 ``prometheus_client`` 官方序列化而不是自己拼字符串，理由是它与注册表
    天然一致：新增/改名任何指标都会立刻出现在输出里，不会再出现「注册了但没暴露」
    这类**需要靠人记得**的同步。契约由 ``tests/unit/test_metrics_exposure_contract.py``
    锁定（断言告警规则与 Grafana 面板引用的每个 metric name 都在本端点输出里）。

    认证与 ``/metrics/prometheus`` 一致（``X-Admin-Token`` 或
    ``Authorization: Bearer``，见 ``api.utils.check_admin_token``）—— Prometheus 侧用
    ``bearer_token_file`` 配好凭据即可（``make monitoring-token``）。
    """
    _require_monitoring_auth(request)
    body = _registry_exposition()
    if body.startswith(b"# prometheus_client is not installed"):
        # 「显式没有」优于「看起来正常的 200 空 body」：Prometheus 侧会立刻看到一个
        # 抓取失败的 target，而不是把空注册表当成正常数据。
        return Response(content=body, media_type="text/plain; charset=utf-8", status_code=503)
    from prometheus_client import CONTENT_TYPE_LATEST

    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


@router.get("/metrics/prometheus")
async def prometheus_metrics(request: Request):
    """Prometheus 格式指标输出（业务聚合 ``csai_*``）。"""
    _require_monitoring_auth(request)
    state = request.app.state
    metrics = getattr(state, "metrics", None)
    if not metrics:
        return PlainTextResponse("# Metrics not available\n", media_type="text/plain")

    stats = await metrics.get_stats()
    kpi = await metrics.get_kpi_stats()
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
        # 以下 4 个由 `get_stats()` 计算得出但此前**从未输出**，导致 Grafana
        # csai-overview 的「P95 响应时间」「缓存命中率」两个面板永远为空。
        "# HELP csai_p95_response_time_seconds P95 response time",
        "# TYPE csai_p95_response_time_seconds gauge",
        f"csai_p95_response_time_seconds {stats.get('p95_response_time', 0)}",
        "",
        "# HELP csai_cache_hit_rate_percent Cache hit rate percentage",
        "# TYPE csai_cache_hit_rate_percent gauge",
        f"csai_cache_hit_rate_percent {stats.get('cache_hit_rate', 0)}",
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

    # 协作模式分布（Grafana 面板引用，此前从未输出 -> 面板永久为空）
    mode_counts = stats.get("mode_counts", {})
    if mode_counts:
        lines.append("# HELP csai_collaboration_mode_total Collaboration mode usage count")
        lines.append("# TYPE csai_collaboration_mode_total counter")
        for mode, count in mode_counts.items():
            safe_mode = _PROM_LABEL_RE.sub("_", mode)
            lines.append(f'csai_collaboration_mode_total{{mode="{safe_mode}"}} {count}')
        lines.append("")

    # KPI 计数（Grafana「KPI — AI 处理率 / 升级率」面板引用，此前从未输出）。
    # 直接取 `get_kpi_stats()` 的整数计数，不从已格式化的百分号字符串二次推导 ——
    # 那样会丢精度并让语义变成「字符串解析结果」。
    lines.append("# HELP csai_total_ai_handled Requests handled by AI")
    lines.append("# TYPE csai_total_ai_handled counter")
    lines.append(f"csai_total_ai_handled {kpi.get('total_ai_handled', 0)}")
    lines.append("")
    lines.append("# HELP csai_total_escalated Requests flagged for human escalation")
    lines.append("# TYPE csai_total_escalated counter")
    lines.append(f"csai_total_escalated {kpi.get('total_escalated', 0)}")
    lines.append("")
    lines.append("# HELP csai_total_single_turn_resolved Sessions resolved in one turn")
    lines.append("# TYPE csai_total_single_turn_resolved counter")
    lines.append(f"csai_total_single_turn_resolved {kpi.get('total_single_turn_resolved', 0)}")
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
