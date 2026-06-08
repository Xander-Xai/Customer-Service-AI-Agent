/**
 * SSE 流式对话
 */

/**
 * 发送流式对话请求
 * @param {string} query - 用户提问
 * @param {string} sessionId - 会话 ID
 * @param {string} sessionToken - 会话令牌
 * @param {object} callbacks - { onChunk, onDone, onError, onStatus }
 * @returns {AbortController} 用于取消请求
 */
export function sendChatStream(query, sessionId, sessionToken, callbacks = {}) {
  const { onChunk, onDone, onError, onStatus } = callbacks;
  const controller = new AbortController();

  const token = localStorage.getItem('token');
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;

  fetch('/api/chat/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify({ query, session_id: sessionId, session_token: sessionToken }),
    signal: controller.signal,
  }).then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function read() {
      reader.read().then(({ done, value }) => {
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.type === 'chunk' && onChunk) onChunk(data.content);
            else if (data.type === 'done' && onDone) onDone(data);
            else if (data.type === 'error' && onError) onError(data.content);
            else if ((data.type === 'status' || data.type === 'progress') && onStatus) onStatus(data);
          } catch (e) { console.warn('[SSE] Parse error:', e.message); }
        }
        read();
      }).catch(err => { if (onError) onError(err.message); });
    }
    read();
  }).catch(err => {
    if (err.name !== 'AbortError' && onError) onError(err.message);
  });

  return controller;
}
