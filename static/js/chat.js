/**
 * 智能对话主界面 - 交互逻辑
 * WebSocket 实时对话 + Agent 状态可视化 + 会话管理
 */

// ===== 状态管理 =====
let currentSessionId = null;
let isWaitingResponse = false;
let messageHistory = [];  // 当前会话消息缓存
let progressStatusEl = null;  // 进度状态 DOM 引用

// ===== 页面初始化 =====
document.addEventListener('DOMContentLoaded', () => {
  // 连接 WebSocket
  API.connect();

  // 注册事件监听
  API.on('connected', handleWSConnected);
  API.on('disconnected', handleWSDisconnected);
  API.on('status', handleStatus);
  API.on('progress', handleProgress);
  API.on('response', handleResponse);
  API.on('error', handleError);

  // 加载会话列表
  loadSessionList();

  // 聚焦输入框
  document.getElementById('chatInput').focus();
});

// ===== WebSocket 事件处理 =====

function handleWSConnected() {
  const dot = document.getElementById('wsStatusDot');
  const text = document.getElementById('wsStatusText');
  dot.classList.remove('disconnected');
  text.textContent = '已连接';
}

function handleWSDisconnected(data) {
  const dot = document.getElementById('wsStatusDot');
  const text = document.getElementById('wsStatusText');
  dot.classList.add('disconnected');
  text.textContent = data.code === 1000 ? '已断开' : '重连中...';
}

function handleStatus(data) {
  showProgressStatus(data.content);
}

function handleProgress(data) {
  showProgressStatus(data.content);
}

function handleResponse(data) {
  removeProgressStatus();
  isWaitingResponse = false;
  updateSendButton();

  // 提取消息信息
  const content = data.content || '';
  const agent = data.agent || '';
  const elapsed = data.elapsed || data.processing_time || 0;
  const mode = data.mode || 'sequential';
  const cached = data.cached || false;
  const agentsUsed = data.agents_used || [];
  const resolutionStatus = data.resolution_status || '';

  // 创建会话（首次消息时）
  if (!currentSessionId) {
    currentSessionId = data.session_id || generateSessionId();
  }

  // 渲染 AI 回复
  appendAssistantMessage(content, {
    agent, elapsed, mode, cached, agentsUsed, resolutionStatus
  });

  // 保存到历史
  messageHistory.push({
    role: 'assistant',
    content,
    agent, elapsed, mode, cached
  });

  // 刷新会话列表
  loadSessionList();
}

function handleError(data) {
  removeProgressStatus();
  isWaitingResponse = false;
  updateSendButton();

  appendSystemMessage(data.content || '发生未知错误');
}

// ===== 消息发送 =====

function sendMessage() {
  const input = document.getElementById('chatInput');
  const query = input.value.trim();
  if (!query || isWaitingResponse) return;

  // 隐藏欢迎界面
  const welcome = document.getElementById('welcomeScreen');
  if (welcome) welcome.style.display = 'none';

  // 渲染用户消息
  appendUserMessage(query);

  // 保存到历史
  messageHistory.push({ role: 'user', content: query });

  // 发送 WebSocket 消息
  isWaitingResponse = true;
  updateSendButton();
  showTypingIndicator();

  API.send(query, currentSessionId);

  // 清空输入
  input.value = '';
  autoResizeInput(input);
  input.focus();
}

// ===== 快捷提问 =====

function useQuickPrompt(card) {
  const textMap = {
    '产品成分查询': '请问你们的精华液含有哪些主要成分？适合敏感肌使用吗？',
    '订单物流追踪': '我想查询一下最近的订单物流状态',
    '使用方法指导': '面霜和精华液的正确使用顺序是什么？',
    '投诉与退款': '我收到的产品有质量问题，想要退货退款'
  };
  const title = card.querySelector('.quick-prompt-text').textContent;
  const query = textMap[title] || title;
  document.getElementById('chatInput').value = query;
  sendMessage();
}

// ===== 消息渲染 =====

