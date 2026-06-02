"""
FastAPI + WebSocket 异步服务层 (v3.0)
核心改造：
- 移除 asyncio.to_thread 同步桥接，图节点原生异步
- 新增 /api/metrics 性能监控端点
- 新增 API Key 认证中间件（可配置）
- CORS 从配置读取（不再 allow_origins=["*"]）
- WebSocket 实时推送 Agent 状态变更
- 渐进式轮询优化（0.3s -> 1.5s）
- httpx.AsyncClient 连接池（20 keepalive / 100 max）
"""
import asyncio
import hmac
import time
import uuid
from collections import defaultdict
from typing import Dict, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
from pydantic import BaseModel, Field

from config import (
    CORS_ORIGINS, API_KEY_ENABLED, API_KEY, VERSION,
    RESPONSE_TIME_TARGET_MAX, RESPONSE_TIME_TARGET_MIN,
    HTTPX_MAX_CONNECTIONS, HTTPX_KEEPALIVE_CONNECTIONS, REDIS_URL,
    MAX_QUERY_LENGTH,
)
from logger import get_logger

logger = get_logger("api")


class ChatRequest(BaseModel):
    query: str = Field(..., max_length=2000)
    session_id: str = Field(default="", max_length=36)


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., max_length=36)
    resolved: bool
    comment: str = Field(default="", max_length=500)


# 全局引用（在 create_app 中注入）
_graph_app = None
_session_manager = None
_response_cache = None
_metrics = None
_bus = None
_sla_alert_mgr = None
_redis_client = None  # 懒初始化的 Redis 客户端单例


