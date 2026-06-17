/**
 * 历史消息查看模块
 * 通过 /api/history/{session_id}/messages 拉取完整对话历史
 */
import { getHistory, getHistoryMessages } from './api/rest.js';
import { formatTime } from './utils/format.js';

/** 加载历史会话列表（与 /api/sessions 等价的别名） */
export async function loadHistoryList(offset = 0, limit = 20) {
  try {
    return await getHistory(offset, limit);
  } catch (_e) {
    return { sessions: [], total: 0 };
  }
}

/** 加载历史消息（需要 X-Session-Token） */
export async function loadHistoryMessages(sessionId) {
  try {
    const data = await getHistoryMessages(sessionId);
    return data?.messages || [];
  } catch (_e) {
    return [];
  }
}

/** 渲染历史消息到容器（用于管理后台/审计视图） */
export function renderHistoryMessages(container, messages) {
  if (!container) return;
  container.replaceChildren();

  if (!messages.length) {
    const empty = document.createElement('div');
    empty.style.textAlign = 'center';
    empty.style.color = 'var(--text-muted)';
    empty.style.padding = '20px';
    empty.textContent = '暂无消息';
    container.appendChild(empty);
    return;
  }

  messages.forEach((m) => {
    const row = document.createElement('div');
    row.style.padding = '8px 12px';
    row.style.borderBottom = '1px solid var(--border)';
    row.style.fontSize = '12px';

    const head = document.createElement('div');
    head.style.display = 'flex';
    head.style.justifyContent = 'space-between';
    head.style.color = 'var(--text-muted)';
    head.style.marginBottom = '4px';

    const role = document.createElement('span');
    role.textContent = m.role === 'user' ? '👤 用户' : '🤖 客服';
    const ts = document.createElement('span');
    ts.textContent = m.timestamp ? formatTime(m.timestamp) : '';
    head.appendChild(role);
    head.appendChild(ts);
    row.appendChild(head);

    const content = document.createElement('div');
    content.style.whiteSpace = 'pre-wrap';
    content.style.wordBreak = 'break-word';
    content.textContent = m.content;
    row.appendChild(content);

    container.appendChild(row);
  });
}