function appendUserMessage(content) {
  const container = document.getElementById('chatMessages');
  const html = `
    <div class="message user">
      <div class="message-avatar">👤</div>
      <div class="message-content">
        <div class="message-bubble">${escapeHtml(content)}</div>
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
}

function appendAssistantMessage(content, meta = {}) {
  const container = document.getElementById('chatMessages');

  // 移除打字指示器
  removeTypingIndicator();

  // Agent 图标映射
  const agentIcons = {
    '产品专家': '🧴',
    '技术支持专家': '🔧',
    '账单专家': '💰',
    '投诉处理专家': '⚠️',
    '通用咨询专家': '📋',
    'response_agent': '📨',
  };

  const agentIcon = agentIcons[meta.agent] || '🤖';
  const modeLabels = {
    'sequential': '快速通道',
    'parallel': '并行处理',
    'consultation': '专家会诊',
    'hierarchical': '层级协作',
  };

  // 构建 Agent 流转轨迹
  let agentFlowHtml = '';
  if (meta.agentsUsed && meta.agentsUsed.length > 1) {
    const steps = meta.agentsUsed.map((a, i) => {
      const icon = agentIcons[a] || '🤖';
      const cls = i === meta.agentsUsed.length - 1 ? 'active' : 'completed';
      return `<div class="agent-flow-step ${cls}">${icon} ${a}</div>`;
    }).join('<span class="agent-flow-arrow">→</span>');
    agentFlowHtml = `<div class="agent-flow">${steps}</div>`;
  }

  const html = `
    <div class="message assistant">
      <div class="message-avatar">${agentIcon}</div>
      <div class="message-content">
        <div class="message-bubble">${renderMarkdown(content)}</div>
        ${agentFlowHtml}
        <div class="message-agent-tag">
          <span class="agent-icon">${agentIcon}</span>
          ${meta.agent || 'AI 助手'}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="mode-badge ${meta.mode || 'sequential'}">
            ${modeLabels[meta.mode] || meta.mode || '快速通道'}
          </span>
          ${meta.cached ? '<span class="cached-badge">⚡ 缓存命中</span>' : ''}
          <span class="message-meta">
            ${meta.elapsed ? `<span class="meta-item">⏱ ${(meta.elapsed).toFixed(1)}s</span>` : ''}
          </span>
        </div>
        <div class="feedback-bar">
          <button class="btn-feedback positive" onclick="sendFeedback(this, true)" title="有帮助">
            👍 有帮助
          </button>
          <button class="btn-feedback negative" onclick="sendFeedback(this, false)" title="没帮助">
            👎 没帮助
          </button>
        </div>
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
}

function appendSystemMessage(content) {
  const container = document.getElementById('chatMessages');
  const html = `
    <div class="message system">
      <div class="message-avatar">ℹ️</div>
      <div class="message-content">
        <div class="message-bubble">${escapeHtml(content)}</div>
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
}

// ===== 打字指示器 =====

function showTypingIndicator() {
  removeTypingIndicator();
  const container = document.getElementById('chatMessages');
  const html = `
    <div class="typing-indicator" id="typingIndicator">
      <div class="message-avatar" style="background:linear-gradient(135deg,var(--primary),#a855f7)">🤖</div>
      <div class="typing-dots">
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);
  scrollToBottom();
}

function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

// ===== 进度状态 =====

function showProgressStatus(text) {
  removeTypingIndicator();
  if (!progressStatusEl) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'progress-status';
    div.innerHTML = `<div class="progress-spinner"></div><span id="progressText">${escapeHtml(text)}</span>`;
    container.appendChild(div);
    progressStatusEl = div;
    scrollToBottom();
  } else {
    const textEl = progressStatusEl.querySelector('#progressText');
    if (textEl) textEl.textContent = text;
  }
}

function removeProgressStatus() {
  if (progressStatusEl) {
    progressStatusEl.remove();
    progressStatusEl = null;
  }
}

// ===== 反馈 =====

function sendFeedback(btn, resolved) {
  if (!currentSessionId) return;
  API.submitFeedback(currentSessionId, resolved).then(() => {
    const bar = btn.parentElement;
    bar.innerHTML = '<span style="font-size:11px;color:var(--text-muted)">✅ 感谢反馈</span>';
  }).catch(() => {
    // 静默失败
  });
}

// ===== 会话管理 =====

