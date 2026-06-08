/**
 * 管理后台入口
 */
import '../styles/variables.css';
import '../styles/layout.css';
import '../styles/components.css';
import '../styles/monitor.css';
import '../styles/admin.css';
import '../styles/animations.css';
import { showToast } from './utils/toast.js';
import { escapeHtml } from './utils/dom.js';
import { formatTime } from './utils/format.js';
import {
  getUsers, getAuditLog, getKnowledgeStats, seedKnowledge, syncKnowledge,
  getAlertConfig, testAlert as testAlertAPI, getHealth, getMetrics, getFeedbackStats,
  getQualityTrends, getHotQuestions, getSatisfaction,
} from './api/rest.js';

let currentUserRole = null;
let monitorRefreshTimer = null;

function logout() {
  if (monitorRefreshTimer) { clearInterval(monitorRefreshTimer); monitorRefreshTimer = null; }
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  window.location.href = '/login.html';
}

// ── 通用管理 API 请求（rest.js 未覆盖的端点） ──
function getHeaders() {
  const token = localStorage.getItem('token');
  if (!token) { logout(); return null; }
  return {
    'Authorization': 'Bearer ' + token,
    'Content-Type': 'application/json',
  };
}

async function adminApi(path, options = {}) {
  const hdrs = getHeaders();
  if (!hdrs) return null;
  const resp = await fetch(path, { headers: hdrs, ...options });
  if (resp.status === 401) { logout(); return null; }
  return resp.json();
}

// ── Section 切换 ──
function switchSection(sectionId) {
  // 隐藏所有 section
  document.querySelectorAll('.admin-section').forEach(el => { el.style.display = 'none'; });
  // 取消所有导航按钮 active
  document.querySelectorAll('.admin-nav-btn').forEach(btn => btn.classList.remove('active'));

  // 显示目标 section
  const sectionMap = {
    monitor: 'sectionMonitor',
    users: 'sectionUsers',
    knowledge: 'sectionKnowledge',
    alerts: 'sectionAlerts',
    system: 'sectionSystem',
  };
  const targetId = sectionMap[sectionId];
  if (targetId) {
    const target = document.getElementById(targetId);
    if (target) target.style.display = 'block';
  }

  // 设置 active 按钮
  const activeBtn = document.querySelector(`.admin-nav-btn[data-section="${sectionId}"]`);
  if (activeBtn) activeBtn.classList.add('active');

  // 启动/停止监控数据刷新
  if (sectionId === 'monitor') {
    refreshMonitorData();
    if (monitorRefreshTimer) clearInterval(monitorRefreshTimer);
    monitorRefreshTimer = setInterval(refreshMonitorData, 10000);
  } else {
    if (monitorRefreshTimer) { clearInterval(monitorRefreshTimer); monitorRefreshTimer = null; }
  }
}

function initSectionNav() {
  document.querySelectorAll('.admin-nav-btn').forEach(btn => {
    btn.addEventListener('click', () => switchSection(btn.dataset.section));
  });
}

// ── 用户信息 & 角色权限 ──
async function loadUserInfo() {
  const me = await adminApi('/api/auth/me');
  if (!me) return null;
  currentUserRole = me.role;
  document.getElementById('userDisplay').textContent = `${me.display_name || me.username} (${me.role})`;

  // 允许 admin 和 supervisor 进入管理后台
  if (me.role !== 'admin' && me.role !== 'supervisor') {
    showToast('需要管理员或主管权限', 'error');
    setTimeout(() => window.location.href = '/', 1000);
    return null;
  }

  // 根据角色隐藏无权导航按钮
  document.querySelectorAll('.admin-nav-btn[data-roles]').forEach(btn => {
    const allowedRoles = btn.dataset.roles.split(',').map(r => r.trim());
    if (!allowedRoles.includes(me.role)) {
      btn.style.display = 'none';
    }
  });

  // 非 admin 隐藏"测试告警"按钮
  const btnTestAlert = document.getElementById('btnTestAlert');
  if (btnTestAlert && me.role !== 'admin') btnTestAlert.style.display = 'none';

  // supervisor 默认显示 monitor section，admin 默认显示 users section
  if (me.role === 'supervisor') {
    switchSection('monitor');
  } else {
    // admin 默认显示 users section
    switchSection('users');
  }

  return me;
}

// ===== 监控渲染函数（从 monitor/index.js 移植） =====

