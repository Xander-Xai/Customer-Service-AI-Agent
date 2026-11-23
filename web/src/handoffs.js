/** Customer/staff handoff desk. Server-side JWT + ownership remain authoritative. */
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/layout.css';
import '../styles/handoffs.css';

import { getUserMe } from './api/rest.js';
import { fetchWithAuth } from './auth/index.js';

const $ = (id) => document.getElementById(id);
const STAFF = new Set(['agent', 'supervisor', 'admin']);
const state = { user: null, tickets: [], selected: null, busy: false, pendingSend: null };

export async function handoffRequest(method, path, body = undefined) {
  const options = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetchWithAuth(path, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    const detail = problem?.detail || problem?.error || `请求失败（HTTP ${response.status}）`;
    throw new Error(typeof detail === 'string' ? detail : '请求处理失败');
  }
  return response.json();
}

function tell(message, error = false) {
  const element = $('operationStatus');
  element.textContent = message || '';
  element.dataset.error = error ? 'true' : 'false';
}

function isStaff() {
  return STAFF.has(state.user?.role);
}

function isManager() {
  return state.user?.role === 'supervisor' || state.user?.role === 'admin';
}

function currentSessionId() {
  return localStorage.getItem('currentSessionId') || '';
}

function customerSessionProof() {
  return localStorage.getItem('currentSessionToken') || '';
}

function ticketText(ticket) {
  return `${ticket.status} · ${ticket.reason || '人工客服申请'}`;
}

function renderTicketList() {
  const list = $('ticketList');
  list.replaceChildren();
  for (const ticket of state.tickets) {
    const item = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ticket-picker';
    btn.textContent = ticketText(ticket);
    btn.setAttribute('aria-label', `查看工单 ${ticket.ticket_id}`);
    if (ticket.ticket_id === state.selected?.ticket_id) btn.setAttribute('aria-current', 'true');
    btn.addEventListener('click', () => selectTicket(ticket.ticket_id));
    item.append(btn);
    list.append(item);
  }
  if (!state.tickets.length) {
    const item = document.createElement('li');
    item.textContent = isStaff() ? '暂无可见工单' : '当前会话尚无工单';
    list.append(item);
  }
}

function renderMessages(messages) {
  const list = $('messageList');
  list.replaceChildren();
  for (const message of messages) {
    const item = document.createElement('li');
    item.className = message.sender_role === 'customer' ? 'customer-message' : 'staff-message';
    const heading = document.createElement('strong');
    heading.textContent = message.sender_role === 'customer' ? '客户' : '人工客服';
    const body = document.createElement('p');
    // Never use innerHTML for ERP/customer content.
    body.textContent = String(message.body || '');
    const timestamp = document.createElement('time');
    timestamp.textContent = String(message.created_at || '');
    item.append(heading, body, timestamp);
    list.append(item);
  }
}

function renderSelection(ticket) {
  state.selected = ticket;
  $('ticketTitle').textContent = ticket ? `工单 ${ticket.ticket_id}` : '请选择一张工单';
  $('ticketStatus').textContent = ticket
    ? `状态：${ticket.status} · 坐席：${ticket.assigned_to || '尚未认领'}`
    : '';
  $('ticketReason').textContent = ticket?.reason || '';
  const own = String(ticket?.assigned_to || '') === String(state.user?.user_id || '');
  const open = ticket?.status === 'OPEN';
  const claimed = ticket?.status === 'CLAIMED';
  const closed = ticket?.status === 'CLOSED';
  $('claimTicketBtn').hidden = !ticket || !isStaff() || !open;
  $('closeTicketBtn').hidden = !ticket || !claimed || !(own || isManager());
  $('reopenTicketBtn').hidden = !ticket || !closed || !(state.user?.role === 'customer' || isManager());
  $('messageForm').hidden = !ticket || closed || (isStaff() && !(own || isManager()));
  $('resolutionPanel').hidden = true;
  if (!ticket) renderMessages([]);
}

async function selectTicket(id) {
  if (!id) return;
  const previousId = state.selected?.ticket_id;
  if (previousId !== id) state.pendingSend = null;
  try {
    const ticket = await handoffRequest('GET', `/api/handoffs/${encodeURIComponent(id)}`);
    renderSelection(ticket);
    renderTicketList();
    const data = await handoffRequest('GET', `/api/handoffs/${encodeURIComponent(id)}/messages`);
    if (state.selected?.ticket_id === id) renderMessages(data.messages || []);
  } catch (error) {
    tell(error.message, true);
  }
}