function startNewChat() {
  currentSessionId = null;
  messageHistory = [];

  // 清空聊天区域并显示欢迎界面
  const container = document.getElementById('chatMessages');
  container.innerHTML = `
    <div class="welcome-screen" id="welcomeScreen">
      <div class="welcome-icon">💬</div>
      <h2 class="welcome-title">智能客服助手</h2>
      <p class="welcome-subtitle">
        基于多智能体协作，为您提供产品咨询、技术支持、订单查询、投诉处理等一站式服务
      </p>
      <div class="quick-prompts">
        <div class="quick-prompt-card" onclick="useQuickPrompt(this)">
          <div class="quick-prompt-icon">🧴</div>
          <div class="quick-prompt-text">产品成分查询</div>
          <div class="quick-prompt-desc">了解产品成分、功效和适用肤质</div>
        </div>
        <div class="quick-prompt-card" onclick="useQuickPrompt(this)">
          <div class="quick-prompt-icon">📦</div>
          <div class="quick-prompt-text">订单物流追踪</div>
          <div class="quick-prompt-desc">查询订单状态、发货和物流信息</div>
        </div>
        <div class="quick-prompt-card" onclick="useQuickPrompt(this)">
          <div class="quick-prompt-icon">🔧</div>
          <div class="quick-prompt-text">使用方法指导</div>
          <div class="quick-prompt-desc">产品搭配、使用顺序和注意事项</div>
        </div>
        <div class="quick-prompt-card" onclick="useQuickPrompt(this)">
          <div class="quick-prompt-icon">⚠️</div>
          <div class="quick-prompt-text">投诉与退款</div>
          <div class="quick-prompt-desc">问题反馈、退款退货流程咨询</div>
        </div>
      </div>
    </div>
  `;

  // 取消侧边栏激活状态
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
}

async function loadSessionList() {
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
             onclick="selectSession('${s.session_id}')">
          <div class="session-item-title">${s.summary || '对话 ' + s.session_id.slice(0, 8)}</div>
          <div class="session-item-meta">
            <span>💬 ${s.message_count || 0}条</span>
            <span>${timeStr}</span>
            ${driftWarn ? `<span style="color:var(--warning)">${driftWarn}</span>` : ''}
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    console.error('加载会话列表失败:', e);
  }
}

async function selectSession(sessionId) {
  currentSessionId = sessionId;
  messageHistory = [];

  try {
    const data = await API.getSession(sessionId);
    const session = data.session;
    const messages = session.messages || [];

    // 清空并渲染历史消息
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
    document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
    const items = document.querySelectorAll('.session-item');
    items.forEach(el => {
      if (el.onclick && el.onclick.toString().includes(sessionId)) {
        el.classList.add('active');
      }
    });

    loadSessionList();
  } catch (e) {
    console.error('加载会话详情失败:', e);
    appendSystemMessage('加载会话失败，请重试');
  }
}

// ===== 监控页面 =====

let monitorRefreshTimer = null;

function switchPage(page) {
  // 更新导航状态
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
  document.querySelector(`[data-page="${page}"]`).classList.add('active');

  const chatPage = document.getElementById('chatPage');
  const monitorPage = document.getElementById('monitorPage');

  if (page === 'chat') {
    chatPage.style.display = 'flex';
    monitorPage.style.display = 'none';
    if (monitorRefreshTimer) {
      clearInterval(monitorRefreshTimer);
      monitorRefreshTimer = null;
    }
  } else {
    chatPage.style.display = 'none';
    monitorPage.style.display = 'block';
    refreshMonitorData();
    // 每 10 秒自动刷新监控数据
    monitorRefreshTimer = setInterval(refreshMonitorData, 10000);
  }
}

async function refreshMonitorData() {
  try {
    const [metricsRes, kpiRes, sessionsRes, alertsRes, cacheRes] = await Promise.all([
      API.getMetrics().catch(() => null),
      API.getKPI().catch(() => null),
      API.getSessions().catch(() => ({ sessions: [] })),
      API.getAlerts().catch(() => ({ alerts: [] })),
      API.getCacheStats().catch(() => null),
    ]);

    renderMetricCards(metricsRes);
    renderAgentChart(metricsRes);
    renderModeChart(metricsRes);
    renderSLA(metricsRes);
    renderCacheStats(cacheRes);
    renderKPI(kpiRes);
    renderSessionsTable(sessionsRes.sessions || []);
    renderAlerts(alertsRes.alerts || []);
  } catch (e) {
    console.error('监控数据刷新失败:', e);
  }
}

