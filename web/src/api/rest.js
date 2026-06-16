/**
 * REST API 封装
 */

import { fetchWithAuth } from '../auth/index.js';

/** 通用请求方法（JWT + API Key 双认证，支持 401 自动刷新） */
async function _request(method, path, body = null, extraHeaders = {}) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  };
  const apiKey = localStorage.getItem('api_key') || '';
  if (apiKey) opts.headers['X-API-Key'] = apiKey;
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetchWithAuth(path, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: resp.statusText }));
    throw new Error(err.error || err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export function getHealth() {
  return _request('GET', '/api/health');
}
export function getMetrics() {
  return _request('GET', '/api/metrics');
}
export function getKPI() {
  return _request('GET', '/api/kpi');
}
export function getCacheStats() {
  return _request('GET', '/api/cache/stats');
}
export function getSessions() {
  return _request('GET', '/api/sessions');
}

export function getSession(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request(
    'GET',
    `/api/sessions/${sessionId}`,
    null,
    token ? { 'X-Session-Token': token } : {},
  );
}

export function deleteSession(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request(
    'DELETE',
    `/api/sessions/${sessionId}`,
    null,
    token ? { 'X-Session-Token': token } : {},
  );
}

export function getAlerts(limit = 20) {
  return _request('GET', `/api/alerts?limit=${limit}`);
}
export function getCircuitBreaker() {
  return _request('GET', '/api/circuit-breaker');
}

export function submitFeedback(sessionId, resolved, comment = '') {
  return _request('POST', '/api/feedback', { session_id: sessionId, resolved, comment });
}

export function submitRating(sessionId, rating, messageIndex = 0, comment = '') {
  return _request('POST', '/api/feedback', {
    session_id: sessionId,
    rating,
    resolved: rating > 0,
    message_index: messageIndex,
    comment,
  });
}

export function getFeedbackStats() {
  return _request('GET', '/api/feedback/stats');
}

/** 多模态对话（图片 + 文字） */
export function sendChatWithImage(query, imageFile, sessionId) {
  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('query', query || '');
  formData.append('session_id', sessionId || '');

  const headers = {};
  const apiKey = localStorage.getItem('api_key') || '';
  if (apiKey) headers['X-API-Key'] = apiKey;

  return fetchWithAuth('/api/chat/image', { method: 'POST', headers, body: formData }).then(
    async (resp) => {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || err.detail || err.message || `HTTP ${resp.status}`);
      }
      return resp.json();
    },
  );
}

/** REST 对话（备用） */
export function sendChat(query, sessionId) {
  const sessionToken = localStorage.getItem('currentSessionToken') || '';
  return _request('POST', '/api/chat', {
    query,
    session_id: sessionId,
    session_token: sessionToken,
  });
}

/** 统一文件上传对话（图片/视频/PDF/DOCX/文本） */
export function sendChatWithFile(query, file, sessionId) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('query', query || '');
  formData.append('session_id', sessionId || '');

  const headers = {};
  const apiKey = localStorage.getItem('api_key') || '';
  if (apiKey) headers['X-API-Key'] = apiKey;

  return fetchWithAuth('/api/chat/file', { method: 'POST', headers, body: formData }).then(
    async (resp) => {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || err.detail || err.message || `HTTP ${resp.status}`);
      }
      return resp.json();
    },
  );
}

// ===== 监控 API =====

/** 最近7天质量评分趋势 */
export function getQualityTrends() {
  return _request('GET', '/api/monitoring/quality-trends');
}

/** 热门问题 TOP10 */
export function getHotQuestions() {
  return _request('GET', '/api/monitoring/hot-questions');
}

/** 客户满意度统计 */
export function getSatisfaction() {
  return _request('GET', '/api/monitoring/satisfaction');
}

// ===== 管理后台 API =====

/** 获取用户列表（管理员） */
export function getUsers() {
  return _request('GET', '/api/auth/users');
}

/** 获取审计日志（管理员） */
export function getAuditLog(limit = 30) {
  return _request('GET', `/api/auth/audit?limit=${limit}`);
}

/** 获取知识库统计 */
export function getKnowledgeStats() {
  return _request('GET', '/api/knowledge/stats');
}

/** 重新种子知识库 */
export function seedKnowledge() {
  return _request('POST', '/api/knowledge/seed');
}

/** 从 ERP 同步知识库 */
export function syncKnowledge() {
  return _request('POST', '/api/knowledge/sync');
}

/** 获取告警配置 */
export function getAlertConfig() {
  return _request('GET', '/api/alerts/config');
}

/** 发送测试告警 */
export function testAlert(title, content, severity) {
  return _request('POST', '/api/alerts/test', { title, content, severity });
}

/** Token 刷新 */
export function refreshToken(refreshTokenValue) {
  return _request('POST', '/api/auth/refresh', { refresh_token: refreshTokenValue });
}

// ===== Prompt 管理 API =====

/** 获取有 Prompt 版本的 Agent 列表 */
export function getPromptAgents() {
  return _request('GET', '/api/admin/prompts/agents');
}

/** 获取指定 Agent 的所有 Prompt 版本 */
export function getPromptVersions(agentName) {
  return _request('GET', `/api/admin/prompts/${agentName}`);
}

/** 创建 Prompt 版本 */
export function createPromptVersion(agentName, promptText, version, activate = true) {
  return _request('POST', `/api/admin/prompts/${agentName}`, {
    prompt_text: promptText,
    version,
    activate,
  });
}

/** 激活 Prompt 版本 */
export function activatePromptVersion(agentName, version) {
  return _request('PUT', `/api/admin/prompts/${agentName}/activate`, { version });
}

/** 获取当前活跃 Prompt 版本 */
export function getActivePrompt(agentName) {
  return _request('GET', `/api/admin/prompts/${agentName}/active`);
}

// ===== 用户角色管理 API =====

/** 修改用户角色 */
export function updateUserRole(userId, role) {
  return _request('PUT', `/api/auth/users/${userId}/role`, { role });
}

// ===== 知识库文档管理 API =====

/** 向知识库添加文档 */
export function addKnowledgeDocs(collection, documents, metadatas = null) {
  return _request('POST', `/api/knowledge/${collection}/add`, { documents, metadatas });
}

// ===== 告警历史 API =====

/** 获取告警历史 */
export function getAlertHistory(limit = 20) {
  return _request('GET', `/api/alerts/history?limit=${limit}`);
}

// ===== Token 用量 API =====

/** 获取 LLM Token 用量统计 */
export function getTokenUsage() {
  return _request('GET', '/api/monitoring/tokens');
}

// ===== 用户认证 API =====

/** 获取当前用户信息 */
export function getUserMe() {
  return _request('GET', '/api/auth/me');
}

// ===== TTS 语音列表 API =====

/** 获取可用 TTS 语音列表 */
export function getTTSVoices() {
  return _request('GET', '/api/tts/voices');
}

// ===== 语音服务 API（通过 fetchWithAuth 认证） =====

/**
 * 发送语音文件到 ASR + 对话管道
 * 审计 v2 Task 2.3: voice.js 改用 fetchWithAuth 以通过中央认证拦截器
 */
export function sendVoiceForm(formData) {
  return fetchWithAuth('/api/chat/voice', {
    method: 'POST',
    body: formData,
  });
}

/**
 * 文本转语音（TTS）
 */
export function sendTTS(text, voice = '') {
  const formData = new FormData();
  formData.append('text', text.substring(0, 2000));
  if (voice) formData.append('voice', voice);

  return fetchWithAuth('/api/tts', {
    method: 'POST',
    body: formData,
  });
}