async function refreshMonitorData() {
  try {
    const [metricsRes, kpiRes, sessionsRes, alertsRes, cacheRes] = await Promise.all([
      getMetrics().catch(() => null),
      getAdminKPI().catch(() => null),
      getAdminSessions().catch(() => ({ sessions: [] })),
      getAdminAlerts().catch(() => ({ alerts: [] })),
      getAdminCacheStats().catch(() => null),
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

// 监控数据 API 调用（使用 adminApi 以获取管理员权限数据）
async function getAdminKPI() { return adminApi('/api/kpi'); }
async function getAdminSessions() { return adminApi('/api/sessions'); }
async function getAdminAlerts() { return adminApi('/api/alerts?limit=20'); }
async function getAdminCacheStats() { return adminApi('/api/cache/stats'); }

function renderMetricCards(data) {
  if (!data || !data.metrics) return;
  const m = data.metrics;
  const container = document.getElementById('metricCards');
  if (!container) return;
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
  if (!container) return;
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
      <div class="bar-label">${escapeHtml(name)}</div>
    </div>
  `).join('');
}

function renderModeChart(data) {
  if (!data || !data.metrics) return;
  const counts = data.metrics.mode_counts || {};
  const container = document.getElementById('modeChart');
  if (!container) return;
  const modeColors = { sequential: 'info', parallel: 'success', consultation: 'warning', hierarchical: 'error', react: 'primary' };
  const modeLabels = { sequential: '快速通道', parallel: '并行处理', consultation: '专家会诊', hierarchical: '层级协作', react: 'ReAct 推理' };
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
      <div class="bar-label">${escapeHtml(modeLabels[name] || name)}</div>
    </div>
  `).join('');
}

function renderSLA(data) {
  if (!data || !data.metrics?.sla) return;
  const sla = data.metrics.sla;
  const complianceRate = 100 - (sla.violation_rate || 0);
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('slaFill');
  if (fill) {
    fill.setAttribute('stroke-dasharray', `${(complianceRate / 100) * circumference} ${circumference}`);
    fill.setAttribute('stroke', complianceRate >= 90 ? 'var(--success)' : complianceRate >= 70 ? 'var(--warning)' : 'var(--error)');
  }
  const slaValue = document.getElementById('slaValue');
  if (slaValue) slaValue.textContent = `${complianceRate.toFixed(1)}%`;
  const statusEl = document.getElementById('slaStatus');
  if (statusEl) {
    statusEl.textContent = complianceRate >= 90 ? '达标' : complianceRate >= 70 ? '注意' : '告警';
    statusEl.className = `status-tag ${complianceRate >= 90 ? 'healthy' : complianceRate >= 70 ? 'warning' : 'critical'}`;
  }
  const detail = document.getElementById('slaDetail');
  if (detail) detail.textContent = `目标: ${sla.target_min}s - ${sla.target_max}s | 违约: ${sla.violations_slow}次`;
}

function renderCacheStats(data) {
  if (!data) return;
  const hitRate = parseFloat(data.hit_rate) || 0;
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('cacheFill');
  if (fill) fill.setAttribute('stroke-dasharray', `${(hitRate / 100) * circumference} ${circumference}`);
  const cacheValue = document.getElementById('cacheValue');
  if (cacheValue) cacheValue.textContent = `${hitRate}%`;
  const detail = document.getElementById('cacheDetail');
  if (detail) detail.textContent = `L1: ${data.l1_hits || 0}次 / L2: ${data.l2_hits || 0}次 | 大小: L1=${data.l1_size || 0} L2=${data.l2_size || 0}`;
}

function renderKPI(data) {
  if (!data || !data.kpi) return;
  const kpi = data.kpi;
  const circumference = 2 * Math.PI * 42;

  const firstRate = parseFloat(kpi.first_resolution_rate) || 0;
  const resFill = document.getElementById('resolutionFill');
  if (resFill) resFill.setAttribute('stroke-dasharray', `${(firstRate / 100) * circumference} ${circumference}`);
  const resValue = document.getElementById('resolutionValue');
  if (resValue) resValue.textContent = kpi.first_resolution_rate || '--';
  const resDetail = document.getElementById('resolutionDetail');
  if (resDetail) resDetail.textContent = `单轮解决: ${kpi.total_single_turn_resolved || 0} | 多轮: ${kpi.total_multi_turn || 0}`;

  const aiRate = parseFloat(kpi.ai_handled_rate) || 0;
  const aiFill = document.getElementById('aiFill');
  if (aiFill) aiFill.setAttribute('stroke-dasharray', `${(aiRate / 100) * circumference} ${circumference}`);
  const aiValue = document.getElementById('aiValue');
  if (aiValue) aiValue.textContent = kpi.ai_handled_rate || '--';
  const aiDetail = document.getElementById('aiDetail');
  if (aiDetail) aiDetail.textContent = `AI 处理: ${kpi.total_ai_handled || 0} | 转人工: ${kpi.total_escalated || 0}`;
}

function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessionsTableBody');
  if (!tbody) return;
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px">暂无会话</td></tr>';
    return;
  }
  tbody.innerHTML = sessions.slice(0, 10).map(s => {
    const driftWarn = s.drift_escalation;
    return `
      <tr>
        <td style="font-family:monospace;font-size:12px">${escapeHtml((s.session_id || '').slice(0, 12))}...</td>
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
  if (!container) return;
  if (!alerts.length) {
    container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">暂无告警</div>';
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

// ===== 原有管理功能 =====

// ── 用户列表 ──
async function loadUsers() {
  const data = await getUsers();
  if (!data) return;
  document.getElementById('usersTableBody').innerHTML = data.users.map(u => `
    <tr>
      <td>${escapeHtml(String(u.user_id))}</td>
      <td>${escapeHtml(u.username)}</td>
      <td>${escapeHtml(u.display_name || '')}</td>
      <td><span class="tag tag-${escapeHtml(u.role)}">${escapeHtml(u.role)}</span></td>
      <td><span class="tag tag-active">${u.is_active ? '启用' : '禁用'}</span></td>
      <td>${u.created_at ? new Date(u.created_at * 1000).toLocaleString('zh-CN') : '-'}</td>
    </tr>
  `).join('');
}

// ── 知识库 ──
async function loadKnowledgeStats() {
  const data = await getKnowledgeStats();
  if (!data) return;
  const el = document.getElementById('knowledgeStats');
  if (!el) return;
  if (!data.available) {
    el.innerHTML = '<div class="admin-stat"><span class="label">状态</span><span class="value" style="color:#ef4444">不可用</span></div>';
    return;
  }
  const cols = data.collections || {};
  el.innerHTML = `
    <div class="admin-stat"><span class="label">产品知识</span><span class="value">${cols.product_knowledge || 0} 条</span></div>
    <div class="admin-stat"><span class="label">FAQ</span><span class="value">${cols.faq || 0} 条</span></div>
    <div class="admin-stat"><span class="label">技术支持</span><span class="value">${cols.tech_support || 0} 条</span></div>
    <div class="admin-stat"><span class="label">投诉知识</span><span class="value">${cols.complaint_knowledge || 0} 条</span></div>
    <div class="admin-stat"><span class="label">总计</span><span class="value" style="color:#6366f1">${data.total || 0} 条</span></div>
  `;
}

async function reseedKnowledge() {
  if (!confirm('确定重新种子？这会覆盖现有数据。')) return;
  const data = await seedKnowledge();
  showToast(data?.message || '操作完成');
  loadKnowledgeStats();
}

async function syncFromErp() {
  showToast('正在从 ERP 同步...', 'success');
  const data = await syncKnowledge();
  showToast(data?.message || '同步完成');
  loadKnowledgeStats();
}

// ── 告警配置 ──
async function loadAlertConfig() {
  const data = await getAlertConfig();
  if (!data) return;
  const el = document.getElementById('alertConfig');
  if (!el) return;
  el.innerHTML = `
    <div class="admin-stat"><span class="label">Webhook 数量</span><span class="value">${data.webhooks.length} 个</span></div>
    <div class="admin-stat"><span class="label">邮件通知</span><span class="value">${data.email_enabled ? '✅ 已配置' : '❌ 未配置'}</span></div>
    ${data.email_to.length ? `<div class="admin-stat"><span class="label">通知邮箱</span><span class="value">${escapeHtml(data.email_to.join(', '))}</span></div>` : ''}
  `;
}

async function testAlert() {
  const data = await testAlertAPI('测试告警', '这是来自管理后台的测试告警', 'info');
  showToast(data?.message || '测试告警已发送');
}

// ── 审计日志 ──
async function loadAuditLog() {
  const data = await getAuditLog(30);
  if (!data) return;
  const el = document.getElementById('auditTableBody');
  if (!el) return;
  el.innerHTML = data.logs.map(l => `
    <tr>
      <td>${new Date(l.timestamp * 1000).toLocaleString('zh-CN')}</td>
      <td>${escapeHtml(l.action)}</td>
      <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(l.detail || '')}</td>
      <td>${escapeHtml(l.ip_address || '')}</td>
    </tr>
  `).join('');
}

// ── 系统健康 ──
async function loadSystemHealth() {
  try {
    const data = await getHealth();
    const el = document.getElementById('systemHealth');
    if (!el) return;
    const s = data.status || 'unknown';
    const color = s === 'healthy' ? '#4ade80' : s === 'degraded' ? '#fbbf24' : '#f87171';
    const redisInfo = data.components?.redis;
    const dbInfo = data.components?.database;
    el.innerHTML = `
      <div class="admin-stat"><span class="label">状态</span><span class="value" style="color:${color}">${escapeHtml(s)}</span></div>
      <div class="admin-stat"><span class="label">版本</span><span class="value">${escapeHtml(data.version || '-')}</span></div>
      <div class="admin-stat"><span class="label">模式</span><span class="value">${escapeHtml(data.mode || '-')}</span></div>
      <div class="admin-stat"><span class="label">Redis</span><span class="value">${redisInfo?.connected ? `✅ ${redisInfo.latency_ms || ''}ms` : '❌'}</span></div>
      <div class="admin-stat"><span class="label">数据库</span><span class="value">${dbInfo?.connected ? `✅ ${dbInfo.latency_ms || ''}ms` : '❌'}</span></div>
      <div class="admin-stat"><span class="label">ChromaDB</span><span class="value">${data.components?.chromadb?.connected ? '✅' : '❌'}</span></div>
      <div class="admin-stat"><span class="label">熔断器</span><span class="value">${escapeHtml(data.components?.circuit_breaker?.state || '-')}</span></div>
      <div class="admin-stat"><span class="label">LLM</span><span class="value">${data.components?.llm?.configured ? `✅ ${escapeHtml(data.components.llm.provider || '')}` : '❌ 未配置'}</span></div>
    `;
  } catch (e) { console.error('Health check failed:', e); }
}

// ── 运行指标 ──
async function loadMetricsStats() {
  try {
    const resp = await getMetrics();
    if (!resp) return;
    const m = resp.metrics || {};
    const el = document.getElementById('metricsStats');
    if (!el) return;
    el.innerHTML = `
      <div class="admin-stat"><span class="label">总请求</span><span class="value">${m.total_requests || 0}</span></div>
      <div class="admin-stat"><span class="label">错误数</span><span class="value">${m.total_errors || 0}</span></div>
      <div class="admin-stat"><span class="label">平均响应</span><span class="value">${(m.avg_response_time || 0).toFixed(2)}s</span></div>
      <div class="admin-stat"><span class="label">缓存命中率</span><span class="value">${(m.cache_hit_rate || 0).toFixed(1)}%</span></div>
      <div class="admin-stat"><span class="label">SLA 违约率</span><span class="value">${(m.sla?.violation_rate || 0).toFixed(1)}%</span></div>
    `;
  } catch (e) { console.error('Metrics load failed:', e); }
}

// ── 反馈统计 ──
async function loadFeedbackStats() {
  try {
    const resp = await getFeedbackStats();
    if (!resp) return;
    const el = document.getElementById('feedbackStats');
    if (!el) return;
    el.innerHTML = `
      <div class="admin-stat"><span class="label">总反馈</span><span class="value">${resp.total || 0}</span></div>
      <div class="admin-stat"><span class="label">👍 好评</span><span class="value" style="color:#4ade80">${resp.positive || 0}</span></div>
      <div class="admin-stat"><span class="label">👎 差评</span><span class="value" style="color:#f87171">${resp.negative || 0}</span></div>
      <div class="admin-stat"><span class="label">好评率</span><span class="value">${((resp.rate || 0) * 100).toFixed(1)}%</span></div>
    `;
  } catch (e) { console.error('Feedback stats load failed:', e); }
}

// ── 质量趋势（纯CSS柱状图） ──
function renderQualityTrends(data) {
  const el = document.getElementById('qualityTrendsChart');
  if (!el || !data?.trends?.length) return;
  const maxQueries = Math.max(...data.trends.map(t => t.total_queries), 1);
  el.innerHTML = data.trends.map(t => {
    const pct = Math.max((t.total_queries / maxQueries) * 100, 5);
    const score = t.avg_score;
    const color = score > 80 ? '#4ade80' : score >= 60 ? '#fbbf24' : '#f87171';
    const dayLabel = t.date.slice(5);
    return `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
        <span style="font-size:11px;color:var(--text-secondary)">${score}</span>
        <div style="width:100%;height:${pct}%;min-height:4px;background:${color};border-radius:4px 4px 0 0;transition:height .3s"></div>
        <span style="font-size:11px;color:var(--text-muted)">${dayLabel}</span>
      </div>`;
  }).join('');
}

async function loadQualityTrends() {
  try {
    const data = await getQualityTrends();
    if (data) renderQualityTrends(data);
  } catch (e) { console.error('Quality trends load failed:', e); }
}

// ── 热门问题 TOP10 ──
function renderHotQuestions(data) {
  const el = document.getElementById('hotQuestionsList');
  if (!el || !data?.questions?.length) return;
  const maxCount = Math.max(...data.questions.map(q => q.count), 1);
  el.innerHTML = data.questions.map((q, i) => {
    const pct = (q.count / maxCount) * 100;
    return `
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
          <span>${i + 1}. ${escapeHtml(q.query)}</span>
          <span style="color:var(--text-muted)">${q.count}次 · ${escapeHtml(q.category)}</span>
        </div>
        <div style="height:6px;background:var(--bg-elevated);border-radius:3px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:var(--primary);border-radius:3px;transition:width .3s"></div>
        </div>
      </div>`;
  }).join('');
}

async function loadHotQuestions() {
  try {
    const data = await getHotQuestions();
    if (data) renderHotQuestions(data);
  } catch (e) { console.error('Hot questions load failed:', e); }
}

// ── 客户满意度 ──
function renderSatisfaction(data) {
  const el = document.getElementById('satisfactionStats');
  if (!el || !data) return;
  const rate = ((data.overall_rate || 0) * 100).toFixed(1);
  const rateColor = data.overall_rate >= 0.8 ? '#4ade80' : data.overall_rate >= 0.6 ? '#fbbf24' : '#f87171';
  let html = `
    <div style="text-align:center;margin-bottom:16px">
      <div style="font-size:48px;font-weight:700;color:${rateColor}">${rate}%</div>
      <div style="font-size:13px;color:var(--text-muted)">好评 ${data.positive || 0} / 总计 ${data.total || 0}</div>
    </div>`;
  if (data.by_category) {
    for (const [cat, info] of Object.entries(data.by_category)) {
      const pct = ((info.rate || 0) * 100).toFixed(0);
      const barColor = info.rate >= 0.8 ? '#4ade80' : info.rate >= 0.6 ? '#fbbf24' : '#f87171';
      html += `
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
            <span>${escapeHtml(cat)}</span>
            <span style="color:var(--text-muted)">${pct}% · ${info.count}次</span>
          </div>
          <div style="height:6px;background:var(--bg-elevated);border-radius:3px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${barColor};border-radius:3px;transition:width .3s"></div>
          </div>
        </div>`;
    }
  }
  el.innerHTML = html;
}

async function loadSatisfaction() {
  try {
    const data = await getSatisfaction();
    if (data) renderSatisfaction(data);
  } catch (e) { console.error('Satisfaction load failed:', e); }
}

// ── 初始化 ──
const TOKEN = localStorage.getItem('token');

document.addEventListener('DOMContentLoaded', async () => {
  if (!TOKEN) { window.location.href = '/login.html'; return; }

  document.getElementById('logoutBtn').addEventListener('click', logout);
  document.getElementById('btnReseedKnowledge').addEventListener('click', reseedKnowledge);
  document.getElementById('btnSyncFromErp').addEventListener('click', syncFromErp);
  document.getElementById('btnRefreshKnowledge').addEventListener('click', loadKnowledgeStats);
  document.getElementById('btnTestAlert').addEventListener('click', testAlert);
  document.getElementById('btnRefreshMonitor').addEventListener('click', refreshMonitorData);

  // 初始化 section 导航
  initSectionNav();

  // 加载用户信息并根据角色配置界面
  const me = await loadUserInfo();
  const role = me?.role || 'customer';

  // 按角色分组加载数据
  if (role === 'admin') {
    // admin 加载全部
    await Promise.all([loadUsers(), loadKnowledgeStats(), loadAlertConfig(), loadAuditLog()]);
    await Promise.all([loadSystemHealth(), loadMetricsStats(), loadFeedbackStats(), loadQualityTrends(), loadHotQuestions(), loadSatisfaction()]);
  } else if (role === 'supervisor') {
    // supervisor 只加载监控相关
    await Promise.all([loadSystemHealth(), loadMetricsStats(), loadFeedbackStats(), loadAlertConfig()]);
    await Promise.all([loadQualityTrends(), loadHotQuestions(), loadSatisfaction()]);
  }
});