// ===== 监控渲染函数 =====

function renderMetricCards(data) {
  if (!data || !data.metrics) return;
  const m = data.metrics;
  const container = document.getElementById('metricCards');
  container.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">📨 总请求</div>
      <div class="metric-value">${m.total_requests || 0}</div>
      <div class="metric-detail">错误率: ${m.error_rate || 0}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">⚡ 平均响应</div>
      <div class="metric-value ${m.avg_response_time > 20 ? 'bad' : m.avg_response_time > 10 ? 'warn' : 'good'}">
        ${m.avg_response_time || 0}s
      </div>
      <div class="metric-detail">P95: ${m.p95_response_time || 0}s</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">🚨 SLA 违约</div>
      <div class="metric-value ${(m.sla?.violations_slow || 0) > 0 ? 'bad' : 'good'}">
        ${m.sla?.violations_slow || 0}
      </div>
      <div class="metric-detail">违约率: ${m.sla?.violation_rate || 0}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">📊 活跃会话</div>
      <div class="metric-value">${Object.keys(m.agent_call_counts || {}).length > 0 ? '活跃' : '空闲'}</div>
      <div class="metric-detail">缓存命中率: ${m.cache_hit_rate || 0}%</div>
    </div>
  `;
}

function renderAgentChart(data) {
  if (!data || !data.metrics) return;
  const counts = data.metrics.agent_call_counts || {};
  const container = document.getElementById('agentChart');
  const colors = ['primary', 'success', 'warning', 'info', 'error'];
  const entries = Object.entries(counts);

  if (entries.length === 0) {
    container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);width:100%">暂无数据</div>';
    return;
  }

  const maxVal = Math.max(...entries.map(e => e[1]), 1);
  container.innerHTML = entries.map(([name, count], i) => `
    <div class="bar-item">
      <div class="bar-value">${count}</div>
      <div class="bar-fill ${colors[i % colors.length]}" style="height:${(count / maxVal) * 100}%"></div>
      <div class="bar-label">${name}</div>
    </div>
  `).join('');
}

function renderModeChart(data) {
  if (!data || !data.metrics) return;
  const counts = data.metrics.mode_counts || {};
  const container = document.getElementById('modeChart');
  const modeColors = { sequential: 'info', parallel: 'success', consultation: 'warning', hierarchical: 'error' };
  const modeLabels = { sequential: '快速通道', parallel: '并行处理', consultation: '专家会诊', hierarchical: '层级协作' };
  const entries = Object.entries(counts);

  if (entries.length === 0) {
    container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);width:100%">暂无数据</div>';
    return;
  }

  const maxVal = Math.max(...entries.map(e => e[1]), 1);
  container.innerHTML = entries.map(([name, count]) => `
    <div class="bar-item">
      <div class="bar-value">${count}</div>
      <div class="bar-fill ${modeColors[name] || 'primary'}" style="height:${(count / maxVal) * 100}%"></div>
      <div class="bar-label">${modeLabels[name] || name}</div>
    </div>
  `).join('');
}

function renderSLA(data) {
  if (!data || !data.metrics?.sla) return;
  const sla = data.metrics.sla;
  const complianceRate = 100 - (sla.violation_rate || 0);
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('slaFill');
  fill.setAttribute('stroke-dasharray', `${(complianceRate / 100) * circumference} ${circumference}`);
  fill.setAttribute('stroke', complianceRate >= 90 ? 'var(--success)' : complianceRate >= 70 ? 'var(--warning)' : 'var(--error)');

  document.getElementById('slaValue').textContent = `${complianceRate.toFixed(1)}%`;
  const statusEl = document.getElementById('slaStatus');
  statusEl.textContent = complianceRate >= 90 ? '达标' : complianceRate >= 70 ? '注意' : '告警';
  statusEl.className = `status-tag ${complianceRate >= 90 ? 'healthy' : complianceRate >= 70 ? 'warning' : 'critical'}`;
  document.getElementById('slaDetail').textContent = `目标: ${sla.target_min}s - ${sla.target_max}s | 违约: ${sla.violations_slow}次`;
}

function renderCacheStats(data) {
  if (!data) return;
  const hitRate = parseFloat(data.hit_rate) || 0;
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('cacheFill');
  fill.setAttribute('stroke-dasharray', `${(hitRate / 100) * circumference} ${circumference}`);

  document.getElementById('cacheValue').textContent = `${hitRate}%`;
  document.getElementById('cacheDetail').textContent = `L1: ${data.l1_hits || 0}次 / L2: ${data.l2_hits || 0}次 | 大小: L1=${data.l1_size || 0} L2=${data.l2_size || 0}`;
}

function renderKPI(data) {
  if (!data || !data.kpi) return;
  const kpi = data.kpi;

  // 首次解决率
  const firstRate = parseFloat(kpi.first_resolution_rate) || 0;
  const circumference = 2 * Math.PI * 42;
  const resFill = document.getElementById('resolutionFill');
  resFill.setAttribute('stroke-dasharray', `${(firstRate / 100) * circumference} ${circumference}`);
  document.getElementById('resolutionValue').textContent = kpi.first_resolution_rate || '--';
  document.getElementById('resolutionDetail').textContent = `单轮解决: ${kpi.total_single_turn_resolved || 0} | 多轮: ${kpi.total_multi_turn || 0}`;

  // AI 处理率
  const aiRate = parseFloat(kpi.ai_handled_rate) || 0;
  const aiFill = document.getElementById('aiFill');
  aiFill.setAttribute('stroke-dasharray', `${(aiRate / 100) * circumference} ${circumference}`);
  document.getElementById('aiValue').textContent = kpi.ai_handled_rate || '--';
  document.getElementById('aiDetail').textContent = `AI 处理: ${kpi.total_ai_handled || 0} | 转人工: ${kpi.total_escalated || 0}`;
}

function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessionsTableBody');
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px">暂无会话</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.slice(0, 10).map(s => {
    const driftWarn = s.drift_escalation;
    return `
      <tr>
        <td style="font-family:monospace;font-size:12px">${(s.session_id || '').slice(0, 12)}...</td>
        <td>${s.message_count || 0}</td>
        <td>${s.drift_count || 0}</td>
        <td>${s.last_activity ? formatTime(s.last_activity) : '--'}</td>
        <td>${driftWarn ? '<span class="status-tag warning">漂移告警</span>' : '<span class="status-tag healthy">正常</span>'}</td>
      </tr>
    `;
  }).join('');
}

function renderAlerts(alerts) {
  const container = document.getElementById('alertsList');
  if (!alerts.length) {
    container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">✅ 暂无告警</div>';
    return;
  }
  container.innerHTML = alerts.map(a => `
    <div class="alert-item">
      <div class="alert-icon ${a.severity || 'warning'}">
        ${a.severity === 'critical' ? '🔴' : '🟡'}
      </div>
      <div class="alert-text">
        <div class="alert-text-title">${escapeHtml(a.message || '')}</div>
        <div class="alert-text-time">${a.timestamp ? formatTime(a.timestamp) : ''} | 窗口违约率: ${a.window_rate || 0}%</div>
      </div>
    </div>
  `).join('');
}

// ===== 会话详情面板 =====

function closeSessionPanel() {
  document.getElementById('sessionDetailPanel').classList.remove('open');
}

// ===== 工具函数 =====

function handleInputKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function autoResizeInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  updateSendButton();
}

function updateSendButton() {
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('btnSend');
  btn.disabled = !input.value.trim() || isWaitingResponse;
}

function scrollToBottom() {
  const container = document.getElementById('chatMessages');
  requestAnimationFrame(() => {
    container.scrollTop = container.scrollHeight;
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/** 简易 Markdown 渲染（加粗、换行、代码块、列表） */
function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  // 代码块
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre style="background:var(--bg-base);padding:10px;border-radius:6px;overflow-x:auto;font-size:12px;margin:8px 0"><code>$2</code></pre>');
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code style="background:var(--bg-hover);padding:2px 5px;border-radius:3px;font-size:12px">$1</code>');
  // 加粗
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // 换行
  html = html.replace(/\n/g, '<br>');
  // 列表项
  html = html.replace(/^- (.+)/gm, '<span style="display:block;padding-left:12px">• $1</span>');
  return html;
}

function formatTime(timestamp) {
  const date = new Date(timestamp * 1000);
  const now = new Date();
  const diff = (now - date) / 1000;

  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;

  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function generateSessionId() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}
