"""
WebSocket 实时对话路由
从 api/app.py create_app() 提取，通过 request.app.state 访问依赖。
"""

import asyncio
import hmac
import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.utils import sanitize_input, validate_session_id
from core.config import (
    API_KEY,
    API_KEY_ENABLED,
    DEV_MODE,
    MAX_QUERY_LENGTH,
    WS_IDLE_TIMEOUT,
    WS_MAX_CONNECTIONS_PER_IP,
    WS_MESSAGE_RATE_LIMIT,
)
from core.logger import get_logger

router = APIRouter()
logger = get_logger("api.ws")

# ── WebSocket 连接跟踪 ──
_ws_connections: dict[str, int] = defaultdict(int)
_ws_lock = asyncio.Lock()
_ws_conn_counter = 0


def cleanup_stale_ws_connections():
    """清理连接数为 0 的条目，防止内存泄漏"""
    stale_keys = [ip for ip, count in _ws_connections.items() if count <= 0]
    for key in stale_keys:
        del _ws_connections[key]
    if stale_keys:
        logger.info(f"[WS] 清理 {len(stale_keys)} 个过期连接记录")
    return len(stale_keys)


async def periodic_ws_cleanup():
    """周期性清理过期的 WebSocket 连接记录（每 5 分钟），由 lifespan 启动"""
    while True:
        await asyncio.sleep(300)
        try:
            async with _ws_lock:
                cleaned = cleanup_stale_ws_connections()
            if cleaned:
                logger.debug(f"[WS] 周期性清理: 移除 {cleaned} 个过期条目")
        except Exception as e:
            logger.warning(f"[WS] 周期性清理异常: {e}")


# ── WebSocket 认证 ──


async def _ws_authenticate(ws: WebSocket, ws_api_key: str) -> tuple:
    """WebSocket 首条消息认证。

    修复历史:
    - v5.3: 移除 DEV_MODE 短路（原审计 v2 P0 A-1 违规要求），所有连接强制认证。
      API Key（query/header/message）优先；否则要求 msg.token (JWT) + msg.session_token。
      返回 (ws_api_key, session_token, ws_jwt_payload) 或抛出异常。
    - v5.4.2: 恢复 DEV_MODE 认证绕过（开发环境无 JWT 时允许匿名连接），
      解决开发模式下 WebSocket 反复断开重连问题。
    """
    # 开发模式：允许匿名连接（无 JWT 时跳过认证）
    if DEV_MODE:
        return "", "", None

    # 检查 query param / header 提供的 API Key
    if ws_api_key and API_KEY_ENABLED:
        if hmac.compare_digest(ws_api_key, API_KEY):
            return ws_api_key, "", None
        await ws.send_json({"type": "error", "message": "认证失败: 无效的 api_key"})
        await ws.close(code=4001, reason="Invalid API key")
        raise ValueError("Invalid API key")

    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        msg_api_key = auth_msg.get("api_key", "")
        if msg_api_key and API_KEY_ENABLED and hmac.compare_digest(msg_api_key, API_KEY):
            return msg_api_key, "", None

        ws_jwt = auth_msg.get("token", "")
        session_token = auth_msg.get("session_token", "")
        if not ws_jwt:
            await ws.send_json({"type": "error", "message": "认证失败: 缺少 token 或 api_key"})
            await ws.close(code=4001, reason="Unauthorized")
            raise ValueError("Missing credentials")

        from auth.service import decode_token

        payload = decode_token(ws_jwt)
        if not payload:
            await ws.send_json({"type": "error", "message": "认证失败: 无效的 token"})
            await ws.close(code=4001, reason="Invalid token")
            raise ValueError("Invalid token")

        return ws_api_key, session_token, payload
    except asyncio.TimeoutError:
        await ws.send_json({"type": "error", "message": "认证超时"})
        await ws.close(code=4002, reason="Auth timeout")
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.debug(f"[WS] 认证异常: {e}")
        await ws.send_json({"type": "error", "message": "认证失败"})
        await ws.close(code=4003, reason="Auth error")
        raise


# ── WebSocket 实时对话 ──


