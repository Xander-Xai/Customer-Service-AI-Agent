/**
 * WebSocket 连接管理
 * 断线重连（指数退避）、消息队列、心跳
 */
import { emit } from './events.js';

const WS_RECONNECT_BASE = 2000;
const WS_RECONNECT_MAX = 30000;
const HEARTBEAT_INTERVAL = 30000;

let _ws = null;
let _reconnectCount = 0;
let _reconnectTimer = null;
let _pendingMessages = [];
let _connectionReady = false;
let _heartbeatTimer = null;
let _sessionId = null;

/** 启动心跳 */
function _startHeartbeat() {
  _stopHeartbeat();
  _heartbeatTimer = setInterval(() => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(JSON.stringify({ type: 'pong' })); }
      catch (e) { console.error('[WS] 心跳发送失败:', e); }
    }
  }, HEARTBEAT_INTERVAL);
}

/** 停止心跳 */
function _stopHeartbeat() {
  if (_heartbeatTimer) { clearInterval(_heartbeatTimer); _heartbeatTimer = null; }
}

/** 建立 WebSocket 连接 */
export function connect(sessionId) {
  if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
    return _ws;
  }

  _sessionId = sessionId;
  _connectionReady = false;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${location.host}/ws/chat`;

  try {
    _ws = new WebSocket(wsUrl);
    console.log('[WS] 连接中...');
  } catch (e) {
    console.error('[WS] 创建连接失败:', e);
    emit('ws_error', { error: e });
    _scheduleReconnect();
    return null;
  }

  _ws.onopen = () => {
    _reconnectCount = 0;
    _connectionReady = true;

    // 通过首条消息发送认证信息（JWT + API Key，避免 URL 泄露）
    const jwtToken = localStorage.getItem('token');
    const apiKey = localStorage.getItem('api_key') || '';
    const authPayload = { type: 'auth' };
    if (jwtToken) authPayload.token = jwtToken;
    if (apiKey) authPayload.api_key = apiKey;
    try { _ws.send(JSON.stringify(authPayload)); }
    catch (e) { console.error('[WS] 发送认证消息失败:', e); }

    emit('connected', { sessionId: _sessionId });
    console.log('[WS] 已连接');
    _flushPendingMessages();
    _startHeartbeat();
  };

  _ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'ping') {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify({ type: 'pong' }));
        }
        return;
      }
      emit(data.type, data);
    } catch (e) {
      console.error('[WS] 消息解析失败:', e);
    }
  };

  _ws.onclose = (event) => {
    _connectionReady = false;
    _stopHeartbeat();
    emit('disconnected', { code: event.code, reason: event.reason });
    console.log('[WS] 断开连接', event.code, event.reason);
    if (event.code !== 1000) _scheduleReconnect();
  };

  _ws.onerror = (error) => {
    emit('ws_error', { error });
    console.error('[WS] 连接错误:', error);
  };

  return _ws;
}

/** 调度重连（指数退避，上限 30s） */
function _scheduleReconnect() {
  if (_reconnectTimer) return;
  _reconnectCount++;
  const delay = Math.min(WS_RECONNECT_BASE * Math.pow(1.5, _reconnectCount - 1), WS_RECONNECT_MAX);
  console.log(`[WS] ${Math.round(delay / 1000)}s 后重连 (第${_reconnectCount}次)`);
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    connect(_sessionId);
  }, delay);
}

/** 发送队列中的暂存消息 */
function _flushPendingMessages() {
  if (!_pendingMessages.length) return;
  console.log(`[WS] 发送 ${_pendingMessages.length} 条暂存消息`);
  const messages = [..._pendingMessages];
  _pendingMessages = [];
  for (const msg of messages) {
    try { _ws.send(JSON.stringify(msg)); }
    catch (e) { console.error('[WS] 发送暂存消息失败:', e); _pendingMessages.push(msg); }
  }
}

/** 通过 WebSocket 发送消息 */
export function send(query, sessionId, sessionToken) {
  const payload = {
    query,
    session_id: sessionId || _sessionId || undefined,
    session_token: sessionToken || undefined,
  };

  if (!_ws || _ws.readyState !== WebSocket.OPEN) {
    _pendingMessages.push(payload);
    emit('pending', { content: '连接未就绪，消息已暂存，连接恢复后自动发送...' });
    if (!_ws || _ws.readyState === WebSocket.CLOSED) connect(sessionId);
    return;
  }

  try { _ws.send(JSON.stringify(payload)); }
  catch (e) {
    console.error('[WS] 发送失败:', e);
    _pendingMessages.push(payload);
    emit('error', { content: '消息发送失败，已暂存' });
  }
}

/** 关闭 WebSocket 连接 */
export function disconnect() {
  _stopHeartbeat();
  if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
  _reconnectCount = 999;
  _connectionReady = false;
  if (_ws) { _ws.close(1000, '用户主动断开'); _ws = null; }
}

/** 获取连接状态 */
export function isConnected() {
  return _connectionReady && _ws && _ws.readyState === WebSocket.OPEN;
}
