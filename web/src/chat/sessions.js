/**
 * 会话管理模块
 */
import { API } from '../api/index.js';
import { escapeHtml } from '../utils/dom.js';
import { formatTime } from '../utils/format.js';
import { appendUserMessage, appendAssistantMessage, appendSystemMessage, setSessionId } from './messages.js';
import { renderWelcome, bindQuickPrompts } from './welcome.js';
import { useQuickPrompt, resetWaitingState } from './input.js';

let currentSessionId = localStorage.getItem('currentSessionId') || null;
let currentSessionToken = localStorage.getItem('currentSessionToken') || null;
let messageHistory = [];

/** 获取当前会话 ID */
export function getCurrentSessionId() { return currentSessionId; }

/** 获取当前会话 Token */
export function getCurrentSessionToken() { return currentSessionToken; }

/** 设置会话信息（由 response handler 调用） */
export function updateSessionInfo(sessionId, sessionToken) {
  if (sessionId) {
    currentSessionId = sessionId;
    localStorage.setItem('currentSessionId', sessionId);
    setSessionId(sessionId);
  }
  if (sessionToken) {
    currentSessionToken = sessionToken;
    localStorage.setItem('currentSessionToken', sessionToken);
  }
}

/** 添加消息到历史 */
export function addToHistory(msg) { messageHistory.push(msg); }

/** 获取消息历史 */
export function getMessageHistory() { return messageHistory; }

/** 新建对话 */
export function startNewChat() {
  currentSessionId = null;
  currentSessionToken = null;
  messageHistory = [];
  localStorage.removeItem('currentSessionId');
  localStorage.removeItem('currentSessionToken');
  setSessionId(null);

  // 重置发送状态和输入框
  resetWaitingState();
  const chatInput = document.getElementById('chatInput');
  if (chatInput) { chatInput.value = ''; chatInput.style.height = 'auto'; }

  const container = document.getElementById('chatMessages');
  renderWelcome(container);
  bindQuickPrompts(useQuickPrompt);

  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
}

/** 加载会话列表 */
export async function loadSessionList() {
  try {
    const data = await API.getSessions();
    const list = document.getElementById('sessionList');
    const sessions = data.sessions || [];

    if (sessions.length === 0) {
      list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);font-size:12px">暂无会话记录</div>';
      return;
    }

    list.innerHTML = sessions.map(s => {
      const isActive = s.session_id === currentSessionId;
      const timeStr = s.last_activity ? formatTime(s.last_activity) : '';
      const driftWarn = s.drift_count > 0 ? `⚠️ ${s.drift_count}次漂移` : '';
      return `
        <div class="session-item ${isActive ? 'active' : ''}"
             data-session-id="${escapeHtml(s.session_id)}">
          <div class="session-item-title">${escapeHtml(s.summary || '对话 ' + s.session_id.slice(0, 8))}</div>
          <div class="session-item-meta">
            <span>💬 ${s.message_count || 0}条</span>
            <span>${timeStr}</span>
            ${driftWarn ? `<span style="color:var(--warning)">${driftWarn}</span>` : ''}
          </div>
          <button class="btn-delete-session" title="删除会话">🗑</button>
        </div>
      `;
    }).join('');

    // 绑定点击事件
    list.querySelectorAll('.session-item').forEach(item => {
      item.addEventListener('click', (e) => {
        if (e.target.closest('.btn-delete-session')) return;
        selectSession(item.dataset.sessionId);
      });
    });

    // 绑定删除按钮
    list.querySelectorAll('.btn-delete-session').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sessionItem = btn.closest('.session-item');
        deleteSessionConfirm(sessionItem.dataset.sessionId);
      });
    });
  } catch (e) {
    console.error('加载会话列表失败:', e);
  }
}

/** 选择并加载会话 */
export async function selectSession(sessionId) {
  currentSessionId = sessionId;
  currentSessionToken = null;
  localStorage.setItem('currentSessionId', sessionId);
  localStorage.removeItem('currentSessionToken');
  messageHistory = [];
  setSessionId(sessionId);

  try {
    const data = await API.getSession(sessionId);
    const session = data.session;
    const messages = session.messages || [];

    const container = document.getElementById('chatMessages');
    container.innerHTML = '';

    if (messages.length === 0) {
      container.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-muted)">该会话暂无消息</div>';
    } else {
      messages.forEach(msg => {
        if (msg.role === 'user') {
          appendUserMessage(msg.content);
        } else {
          appendAssistantMessage(msg.content, { agent: msg.agent || '' });
        }
      });
    }

    // 更新侧边栏激活状态
    document.querySelectorAll('.session-item').forEach(el => {
      el.classList.toggle('active', el.dataset.sessionId === sessionId);
    });

    loadSessionList();
  } catch (e) {
    console.error('加载会话详情失败:', e);
    appendSystemMessage('加载会话失败，请重试');
  }
}

/** 删除会话确认 */
export async function deleteSessionConfirm(sessionId) {
  const title = sessionId.slice(0, 8);
  if (!confirm(`确定删除对话 ${title}...？此操作不可撤销。`)) return;

  try {
    await API.deleteSession(sessionId);
    if (sessionId === currentSessionId) {
      startNewChat();
    }
    loadSessionList();
  } catch (e) {
    console.error('删除会话失败:', e);
    appendSystemMessage('删除会话失败: ' + (e.message || '未知错误'));
  }
}
