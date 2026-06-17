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
import { createElement } from '../utils/dom.js';
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

  const detailPanel = document.getElementById('sessionDetailPanel');
  if (detailPanel) detailPanel.classList.remove('open');

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
      list.replaceChildren(
        createElement(
          'div',
          { style: 'padding:32px 20px;text-align:center;color:var(--text-muted);font-size:13px' },
          [
            createElement('div', { style: 'font-size:32px;margin-bottom:12px' }, ['💬']),
            CHAT.emptySessionList,
          ],
        ),
      );
      return;
    }

    const items = sessions.map((s) => {
      const isActive = s.session_id === sid;
      const timeStr = s.last_activity ? formatTime(s.last_activity) : '';
      const driftWarn = s.drift_count > 0 ? `⚠️ ${s.drift_count}次漂移` : '';

      const metaChildren = [
        createElement('span', {}, [`💬 ${s.message_count || 0}条`]),
        createElement('span', {}, [timeStr]),
      ];
      if (driftWarn) {
        metaChildren.push(createElement('span', { style: 'color:var(--warning)' }, [driftWarn]));
      }

      const sessionItem = createElement(
        'div',
        {
          className: `session-item ${isActive ? 'active' : ''}`,
          dataset: { sessionId: s.session_id },
        },
        [
          createElement('div', { className: 'session-item-title' }, [
            s.title || `对话 ${s.session_id.slice(0, 8)}`,
          ]),
          createElement('div', { className: 'session-item-meta' }, metaChildren),
          createElement('button', { className: 'btn-delete-session', title: '删除会话' }, ['🗑']),
        ],
      );

      sessionItem.addEventListener('click', (e) => {
        if (e.target.closest('.btn-delete-session')) return;
        selectSession(s.session_id);
      });

      const deleteBtn = sessionItem.querySelector('.btn-delete-session');
      if (deleteBtn) {
        deleteBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          deleteSessionConfirm(s.session_id);
        });
      }

      return sessionItem;
    });

    list.replaceChildren(...items);
  } catch (_e) {}
}

/** 选择并加载会话 */
export async function selectSession(sessionId) {
  setSession(sessionId);
  setSessionId(sessionId);

  // 启动后台检查 LangGraph checkpoint（如有断点续传）
  API.getSessionCheckpoint(sessionId)
    .then((cp) => {
      if (cp?.has_checkpoint) {
        // 静默标记 - 后续 send 操作会自然使用
        try {
          sessionStorage.setItem(`cp:${sessionId}`, '1');
        } catch (_e) {}
      }
    })
    .catch(() => {
      /* checkpoint 未启用或会话不存在，静默 */
    });

  try {
    const data = await API.getSession(sessionId);
    const session = data.session;

    // 从响应中获取 session_token，确保后续请求能通过验证
    if (session.session_token) {
      updateSession(sessionId, session.session_token);
    }

    // 激活会话详情侧面板
    _showSessionDetail(sessionId, session);

    const messages = session.messages || [];

    const container = document.getElementById('chatMessages');
    container.replaceChildren();

    if (messages.length === 0) {
      container.replaceChildren(
        createElement('div', { style: 'padding:40px;text-align:center;color:var(--text-muted)' }, [
          createElement('div', { style: 'font-size:32px;margin-bottom:12px' }, ['📭']),
          CHAT.emptySession,
        ]),
      );
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

  const summaryChild = session.summary
    ? createElement('div', {}, [
        createElement(
          'div',
          { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
          ['摘要'],
        ),
        createElement('div', { style: 'font-size:13px' }, [session.summary]),
      ])
    : null;

  content.replaceChildren(
    createElement(
      'div',
      { style: 'display:flex;flex-direction:column;gap:12px' },
      [
        createElement('div', {}, [
          createElement(
            'div',
            { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
            ['会话 ID'],
          ),
          createElement(
            'div',
            { style: 'font-family:monospace;font-size:12px;word-break:break-all' },
            [sessionId],
          ),
        ]),
        createElement('div', { style: 'display:flex;gap:16px' }, [
          createElement('div', {}, [
            createElement(
              'div',
              { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
              ['消息数'],
            ),
            createElement('div', { style: 'font-size:14px;font-weight:600' }, [msgCount]),
          ]),
          createElement('div', {}, [
            createElement(
              'div',
              { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
              ['漂移次数'],
            ),
            createElement('div', { style: 'font-size:14px;font-weight:600' }, [
              session.drift_count || 0,
            ]),
          ]),
        ]),
        createElement('div', {}, [
          createElement(
            'div',
            { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
            ['创建时间'],
          ),
          createElement('div', { style: 'font-size:13px' }, [createdAt]),
        ]),
        createElement('div', {}, [
          createElement(
            'div',
            { style: 'font-size:11px;color:var(--text-muted);margin-bottom:2px' },
            ['最后活动'],
          ),
          createElement('div', { style: 'font-size:13px' }, [lastActivity]),
        ]),
        summaryChild,
      ].filter(Boolean),
    ),
  );

  panel.classList.add('open');
}

// 初始化面板关闭按钮 + Esc 键关闭
(function _initPanelClose() {
  const btn = document.getElementById('btnClosePanel');
  const panel = document.getElementById('sessionDetailPanel');
  if (btn && panel) {
    btn.addEventListener('click', () => panel.classList.remove('open'));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && panel.classList.contains('open')) {
        panel.classList.remove('open');
      }
    });
  }
})();
