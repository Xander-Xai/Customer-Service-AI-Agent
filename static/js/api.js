/**
 * API 对接层 - 封装所有后端接口调用
 * 包含 WebSocket 连接管理、断线重连、REST 接口封装
 */
const API = (() => {
  // ===== 配置 =====
  const WS_RECONNECT_DELAY = 3000;  // 断线重连延迟（ms）
  const WS_MAX_RECONNECT = 5;       // 最大重连次数

  // ===== WebSocket 管理 =====
  let _ws = null;
  let _reconnectCount = 0;
  let _reconnectTimer = null;
  let _listeners = {};
  let _sessionId = null;

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
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/chat`;

    _ws = new WebSocket(wsUrl);

    _ws.onopen = () => {
      _reconnectCount = 0;
      _emit('connected', { sessionId: _sessionId });
      console.log('[WS] 已连接');
    };

    _ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        _emit(data.type, data);
      } catch (e) {
        console.error('[WS] 消息解析失败:', e);
      }
    };

    _ws.onclose = (event) => {
      _emit('disconnected', { code: event.code, reason: event.reason });
      console.log('[WS] 断开连接', event.code);

      // 自动重连（非手动关闭）
      if (event.code !== 1000 && _reconnectCount < WS_MAX_RECONNECT) {
        _reconnectCount++;
        const delay = WS_RECONNECT_DELAY * _reconnectCount;
        console.log(`[WS] ${delay / 1000}s 后重连 (${_reconnectCount}/${WS_MAX_RECONNECT})`);
        _reconnectTimer = setTimeout(() => connectWS(_sessionId), delay);
      }
    };

    _ws.onerror = (error) => {
      _emit('ws_error', { error });
      console.error('[WS] 连接错误:', error);
    };

    return _ws;
  }

  /**
   * 通过 WebSocket 发送消息
   * @param {string} query - 用户提问
   * @param {string} sessionId - 会话 ID（可选）
   */
  function sendWSMessage(query, sessionId) {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) {
      _emit('error', { content: '连接未就绪，正在重连...' });
      connectWS(sessionId);
      return;
    }
    _ws.send(JSON.stringify({
      query: query,
      session_id: sessionId || _sessionId || undefined
    }));
  }

  /**
   * 关闭 WebSocket 连接
   */
  function disconnectWS() {
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }
    _reconnectCount = WS_MAX_RECONNECT; // 阻止自动重连
    if (_ws) {
      _ws.close(1000, '用户主动断开');
      _ws = null;
    }
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
   * 通用请求方法
   */
  async function _request(method, path, body = null) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
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

  /** 会话详情 */
  function getSession(sessionId) { return _request('GET', `/api/sessions/${sessionId}`); }

  /** 删除会话 */
  function deleteSession(sessionId) { return _request('DELETE', `/api/sessions/${sessionId}`); }

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
