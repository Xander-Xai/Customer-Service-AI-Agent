/**
 * API 对接层 - 封装所有后端接口调用
 * 包含 WebSocket 连接管理、断线重连、消息队列、REST 接口封装
 *
 * v3.6 修复：
 * - 消息队列：未连接时暂存消息，连接后自动发送
 * - 连接状态可视化反馈（事件驱动）
 * - 无限重连（指数退避，最大 30s）
 * - 重连计数在连接成功时重置
 */
const API = (() => {
  // ===== 配置 =====
  const WS_RECONNECT_BASE = 2000;   // 初始重连延迟（ms）
  const WS_RECONNECT_MAX = 30000;   // 最大重连延迟（ms）

  // v3.8: 本地开发允许无 API Key 连接（生产环境请配置 API_KEY）
  const API_KEY = localStorage.getItem('api_key') || '';

  // ===== WebSocket 管理 =====
  let _ws = null;
  let _reconnectCount = 0;
  let _reconnectTimer = null;
  let _listeners = {};
  let _sessionId = null;
  let _pendingMessages = [];  // v3.6: 消息队列（连接未就绪时暂存）
  let _connectionReady = false;
  let _heartbeatTimer = null;  // v3.8: 心跳定时器
  const _HEARTBEAT_INTERVAL = 30000;  // v3.8: 客户端主动心跳间隔（30秒）

  /**
   * v3.8: 启动心跳定时器
   */
  function _startHeartbeat() {
    _stopHeartbeat();
    _heartbeatTimer = setInterval(() => {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        try {
          _ws.send(JSON.stringify({ type: 'ping' }));
        } catch (e) {
          console.error('[WS] 心跳发送失败:', e);
        }
      }
    }, _HEARTBEAT_INTERVAL);
  }

  /**
   * v3.8: 停止心跳定时器
   */
  function _stopHeartbeat() {
    if (_heartbeatTimer) {
      clearInterval(_heartbeatTimer);
      _heartbeatTimer = null;
    }
  }

  /**
   * 建立 WebSocket 连接
   * @param {string} sessionId - 会话 ID（可选，服务端会生成默认值）
   * @returns {WebSocket}
   */
  function connectWS(sessionId) {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
      return _ws;
    }

    _sessionId = sessionId;
    _connectionReady = false;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/chat`;

    try {
      // v3.8: 认证支持：通过 URL 参数传递 API Key
      const authUrl = API_KEY ? `${wsUrl}?api_key=${encodeURIComponent(API_KEY)}` : wsUrl;
      _ws = new WebSocket(authUrl);
      console.log('[WS] 连接中...' + (API_KEY ? ' (已传 API Key)' : ' (无认证)'));
    } catch (e) {
      console.error('[WS] 创建连接失败:', e);
      _emit('ws_error', { error: e });
      _scheduleReconnect();
      return null;
    }

    _ws.onopen = () => {
      _reconnectCount = 0;  // v3.6: 连接成功重置计数
      _connectionReady = true;
      _emit('connected', { sessionId: _sessionId });
      console.log('[WS] 已连接');
      // v3.6: 发送队列中的暂存消息
      _flushPendingMessages();
      // v3.8: 启动心跳
      _startHeartbeat();
    };

    _ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // v3.8: 心跳响应 — 服务端发送 ping，客户端回复 pong 保持连接
        if (data.type === 'ping') {
          if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify({ type: 'pong' }));
          }
          return;
        }
        _emit(data.type, data);
      } catch (e) {
        console.error('[WS] 消息解析失败:', e);
      }
    };

    _ws.onclose = (event) => {
      _connectionReady = false;
      _stopHeartbeat();  // v3.8: 停止心跳
      _emit('disconnected', { code: event.code, reason: event.reason });
      console.log('[WS] 断开连接', event.code, event.reason);

      // v3.6: 非手动关闭时自动重连（无次数上限，指数退避）
      if (event.code !== 1000) {
        _scheduleReconnect();
      }
    };

    _ws.onerror = (error) => {
      _emit('ws_error', { error });
      console.error('[WS] 连接错误:', error);
    };

    return _ws;
  }

  /**
   * v3.6: 调度重连（指数退避，上限 30s）
   */
  function _scheduleReconnect() {
    if (_reconnectTimer) return;  // 避免重复调度
    _reconnectCount++;
    const delay = Math.min(WS_RECONNECT_BASE * Math.pow(1.5, _reconnectCount - 1), WS_RECONNECT_MAX);
    console.log(`[WS] ${Math.round(delay / 1000)}s 后重连 (第${_reconnectCount}次)`);
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      connectWS(_sessionId);
    }, delay);
  }

  /**
   * v3.6: 连接就绪后发送队列中的暂存消息
   */
  function _flushPendingMessages() {
    if (!_pendingMessages.length) return;
    console.log(`[WS] 发送 ${_pendingMessages.length} 条暂存消息`);
    const messages = [..._pendingMessages];
    _pendingMessages = [];
    for (const msg of messages) {
      try {
        _ws.send(JSON.stringify(msg));
      } catch (e) {
        console.error('[WS] 发送暂存消息失败:', e);
        _pendingMessages.push(msg);  // 重新入队
      }
    }
  }

  /**
   * 通过 WebSocket 发送消息
   * v3.6: 未连接时自动暂存到队列，连接后自动发送
   * @param {string} query - 用户提问
   * @param {string} sessionId - 会话 ID（可选）
   */
  function sendWSMessage(query, sessionId, sessionToken) {
    const payload = {
      query: query,
      session_id: sessionId || _sessionId || undefined,
      session_token: sessionToken || undefined  // v3.8: 会话所有权令牌
    };

    if (!_ws || _ws.readyState !== WebSocket.OPEN) {
      // v3.6: 暂存消息而非丢弃
      _pendingMessages.push(payload);
      _emit('pending', { content: '连接未就绪，消息已暂存，连接恢复后自动发送...' });
      // 尝试重连
      if (!_ws || _ws.readyState === WebSocket.CLOSED) {
        connectWS(sessionId);
      }
      return;
    }

    try {
      _ws.send(JSON.stringify(payload));
    } catch (e) {
      console.error('[WS] 发送失败:', e);
      _pendingMessages.push(payload);
      _emit('error', { content: '消息发送失败，已暂存' });
    }
  }

  /**
   * 关闭 WebSocket 连接
   */
  function disconnectWS() {
    _stopHeartbeat();  // v3.8: 停止心跳
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }
    _reconnectCount = 999; // 阻止自动重连
    _connectionReady = false;
    if (_ws) {
      _ws.close(1000, '用户主动断开');
      _ws = null;
    }
  }

  /**
   * 获取当前连接状态
   */
  function isConnected() {
    return _connectionReady && _ws && _ws.readyState === WebSocket.OPEN;
  }

  // ===== 事件系统 =====
  function on(event, callback) {
    if (!_listeners[event]) _listeners[event] = [];
    _listeners[event].push(callback);
    return () => off(event, callback); // 返回取消订阅函数
  }

  function off(event, callback) {
    if (_listeners[event]) {
      _listeners[event] = _listeners[event].filter(cb => cb !== callback);
    }
  }

  function _emit(event, data) {
    (_listeners[event] || []).forEach(cb => {
      try { cb(data); } catch (e) { console.error(`[Event:${event}] 回调异常:`, e); }
    });
  }

  // ===== REST API 封装 =====

  /**
   * 通用请求方法（v3.6: API Key 认证支持）
   * v3.8: 支持 X-Session-Token header（会话所有权校验）
   */
  async function _request(method, path, body = null, extraHeaders = {}) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json', ...extraHeaders },
    };
    if (body) opts.body = JSON.stringify(body);

    const resp = await fetch(path, opts);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: resp.statusText }));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return resp.json();
  }

  /** 健康检查 */
  function getHealth() { return _request('GET', '/api/health'); }

  /** 性能监控 */
  function getMetrics() { return _request('GET', '/api/metrics'); }

  /** 业务 KPI */
  function getKPI() { return _request('GET', '/api/kpi'); }

  /** 缓存统计 */
  function getCacheStats() { return _request('GET', '/api/cache/stats'); }

  /** 会话列表 */
  function getSessions() { return _request('GET', '/api/sessions'); }

  /** 会话详情（v3.8: 携带 session_token 用于所有权校验） */
  function getSession(sessionId) {
    const token = localStorage.getItem('currentSessionToken') || '';
    return _request('GET', `/api/sessions/${sessionId}`, null,
      token ? { 'X-Session-Token': token } : {});
  }

  /** 删除会话（v3.8: 携带 session_token） */
  function deleteSession(sessionId) {
    const token = localStorage.getItem('currentSessionToken') || '';
    return _request('DELETE', `/api/sessions/${sessionId}`, null,
      token ? { 'X-Session-Token': token } : {});
  }

  /** SLA 告警 */
  function getAlerts(limit = 20) { return _request('GET', `/api/alerts?limit=${limit}`); }

  /** 熔断器状态 */
  function getCircuitBreaker() { return _request('GET', '/api/circuit-breaker'); }

  /** 提交反馈 */
  function submitFeedback(sessionId, resolved, comment = '') {
    return _request('POST', '/api/feedback', { session_id: sessionId, resolved, comment });
  }

  /** REST 对话（备用，主用 WebSocket） */
  function sendChat(query, sessionId) {
    return _request('POST', '/api/chat', { query, session_id: sessionId });
  }

  // ===== 公开接口 =====
  return {
    // WebSocket
    connect: connectWS,
    send: sendWSMessage,
    disconnect: disconnectWS,
    isConnected,

    // 事件系统
    on,
    off,

    // REST API
    getHealth,
    getMetrics,
    getKPI,
    getCacheStats,
    getSessions,
    getSession,
    deleteSession,
    getAlerts,
    getCircuitBreaker,
    submitFeedback,
    sendChat,
  };
})();