async function loadTickets(silent = false) {
  if (state.busy) return;
  state.busy = true;
  try {
    if (isStaff()) {
      const filter = $('queueFilter').value;
      const qs = new URLSearchParams({ limit: '100' });
      if (filter) qs.set('status', filter);
      const result = await handoffRequest('GET', `/api/handoffs?${qs}`);
      state.tickets = result.tickets || [];
    } else {
      const session = currentSessionId();
      if (!session) {
        state.tickets = [];
        if (!silent) tell('请先在聊天页面创建会话，再申请转人工。', true);
      } else {
        try {
          const ticket = await handoffRequest(
            'GET',
            `/api/handoffs/session/${encodeURIComponent(session)}`,
          );
          state.tickets = [ticket];
        } catch (error) {
          if (error.message === '工单不存在或无权访问') state.tickets = [];
          else throw error;
        }
      }
    }
    renderTicketList();
    if (state.selected) {
      if (!state.tickets.some((ticket) => ticket.ticket_id === state.selected.ticket_id)) {
        renderSelection(null);
      } else {
        await selectTicket(state.selected.ticket_id);
      }
    } else if (state.tickets.length === 1) {
      await selectTicket(state.tickets[0].ticket_id);
    }
  } catch (error) {
    tell(error.message, true);
  } finally {
    state.busy = false;
  }
}

async function runAction(task) {
  if (state.busy) return;
  state.busy = true;
  try {
    await task();
    tell('操作已保存');
  } catch (error) {
    tell(error.message, true);
  } finally {
    state.busy = false;
  }
  await loadTickets(true);
}

function installListeners() {
  $('refreshTicketsBtn').addEventListener('click', () => loadTickets());
  $('queueFilter').addEventListener('change', () => {
    renderSelection(null);
    loadTickets();
  });
  $('createTicketBtn').addEventListener('click', () =>
    runAction(async () => {
      const reason = $('handoffReason').value.trim();
      if (!reason) throw new Error('请先描述要解决的问题');
      const sessionId = currentSessionId();
      const sessionToken = customerSessionProof();
      if (!sessionId || !sessionToken) throw new Error('缺少有效会话令牌，请返回聊天页面');
      const result = await handoffRequest('POST', '/api/handoffs', {
        session_id: sessionId,
        session_token: sessionToken,
        reason,
        message: reason,
      });
      $('handoffReason').value = '';
      state.selected = result;
    }),
  );
  $('claimTicketBtn').addEventListener('click', () =>
    runAction(async () => {
      await handoffRequest(
        'POST',
        `/api/handoffs/${encodeURIComponent(state.selected.ticket_id)}/claim`,
      );
    }),
  );
  $('closeTicketBtn').addEventListener('click', () => {
    $('resolutionPanel').hidden = false;
    $('resolutionText').focus();
  });
  $('cancelCloseBtn').addEventListener('click', () => {
    $('resolutionPanel').hidden = true;
  });
  $('submitCloseBtn').addEventListener('click', () =>
    runAction(async () => {
      const resolution = $('resolutionText').value.trim();
      if (!resolution) throw new Error('必须填写结单说明');
      await handoffRequest(
        'POST',
        `/api/handoffs/${encodeURIComponent(state.selected.ticket_id)}/close`,
        { resolution },
      );
      $('resolutionText').value = '';
    }),
  );
  $('reopenTicketBtn').addEventListener('click', () =>
    runAction(async () => {
      await handoffRequest(
        'POST',
        `/api/handoffs/${encodeURIComponent(state.selected.ticket_id)}/reopen`,
      );
    }),
  );
  $('messageForm').addEventListener('submit', (event) => {
    event.preventDefault();
    runAction(async () => {
      const ticket = state.selected;
      const body = $('messageBody').value.trim();
      if (!ticket || !body) throw new Error('请输入回复内容');
      // A network retry keeps the same delivery key and does not double-send.
      if (state.pendingSend?.ticket !== ticket.ticket_id || state.pendingSend?.body !== body) {
        state.pendingSend = {
          ticket: ticket.ticket_id,
          body,
          key: crypto.randomUUID(),
        };
      }
      await handoffRequest(
        'POST',
        `/api/handoffs/${encodeURIComponent(ticket.ticket_id)}/messages`,
        { body, client_message_id: state.pendingSend.key },
      );
      state.pendingSend = null;
      $('messageBody').value = '';
    });
  });
}

async function init() {
  installListeners();
  try {
    state.user = await getUserMe();
    const role = state.user.role;
    $('actorLabel').textContent = `${state.user.username} · ${role}`;
    if (!STAFF.has(role) && role !== 'customer') throw new Error('当前身份无权使用工单');
    $('customerCreate').hidden = role !== 'customer';
    $('staffQueueControls').hidden = !STAFF.has(role);
    $('queueTitle').textContent = STAFF.has(role) ? '坐席工单队列' : '我的人工工单';
    $('adminNavLink').hidden = !(role === 'supervisor' || role === 'admin');
    await loadTickets();
    window.setInterval(() => {
      if (document.visibilityState === 'visible') loadTickets(true);
    }, 10000);
  } catch (error) {
    tell(error.message, true);
  }
}

document.addEventListener('DOMContentLoaded', init);
