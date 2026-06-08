/**
 * 管理后台入口
 */
import '../styles/variables.css';
import '../styles/layout.css';
import '../styles/components.css';
import '../styles/admin.css';
import '../styles/animations.css';

const TOKEN = localStorage.getItem('token');
if (!TOKEN) window.location.href = '/login.html';

const headers = {
  'Authorization': 'Bearer ' + TOKEN,
  'Content-Type': 'application/json',
};

function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast ' + type;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

function logout() {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  window.location.href = '/login.html';
}

async function api(path, options = {}) {
  const resp = await fetch(path, { headers, ...options });
  if (resp.status === 401) { logout(); return null; }
  return resp.json();
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ── 用户信息 ──
async function loadUserInfo() {
  const me = await api('/api/auth/me');
  if (me) {
    document.getElementById('userDisplay').textContent = `${me.display_name || me.username} (${me.role})`;
    if (me.role !== 'admin') {
      showToast('需要管理员权限', 'error');
      setTimeout(() => window.location.href = '/', 1000);
    }
  }
}

// ── 用户列表 ──
async function loadUsers() {
  const data = await api('/api/auth/users');
  if (!data) return;
  document.getElementById('usersTableBody').innerHTML = data.users.map(u => `
    <tr>
      <td>${esc(String(u.user_id))}</td>
      <td>${esc(u.username)}</td>
      <td>${esc(u.display_name || '')}</td>
      <td><span class="tag tag-${esc(u.role)}">${esc(u.role)}</span></td>
      <td><span class="tag tag-active">${u.is_active ? '启用' : '禁用'}</span></td>
      <td>${u.created_at ? new Date(u.created_at * 1000).toLocaleString('zh-CN') : '-'}</td>
    </tr>
  `).join('');
}

// ── 知识库 ──
async function loadKnowledgeStats() {
  const data = await api('/api/knowledge/stats');
  if (!data) return;
  const el = document.getElementById('knowledgeStats');
  if (!data.available) {
    el.innerHTML = '<div class="admin-stat"><span class="label">状态</span><span class="value" style="color:#ef4444">不可用</span></div>';
    return;
  }
  el.innerHTML = `
    <div class="admin-stat"><span class="label">产品知识</span><span class="value">${data.collections.product_knowledge} 条</span></div>
    <div class="admin-stat"><span class="label">FAQ</span><span class="value">${data.collections.faq} 条</span></div>
    <div class="admin-stat"><span class="label">技术支持</span><span class="value">${data.collections.tech_support} 条</span></div>
    <div class="admin-stat"><span class="label">总计</span><span class="value" style="color:#6366f1">${data.total} 条</span></div>
  `;
}

async function reseedKnowledge() {
  if (!confirm('确定重新种子？这会覆盖现有数据。')) return;
  const data = await api('/api/knowledge/seed', { method: 'POST' });
  showToast(data?.message || '操作完成');
  loadKnowledgeStats();
}

async function syncFromErp() {
  showToast('正在从 ERP 同步...', 'success');
  const data = await api('/api/knowledge/sync', { method: 'POST' });
  showToast(data?.message || '同步完成');
  loadKnowledgeStats();
}

// ── 告警配置 ──
async function loadAlertConfig() {
  const data = await api('/api/alerts/config');
  if (!data) return;
  document.getElementById('alertConfig').innerHTML = `
    <div class="admin-stat"><span class="label">Webhook 数量</span><span class="value">${data.webhooks.length} 个</span></div>
    <div class="admin-stat"><span class="label">邮件通知</span><span class="value">${data.email_enabled ? '✅ 已配置' : '❌ 未配置'}</span></div>
    ${data.email_to.length ? `<div class="admin-stat"><span class="label">通知邮箱</span><span class="value">${data.email_to.join(', ')}</span></div>` : ''}
  `;
}

async function testAlert() {
  const data = await api('/api/alerts/test', {
    method: 'POST',
    body: JSON.stringify({ title: '测试告警', content: '这是来自管理后台的测试告警', severity: 'info' }),
  });
  showToast(data?.message || '测试告警已发送');
}

// ── 审计日志 ──
async function loadAuditLog() {
  const data = await api('/api/auth/audit?limit=30');
  if (!data) return;
  document.getElementById('auditTableBody').innerHTML = data.logs.map(l => `
    <tr>
      <td>${new Date(l.timestamp * 1000).toLocaleString('zh-CN')}</td>
      <td>${esc(l.action)}</td>
      <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${esc(l.detail || '')}</td>
      <td>${esc(l.ip_address || '')}</td>
    </tr>
  `).join('');
}

// ── 系统健康 ──
async function loadSystemHealth() {
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    const el = document.getElementById('systemHealth');
    if (!el) return;
    const s = data.status || 'unknown';
    const color = s === 'healthy' ? '#4ade80' : s === 'degraded' ? '#fbbf24' : '#f87171';
    el.innerHTML = `
      <div class="admin-stat"><span class="label">状态</span><span class="value" style="color:${color}">${s}</span></div>
      <div class="admin-stat"><span class="label">版本</span><span class="value">${data.version || '-'}</span></div>
      <div class="admin-stat"><span class="label">模式</span><span class="value">${data.mode || '-'}</span></div>
      <div class="admin-stat"><span class="label">Redis</span><span class="value">${data.components?.redis?.connected ? '✅' : '❌'}</span></div>
      <div class="admin-stat"><span class="label">熔断器</span><span class="value">${data.components?.circuit_breaker?.state || '-'}</span></div>
      <div class="admin-stat"><span class="label">LLM</span><span class="value">${data.components?.llm?.configured ? '✅ 已配置' : '❌ 未配置'}</span></div>
    `;
  } catch (e) { console.error('Health check failed:', e); }
}

// ── 运行指标 ──
async function loadMetricsStats() {
  try {
    const resp = await api('/api/metrics');
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
    const resp = await api('/api/feedback/stats');
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

// ── 初始化 ──
document.addEventListener('DOMContentLoaded', async () => {
  if (!TOKEN) return;

  document.getElementById('logoutBtn').addEventListener('click', logout);
  document.getElementById('btnReseedKnowledge').addEventListener('click', reseedKnowledge);
  document.getElementById('btnSyncFromErp').addEventListener('click', syncFromErp);
  document.getElementById('btnRefreshKnowledge').addEventListener('click', loadKnowledgeStats);
  document.getElementById('btnTestAlert').addEventListener('click', testAlert);

  await loadUserInfo();
  await Promise.all([loadUsers(), loadKnowledgeStats(), loadAlertConfig(), loadAuditLog()]);
  await Promise.all([loadSystemHealth(), loadMetricsStats(), loadFeedbackStats()]);
});
