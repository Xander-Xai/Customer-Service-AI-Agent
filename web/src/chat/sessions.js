/**
 * 会话管理模块
 * 状态从 chatState 读取，保持原有导出函数签名不变
 */
import { API } from '../api/index.js';
import {
  addMessage,
  getSessionId,
  getSessionToken,
  resetSession,
  resetWaiting,
  setSession,
  updateSession,
} from '../state/chatState.js';
import { CHAT } from '../utils/copy.js';
import { escapeHtml } from '../utils/dom.js';
import { formatTime } from '../utils/format.js';
import { useQuickPrompt } from './input.js';
import {
  appendAssistantMessage,
  appendSystemMessage,
  appendUserMessage,
  setSessionId,
} from './messages.js';
import { bindQuickPrompts, renderWelcome } from './welcome.js';

/** 获取当前会话 ID（向后兼容） */
export function getCurrentSessionId() {
  return getSessionId();
}

/** 获取当前会话 Token（向后兼容） */
export function getCurrentSessionToken() {
  return getSessionToken();
}

/** 更新会话信息（向后兼容） */
export function updateSessionInfo(sessionId, sessionToken) {
  updateSession(sessionId, sessionToken);
  setSessionId(sessionId);
}

/** 添加消息到历史（向后兼容） */
export function addToHistory(msg) {
  addMessage(msg);
}

/** 新建对话 */
export function startNewChat() {
  resetSession();
  resetWaiting();
  setSessionId(null);

  const chatInput = document.getElementById('chatInput');
  if (chatInput) {
    chatInput.value = '';
    chatInput.style.height = 'auto';
  }

  const container = document.getElementById('chatMessages');
  renderWelcome(container);
  bindQuickPrompts(useQuickPrompt);

  document.querySelectorAll('.session-item').forEach((el) => {
    el.classList.remove('active');
  });
}

/** 加载会话列表 */
export async function loadSessionList() {
  try {
    const data = await API.getSessions();
    const list = document.getElementById('sessionList');
    const sessions = data.sessions || [];
    const sid = getSessionId();

    if (sessions.length === 0) {
      list.innerHTML = `<div style="padding:32px 20px;text-align:center;color:var(--text-muted);font-size:13px"><div style="font-size:32px;margin-bottom:12px">💬</div>${CHAT.emptySessionList}</div>`;
      return;
    }

    list.innerHTML = sessions
      .map((s) => {
        const isActive = s.session_id === sid;
        const timeStr = s.last_activity ? formatTime(s.last_activity) : '';
        const driftWarn = s.drift_count > 0 ? `⚠️ ${s.drift_count}次漂移` : '';
        return `
        <div class="session-item ${isActive ? 'active' : ''}"
             data-session-id="${escapeHtml(s.session_id)}">
          <div class="session-item-title">${escapeHtml(s.summary || `对话 ${s.session_id.slice(0, 8)}`)}</div>
          <div class="session-item-meta">
            <span>💬 ${s.message_count || 0}条</span>
            <span>${timeStr}</span>
            ${driftWarn ? `<span style="color:var(--warning)">${driftWarn}</span>` : ''}
          </div>
          <button class="btn-delete-session" title="删除会话">🗑</button>
        </div>
      `;
      })
      .join('');

    list.querySelectorAll('.session-item').forEach((item) => {
      item.addEventListener('click', (e) => {
        if (e.target.closest('.btn-delete-session')) return;
        selectSession(item.dataset.sessionId);
      });
    });

    list.querySelectorAll('.btn-delete-session').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sessionItem = btn.closest('.session-item');
        deleteSessionConfirm(sessionItem.dataset.sessionId);
      });
    });
  } catch (_e) {}
}

/** 选择并加载会话 */
export async function selectSession(sessionId) {
  setSession(sessionId);
  setSessionId(sessionId);

  try {
    const data = await API.getSession(sessionId);
    const session = data.session;

    // 激活会话详情侧面板
    _showSessionDetail(sessionId, session);

    const messages = session.messages || [];

    const container = document.getElementById('chatMessages');
    container.innerHTML = '';

    if (messages.length === 0) {
      container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-muted)"><div style="font-size:32px;margin-bottom:12px">📭</div>${CHAT.emptySession}</div>`;
    } else {
      messages.forEach((msg) => {
        if (msg.role === 'user') {
          appendUserMessage(msg.content);
        } else {
          appendAssistantMessage(msg.content, { agent: msg.agent || '' });
        }
      });
    }

    document.querySelectorAll('.session-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.sessionId === sessionId);
    });
  } catch (e) {
    appendSystemMessage(`加载会话失败: ${e.message || '未知错误'}`);
  }
}

/** 删除会话确认 */
async function deleteSessionConfirm(sessionId) {
  if (!confirm('确定要删除这个会话吗？')) return;
  try {
    await API.deleteSession(sessionId);
    if (getSessionId() === sessionId) startNewChat();
    loadSessionList();
  } catch (e) {
    appendSystemMessage(`删除失败: ${e.message || '未知错误'}`);
  }
}

/** 显示会话详情侧面板 */
function _showSessionDetail(sessionId, session) {
  const panel = document.getElementById('sessionDetailPanel');
  const content = document.getElementById('sessionDetailContent');
  if (!panel || !content) return;

  const msgCount = session.message_count || (session.messages?.length ?? 0);
  const createdAt = session.created_at ? formatTime(session.created_at) : '--';
  const lastActivity = session.last_activity ? formatTime(session.last_activity) : '--';

  content.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">会话 ID</div>
        <div style="font-family:monospace;font-size:12px;word-break:break-all">${escapeHtml(sessionId)}</div>
      </div>
      <div style="display:flex;gap:16px">
        <div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">消息数</div>
          <div style="font-size:14px;font-weight:600">${msgCount}</div>
        </div>
        <div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">漂移次数</div>
          <div style="font-size:14px;font-weight:600">${session.drift_count || 0}</div>
        </div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">创建时间</div>
        <div style="font-size:13px">${createdAt}</div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">最后活动</div>
        <div style="font-size:13px">${lastActivity}</div>
      </div>
      ${session.summary ? `
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">摘要</div>
        <div style="font-size:13px">${escapeHtml(session.summary)}</div>
      </div>
      ` : ''}
    </div>
  `;

  panel.classList.add('open');
}

// 初始化面板关闭按钮
(function _initPanelClose() {
  const btn = document.getElementById('btnClosePanel');
  const panel = document.getElementById('sessionDetailPanel');
  if (btn && panel) {
    btn.addEventListener('click', () => panel.classList.remove('open'));
  }
})();
