/**
 * REST API 封装
 */

/** 通用请求方法（JWT + API Key 双认证） */
async function _request(method, path, body = null, extraHeaders = {}) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  };
  const token = localStorage.getItem('token');
  if (token) {
    opts.headers['Authorization'] = 'Bearer ' + token;
  } else {
    const apiKey = localStorage.getItem('api_key') || '';
    if (apiKey) opts.headers['X-API-Key'] = apiKey;
  }
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: resp.statusText }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export function getHealth() { return _request('GET', '/api/health'); }
export function getMetrics() { return _request('GET', '/api/metrics'); }
export function getKPI() { return _request('GET', '/api/kpi'); }
export function getCacheStats() { return _request('GET', '/api/cache/stats'); }
export function getSessions() { return _request('GET', '/api/sessions'); }

export function getSession(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request('GET', `/api/sessions/${sessionId}`, null,
    token ? { 'X-Session-Token': token } : {});
}

export function deleteSession(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request('DELETE', `/api/sessions/${sessionId}`, null,
    token ? { 'X-Session-Token': token } : {});
}

export function getAlerts(limit = 20) { return _request('GET', `/api/alerts?limit=${limit}`); }
export function getCircuitBreaker() { return _request('GET', '/api/circuit-breaker'); }

export function submitFeedback(sessionId, resolved, comment = '') {
  return _request('POST', '/api/feedback', { session_id: sessionId, resolved, comment });
}

export function submitRating(sessionId, rating, comment = '') {
  return _request('POST', '/api/feedback', { session_id: sessionId, rating, resolved: rating > 0, comment });
}

export function getFeedbackStats() { return _request('GET', '/api/feedback/stats'); }
export function getHistory() { return _request('GET', '/api/history'); }

export function getHistoryMessages(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request('GET', `/api/history/${sessionId}/messages`, null,
    token ? { 'X-Session-Token': token } : {});
}

/** 多模态对话（图片 + 文字） */
export function sendChatWithImage(query, imageFile, sessionId) {
  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('query', query || '');
  formData.append('session_id', sessionId || '');

  const headers = {};
  const token = localStorage.getItem('token');
  if (token) {
    headers['Authorization'] = 'Bearer ' + token;
  } else {
    const apiKey = localStorage.getItem('api_key') || '';
    if (apiKey) headers['X-API-Key'] = apiKey;
  }

  return fetch('/api/chat/image', { method: 'POST', headers, body: formData })
    .then(async resp => {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || `HTTP ${resp.status}`);
      }
      return resp.json();
    });
}

/** REST 对话（备用） */
export function sendChat(query, sessionId) {
  return _request('POST', '/api/chat', { query, session_id: sessionId });
}