def _get_redis_client():
    """获取 Redis 客户端单例（懒初始化，失败返回 None）"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception:
            _redis_client = None
    return _redis_client


def create_app(graph_app, session_manager=None, response_cache=None, metrics=None, message_bus=None, sla_alert_mgr=None):
    """创建 FastAPI 应用（v3.2: SLA 告警 + 反馈端点 + 告警端点）"""
    global _graph_app, _session_manager, _response_cache, _metrics, _bus, _sla_alert_mgr
    _graph_app = graph_app
    _session_manager = session_manager
    _response_cache = response_cache
    _metrics = metrics
    _bus = message_bus
    _sla_alert_mgr = sla_alert_mgr

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(
                max_connections=HTTPX_MAX_CONNECTIONS,
                max_keepalive_connections=HTTPX_KEEPALIVE_CONNECTIONS,
            ),
        )
        logger.info(f"httpx 连接池就绪 (max={HTTPX_MAX_CONNECTIONS} / keepalive={HTTPX_KEEPALIVE_CONNECTIONS})")

        yield
        await app.state.http_client.aclose()
        from core.monitoring import OpenAICompatibleClient
        await OpenAICompatibleClient.close_all_clients()
        logger.info("httpx 连接池已关闭")

    app = FastAPI(title="多智能体客服系统", version=VERSION, lifespan=lifespan)

    # CORS 配置（v3.4: 限制允许的方法和头）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    # v3.6: 请求限流中间件
    _rate_limit_store: Dict[str, list] = defaultdict(list)
    _RATE_LIMIT_MAX = 60  # 每分钟最大请求数
    _RATE_LIMIT_WINDOW = 60  # 窗口秒数

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in ("/api/health", "/api/metrics", "/api/kpi", "/"):
            return await call_next(request)
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        # v3.6: WebSocket 端点不做限流（长连接，不适用 HTTP 限流模型）
        if request.url.path.startswith("/ws/"):
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if now - t < _RATE_LIMIT_WINDOW]
        if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        _rate_limit_store[client_ip].append(now)
        return await call_next(request)

    # v3.4: 安全响应头中间件
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # 前端静态资源（v3.3: 暗色主题 UI）
    # 使用项目根目录下的 static 和 templates
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = os.path.join(project_root, "static")
    templates_dir = os.path.join(project_root, "templates")

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"静态资源已挂载: {static_dir}")

    @app.get("/", response_class=HTMLResponse)
    async def serve_index():
        """v3.3 前端主页"""
        index_path = os.path.join(templates_dir, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse("<h1>前端文件未找到</h1>", status_code=404)

    # API Key 认证中间件（v3.0: 可配置）
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if API_KEY_ENABLED:
            # 跳过健康检查、WebSocket 和前端静态资源
            if request.url.path in ("/api/health", "/api/metrics", "/api/kpi", "/api/admin/health", "/"):
                return await call_next(request)
            if request.url.path.startswith("/static/"):
                return await call_next(request)
            api_key = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(api_key, API_KEY):
                return JSONResponse({"error": "Unauthorized: Invalid API Key"}, status_code=401)
        response = await call_next(request)
        return response

    # ------ WebSocket 实时对话 ------

    @app.websocket("/ws/chat")
    async def websocket_chat(ws: WebSocket):
        # v3.4: WebSocket 认证检查 (v3.6: 从 header 获取，防 timing attack)
        if API_KEY_ENABLED:
            key = ws.headers.get("x-api-key", "")
            if not key or not hmac.compare_digest(key, API_KEY):
                await ws.close(code=4001, reason="Unauthorized")
                return
        await ws.accept()
        session_id = str(uuid.uuid4())
        logger.info(f"[WS] 新连接: {session_id}")

        # 注册 Bus 订阅（实时推送 Agent 状态到前端）
        status_messages = []

        async def on_agent_event(msg):
            try:
                payload = msg.payload
                agent_name = payload.get("agent", "")
                topic = msg.topic
                if topic == "agent.processing":
                    status_messages.append(f"{agent_name}正在处理...")
                elif topic == "agent.completed":
                    status_messages.append(f"{agent_name}处理完成")
            except Exception:
                pass

        if _bus:
            _bus.subscribe("agent.processing", on_agent_event)
            _bus.subscribe("agent.completed", on_agent_event)

        try:
            while True:
                data = await ws.receive_json()
                query = data.get("query", "").strip()[:MAX_QUERY_LENGTH]  # v3.4: 输入长度限制
                sid = data.get("session_id", session_id)
                if not query:
                    await ws.send_json({"type": "error", "content": "查询不能为空"})
                    continue

                await ws.send_json({"type": "status", "content": "正在分析您的问题..."})
                status_messages.clear()

                # 渐进式轮询推送（v3.0: 增加 Bus 事件推送）
                async def progressive_notify():
                    delays = [0.3, 0.5, 1.0, 1.5]
                    messages = ["正在识别意图...", "正在分配专家...", "专家处理中...", "即将完成..."]
                    for delay, msg in zip(delays, messages):
                        await asyncio.sleep(delay)
                        try:
                            # 优先推送 Bus 中的真实事件
                            if status_messages:
                                await ws.send_json({"type": "progress", "content": status_messages[-1]})
                            else:
                                await ws.send_json({"type": "progress", "content": msg})
                        except Exception:
                            break

                notify_task = asyncio.create_task(progressive_notify())

                try:
                    result = await _run_graph(sid, query)
                    notify_task.cancel()

                    # X-Processing-Time 响应头信息嵌入响应体
                    await ws.send_json({
                        "type": "response",
                        "content": result.get("response", ""),
                        "agent": result.get("current_agent", ""),
                        "elapsed": result.get("elapsed", 0),
                        "mode": result.get("collaboration_mode", "sequential"),
                        "cached": result.get("cached", False),
                        "agents_used": result.get("agents_used", []),
                        "processing_time": result.get("elapsed", 0),
                        "resolution_status": result.get("resolution_status", ""),
                        "session_id": sid,  # v3.6: 携带 session_id 供前端同步
                    })
                except Exception as e:
                    notify_task.cancel()
                    logger.error(f"WS 处理失败: {e}", exc_info=True)
                    await ws.send_json({"type": "error", "content": "处理失败，请稍后重试"})

        except WebSocketDisconnect:
            logger.info(f"[WS] 断开: {session_id}")
        finally:
            if _bus:
                _bus.unsubscribe("agent.processing", on_agent_event)
                _bus.unsubscribe("agent.completed", on_agent_event)

    # ------ REST API ------

    @app.post("/api/chat")
    async def rest_chat(data: ChatRequest):
        query = data.query.strip()[:MAX_QUERY_LENGTH]  # v3.4: 输入长度限制
        sid = data.session_id or str(uuid.uuid4())
        if not query:
            return JSONResponse({"error": "query 不能为空"}, status_code=400)
        try:
            result = await _run_graph(sid, query)
            return result
        except Exception as e:
            logger.error(f"REST 处理失败: {e}", exc_info=True)
            return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)

    @app.get("/api/health")
    async def health():
        from multi_agent_customer_service import circuit_breaker as _cb
        return {
            "status": "healthy",
            "version": VERSION,
            "timestamp": time.time(),
            "circuit_breaker": _cb.get_status(),
        }

    @app.get("/api/metrics")
    async def metrics_endpoint():
        """性能监控端点（v3.1: 含 SLA 详情 + 持久化快照）"""
        if _metrics:
            stats = await _metrics.get_stats()
        else:
            stats = {"error": "metrics not initialized"}
        cache_stats = _response_cache.get_stats() if _response_cache else {}

        # 持久化指标快照到 Redis（如可用）
        await _persist_metrics_snapshot()

        return {
            "version": VERSION,
            "metrics": stats,
            "cache": cache_stats,
            "timestamp": time.time(),
        }

    @app.get("/api/kpi")
    async def kpi_endpoint():
        """业务 KPI 端点（v3.1: 含持久化 + 历史趋势）"""
        if _metrics:
            kpi = await _metrics.get_kpi_stats()
        else:
            kpi = {"error": "metrics not initialized"}

        # 持久化指标快照到 Redis（如可用）
        await _persist_metrics_snapshot()

        result = {
            "version": VERSION,
            "kpi": kpi,
            "timestamp": time.time(),
        }

        # 加载历史快照（如 Redis 可用）
        r = _get_redis_client()
        if r and _metrics:
            try:
                snapshot = _metrics.load_snapshot(r)
                if snapshot:
                    result["last_snapshot"] = snapshot
            except Exception:
                pass

        return result

    @app.get("/api/cache/stats")
    async def cache_stats():
        if _response_cache:
            return _response_cache.get_stats()
        return {"error": "cache not initialized"}

    @app.get("/api/sessions")
    async def list_sessions():
        if _session_manager:
            return {"sessions": _session_manager.list_sessions()}
        return {"sessions": []}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        if _session_manager:
            session = _session_manager.get_session(session_id)
            if session:
                return {"session": {
                    "session_id": session_id,
                    "messages": session.get("messages", []),
                    "created_at": session.get("created_at"),
                    "last_activity": session.get("last_activity"),
                    "message_count": session.get("message_count", 0),
                    "summary": session.get("summary", ""),
                }}
            return JSONResponse({"error": "session not found"}, status_code=404)
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        if _session_manager:
            _session_manager.delete_session(session_id)
            return {"message": f"会话 {session_id} 已删除"}
        return JSONResponse({"error": "session manager not initialized"}, status_code=500)

    # ------ v3.2: 客户反馈端点（首次解决率校准）------

    @app.post("/api/feedback")
    async def submit_feedback(data: FeedbackRequest):
        """
        客户满意度反馈端点
        请求体: {"session_id": "xxx", "resolved": true/false, "comment": "可选备注"}
        """
        session_id = data.session_id.strip()
        resolved = data.resolved
        comment = data.comment

        if not session_id:
            return JSONResponse({"error": "session_id 不能为空"}, status_code=400)
        if resolved is None:
            return JSONResponse({"error": "resolved 字段必填（true/false）"}, status_code=400)

        # v3.4: 验证会话存在
        if _session_manager:
            session = _session_manager.get_session(session_id)
            if not session or not session.get("messages"):
                return JSONResponse({"error": "会话不存在或无对话记录"}, status_code=404)

        # 更新 MetricsCollector（v3.6: 通过方法安全获取锁）
        if _metrics:
            await _metrics.record_feedback(resolved=bool(resolved))

        # 广播反馈事件
        if _bus:
            try:
                from core.message_bus import Message, MessageType
                await _bus.publish(Message(
                    msg_type=MessageType.BROADCAST,
                    topic="feedback.received",
                    sender="api_feedback",
                    payload={"session_id": session_id, "resolved": resolved, "comment": comment},
                ))
            except Exception:
                pass

        logger.info(f"[Feedback] session={session_id} resolved={resolved} comment={comment[:50]}")
        return {"status": "ok", "session_id": session_id, "resolved": resolved}

    # ------ v3.2: SLA 告警端点 ------

    @app.get("/api/alerts")
    async def get_alerts(limit: int = 20):
        """获取最近的 SLA 告警记录"""
        if _sla_alert_mgr:
            return {"alerts": _sla_alert_mgr.get_alerts(limit=limit)}
        return {"alerts": [], "message": "alert manager not initialized"}

    @app.get("/api/circuit-breaker")
    async def get_circuit_breaker():
        """获取 LLM 熔断器状态"""
        from multi_agent_customer_service import circuit_breaker
        return {"circuit_breaker": circuit_breaker.get_status()}

    return app


async def _persist_metrics_snapshot():
    """将指标快照持久化到 Redis（静默失败，不影响主流程）"""
    if not _metrics:
        return
    r = _get_redis_client()
    if r:
        try:
            await _metrics.save_snapshot(r)
        except Exception:
            pass


async def _run_graph(session_id: str, query: str) -> Dict[str, Any]:
    """
    执行 LangGraph 图（v3.2: 原生异步 + SLA 告警 + 解决状态追踪）
    缓存逻辑完全由图内 check_cache_node + final_response_node 管理
    """
    start = time.time()

    state = {
        "session_id": session_id,
        "current_agent": "",
        "customer_query": query,
        "query_type": "",
        "response": "",
        "complexity": 0,
        "fast_path": True,
        "collaboration_mode": "",
        "cached": False,
        "agents_used": [],
        "resolution_status": "",
    }

    # v3.0: 使用 ainvoke 原生异步执行
    try:
        result = await _graph_app.ainvoke(state)
    except AttributeError:
        # 兼容不支持 ainvoke 的 LangGraph 版本
        result = await asyncio.to_thread(_graph_app.invoke, state)

    elapsed = time.time() - start
    result["elapsed"] = elapsed

    # SLA 告警：响应时长超出目标范围
    if elapsed > RESPONSE_TIME_TARGET_MAX:
        logger.warning(
            f"[SLA] 响应超时: {elapsed:.2f}s > {RESPONSE_TIME_TARGET_MAX}s "
            f"(session={session_id}, mode={result.get('collaboration_mode', '')}, "
            f"agent={result.get('current_agent', '')})"
        )
    elif elapsed < RESPONSE_TIME_TARGET_MIN:
        logger.info(
            f"[SLA] 响应偏快: {elapsed:.2f}s < {RESPONSE_TIME_TARGET_MIN}s "
            f"(session={session_id}, cached={result.get('cached', False)})"
        )

    # 采集指标（v3.4: record_request 改为 async）
    if _metrics:
        await _metrics.record_request(
            elapsed=elapsed,
            agent=result.get("current_agent", ""),
            mode=result.get("collaboration_mode", ""),
            cached=result.get("cached", False),
            session_id=session_id,
            escalated=result.get("collaboration_mode", "") == "hierarchical",
            resolution_status=result.get("resolution_status", ""),
        )
        # v3.2: SLA 告警检查（异步，不阻塞响应）
        if _sla_alert_mgr:
            try:
                await _sla_alert_mgr.check_and_alert(_metrics)
            except Exception:
                pass

    return result