@router.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket):
    global _ws_conn_counter

    ws_api_key = ws.query_params.get("api_key", "") or ws.headers.get("x-api-key", "")
    client_ip = ws.client.host if ws.client else "unknown"

    async with _ws_lock:
        if _ws_connections[client_ip] >= WS_MAX_CONNECTIONS_PER_IP:
            await ws.close(code=4029, reason="Too many connections")
            return
        _ws_connections[client_ip] += 1

    await ws.accept()

    try:
        ws_api_key, session_token, ws_jwt_payload = await _ws_authenticate(ws, ws_api_key)
    except Exception as e:
        logger.debug(f"[WS] 认证流程异常: {e}")
        async with _ws_lock:
            _ws_connections[client_ip] = max(0, _ws_connections[client_ip] - 1)
        return

    # 从 app.state 获取依赖
    state = ws.app.state
    session_manager = state.session_manager
    bus = state.message_bus
    run_graph = state.run_graph

    session_id = str(uuid.uuid4())
    if session_manager and not session_token:
        session_token = session_manager.generate_session_token(session_id)
    logger.info(f"[WS] 新连接: {session_id} ip={client_ip}")

    # v5.3: H-1 修复 — 把 JWT 里的 user_id 提到外层，确保 run_graph() 与 quota 检查能拿到
    # （审计 v2 P0 H-1：WS 路径未注入 user_id 导致钱包枯竭攻击防护失效）
    ws_uid = ""
    if session_manager and ws_jwt_payload:
        ws_uid = ws_jwt_payload.get("sub", "")
        if ws_uid:
            session_manager.set_user_id(session_id, ws_uid)

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
        except Exception as e:
            logger.debug(f"Bus 事件处理异常: {e}")

    if bus:
        await bus.subscribe("agent.processing", on_agent_event)
        await bus.subscribe("agent.completed", on_agent_event)

    msg_timestamps: list = []

    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_json(), timeout=WS_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                    try:
                        data = await asyncio.wait_for(ws.receive_json(), timeout=WS_IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await ws.send_json({"type": "error", "content": "连接空闲超时，请重新连接"})
                        await ws.close(code=4008, reason="Idle timeout")
                        break
                except Exception as e:
                    logger.debug(f"[WS] ping 发送失败，关闭连接: {e}")
                    await ws.close(code=4008, reason="Idle timeout")
                    break

            now = time.time()
            msg_timestamps = [t for t in msg_timestamps if now - t < 60]

            if data.get("type") == "auth":
                logger.debug(f"[WS] 跳过认证消息: session={session_id}")
                continue

            if data.get("type") == "pong":
                continue

            if len(msg_timestamps) >= WS_MESSAGE_RATE_LIMIT:
                await ws.send_json({"type": "error", "content": "消息发送过于频繁，请稍后再试"})
                continue
            msg_timestamps.append(now)

            query = data.get("query", "").strip()[:MAX_QUERY_LENGTH]
            query = sanitize_input(query)
            sid = validate_session_id(data.get("session_id", ""))

            if sid != session_id and session_manager:
                token = data.get("session_token", "")
                if not session_manager.validate_session_token(sid, token):
                    await ws.send_json({"type": "error", "content": "会话令牌无效"})
                    continue
            if not query:
                await ws.send_json({"type": "error", "content": "查询不能为空"})
                continue

            await ws.send_json({"type": "status", "content": "正在分析您的问题..."})
            status_messages.clear()

            async def progressive_notify():
                delays = [0.3, 0.5, 1.0, 1.5]
                messages = ["正在为您查询...", "正在处理中...", "请稍候...", "即将完成..."]
                for delay, msg in zip(delays, messages, strict=False):
                    await asyncio.sleep(delay)
                    try:
                        if status_messages:
                            await ws.send_json({"type": "progress", "content": status_messages[-1]})
                        else:
                            await ws.send_json({"type": "progress", "content": msg})
                    except Exception as e:
                        logger.debug(f"[WS] 进度通知发送失败: {e}")
                        break

            notify_task = asyncio.create_task(progressive_notify())

            try:
                result = await run_graph(sid, query, user_id=ws_uid)
                notify_task.cancel()

                await ws.send_json(
                    {
                        "type": "response",
                        "content": result.get("response", ""),
                        "agent": result.get("current_agent", ""),
                        "elapsed": result.get("elapsed", 0),
                        "mode": result.get("collaboration_mode", "sequential"),
                        "cached": result.get("cached", False),
                        "agents_used": result.get("agents_used", []),
                        "processing_time": result.get("elapsed", 0),
                        "resolution_status": result.get("resolution_status", ""),
                        "session_id": sid,
                        "session_token": session_token,
                    }
                )
            except Exception as e:
                notify_task.cancel()
                logger.error(f"WS 处理失败: {e}", exc_info=False)
                await ws.send_json({"type": "error", "content": "处理失败，请稍后重试"})

    except WebSocketDisconnect:
        logger.info(f"[WS] 断开: {session_id}")
    finally:
        async with _ws_lock:
            _ws_connections[client_ip] = max(0, _ws_connections[client_ip] - 1)
            _ws_conn_counter += 1
            if _ws_conn_counter >= 50:
                _ws_conn_counter = 0
                cleanup_stale_ws_connections()
        if bus:
            try:
                await bus.unsubscribe("agent.processing", on_agent_event)
                await bus.unsubscribe("agent.completed", on_agent_event)
            except Exception as e:
                logger.warning(f"[WS] Bus 取消订阅失败 session={session_id}: {e}")
