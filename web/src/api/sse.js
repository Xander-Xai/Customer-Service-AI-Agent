/**
 * SSE 流式对话
 */

import { fetchWithAuth } from '../auth/index.js';

/**
 * 发送流式对话请求
 * @param {string} query - 用户提问
 * @param {string} sessionId - 会话 ID
 * @param {string} sessionToken - 会话令牌
 * @param {object} callbacks - { onChunk, onContentComplete, onDone, onError, onStatus, onThinking, onToolCall, onToolResult, onRagStatus, onAgentSwitch }
 * @returns {AbortController} 用于取消请求
 */
export function sendChatStream(query, sessionId, sessionToken, callbacks = {}) {
  const {
    onChunk,
    onContentComplete,
    onDone,
    onError,
    onStatus,
    onThinking,
    onToolCall,
    onToolResult,
    onRagStatus,
    onAgentSwitch,
  } = callbacks;
  const controller = new AbortController();
  let sawDone = false;

  const headers = { 'Content-Type': 'application/json' };
  const apiKey = localStorage.getItem('api_key') || '';
  if (apiKey) headers['X-API-Key'] = apiKey;

  const payload = {
    query,
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(sessionToken ? { session_token: sessionToken } : {}),
  };
  fetchWithAuth('/api/chat/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal: controller.signal,
  })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (!response.body) throw new Error('SSE 响应没有可读流');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      function dispatchData(data) {
        if (data.type === 'chunk' && onChunk) onChunk(data.content);
        else if (data.type === 'content_complete' && onContentComplete) onContentComplete(data);
        else if (data.type === 'done' && onDone) {
          sawDone = true;
          onDone(data);
        } else if (data.type === 'error' && onError) onError(data.content);
        else if ((data.type === 'status' || data.type === 'progress') && onStatus)
          onStatus(data);
        else if (data.type === 'thinking' && onThinking)
          onThinking(data);
        else if (data.type === 'tool_call' && onToolCall)
          onToolCall(data);
        else if (data.type === 'tool_result' && onToolResult)
          onToolResult(data);
        else if (data.type === 'rag_status' && onRagStatus)
          onRagStatus(data);
        else if (data.type === 'agent_switch' && onAgentSwitch)
          onAgentSwitch(data);
      }

      function flushBuffer(final = false) {
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const line of parts) {
          if (!line.startsWith('data: ')) continue;
          try {
            dispatchData(JSON.parse(line.slice(6)));
          } catch (_e) {}
        }

        if (final) {
          const trimmed = buffer.trim();
          buffer = '';
          if (!trimmed.startsWith('data: ')) return;
          try {
            dispatchData(JSON.parse(trimmed.slice(6)));
          } catch (_e) {}
        }
      }

      function read() {
        reader
          .read()
          .then(({ done, value }) => {
            if (done) {
              flushBuffer(true);
              if (!sawDone && onError) onError('SSE 流已结束，但未收到 done 事件');
              return;
            }
            buffer += decoder.decode(value, { stream: true });
            flushBuffer(false);
            read();
          })
          .catch((err) => {
            if (onError) onError(err.message);
          });
      }
      read();
    })
    .catch((err) => {
      if (err.name !== 'AbortError' && onError) onError(err.message);
    });

  return controller;
}

/**
 * v5.1: 多模态 SSE 流式对话（图片 + 文字）
 * @param {string} query - 用户提问
 * @param {File} imageFile - 图片文件
 * @param {string} sessionId - 会话 ID
 * @param {string} sessionToken - 会话令牌
 * @param {object} callbacks - { onChunk, onContentComplete, onDone, onError, onStatus }
 * @returns {AbortController} 用于取消请求
 */
export function sendChatStreamWithImage(query, imageFile, sessionId, sessionToken, callbacks = {}) {
  const {
    onChunk,
    onContentComplete,
    onDone,
    onError,
    onStatus,
    onThinking,
    onToolCall,
    onToolResult,
    onRagStatus,
    onAgentSwitch,
  } = callbacks;
  const controller = new AbortController();
  let sawDone = false;

  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('query', query || '');
  formData.append('session_id', sessionId || '');
  formData.append('session_token', sessionToken || '');

  const headers = {};
  const apiKey = localStorage.getItem('api_key') || '';
  if (apiKey) headers['X-API-Key'] = apiKey;

  fetchWithAuth('/api/chat/multimodal/stream', {
    method: 'POST',
    headers,
    body: formData,
    signal: controller.signal,
  })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (!response.body) throw new Error('SSE 响应没有可读流');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      function dispatchData(data) {
        if (data.type === 'chunk' && onChunk) onChunk(data.content);
        else if (data.type === 'content_complete' && onContentComplete) onContentComplete(data);
        else if (data.type === 'done' && onDone) {
          sawDone = true;
          onDone(data);
        } else if (data.type === 'error' && onError) onError(data.content);
        else if ((data.type === 'status' || data.type === 'progress') && onStatus)
          onStatus(data);
        else if (data.type === 'thinking' && onThinking)
          onThinking(data);
        else if (data.type === 'tool_call' && onToolCall)
          onToolCall(data);
        else if (data.type === 'tool_result' && onToolResult)
          onToolResult(data);
        else if (data.type === 'rag_status' && onRagStatus)
          onRagStatus(data);
        else if (data.type === 'agent_switch' && onAgentSwitch)
          onAgentSwitch(data);
      }

      function flushBuffer(final = false) {
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const line of parts) {
          if (!line.startsWith('data: ')) continue;
          try {
            dispatchData(JSON.parse(line.slice(6)));
          } catch (_e) {}
        }

        if (final) {
          const trimmed = buffer.trim();
          buffer = '';
          if (!trimmed.startsWith('data: ')) return;
          try {
            dispatchData(JSON.parse(trimmed.slice(6)));
          } catch (_e) {}
        }
      }

      function read() {
        reader
          .read()
          .then(({ done, value }) => {
            if (done) {
              flushBuffer(true);
              if (!sawDone && onError) onError('SSE 流已结束，但未收到 done 事件');
              return;
            }
            buffer += decoder.decode(value, { stream: true });
            flushBuffer(false);
            read();
          })
          .catch((err) => {
            if (onError) onError(err.message);
          });
      }
      read();
    })
    .catch((err) => {
      if (err.name !== 'AbortError' && onError) onError(err.message);
    });

  return controller;
}
