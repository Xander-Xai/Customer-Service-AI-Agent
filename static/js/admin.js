/**
 * 管理后台交互逻辑（D4）
 * 用户管理 + 系统监控 + 反馈统计
 */

// v4.3 安全加固: HTML 转义防止 XSS
function escapeHtml(str) {
  if (str == null) return '';
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

// ===== 初始化 =====
document.addEventListener('DOMContentLoaded', () => {
  _initAuthState();
  loadDashboard();
});

function _initAuthState() {
  const token = localStorage.getItem('token');
  const userRaw = localStorage.getItem('user');
  if (!token || !userRaw) {
    window.location.href = '/login.html';
    return;
  }
  try {
    const user = JSON.parse(userRaw);
    if (user.role !== 'admin') {
      alert('需要管理员权限');
      window.location.href = '/';
      return;
    }
    const display = document.getElementById('userDisplay');
    if (display) display.textContent = `👤 ${user.display_name || user.username}`;
  } catch (e) {
    window.location.href = '/login.html';
  }
}

function logout() {
  const token = localStorage.getItem('token');
  if (token) {
    fetch('/api/auth/logout', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
    }).catch(() => {}).finally(() => {
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      window.location.href = '/login.html';
    });
  } else {
    window.location.href = '/login.html';
  }
}

// ===== 数据加载 =====
function _authHeaders() {
  const token = localStorage.getItem('token');
  return {
    'Content-Type': 'application/json',
    'Authorization': token ? `Bearer ${token}` : '',
  };
}

async function loadDashboard() {
  await Promise.all([
    loadUsers(),
    loadSystemStats(),
    loadFeedbackStats(),
  ]);
}

// 用户列表
async function loadUsers() {
  const container = document.getElementById('userTableBody');
  if (!container) return;
  try {
    const resp = await fetch('/api/auth/users', { headers: _authHeaders() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const users = data.users || [];
    if (users.length === 0) {
      container.innerHTML = '<tr><td colspan="5" class="empty-state">暂无用户</td></tr>';
      return;
    }
    container.innerHTML = users.map(u => `
      <tr>
        <td>${escapeHtml(String(u.id))}</td>
        <td>${escapeHtml(u.username)}</td>
        <td><span class="tag tag-${escapeHtml(u.role)}">${escapeHtml(u.role)}</span></td>
        <td><span class="tag ${u.is_active ? 'tag-active' : ''}">${u.is_active ? '启用' : '禁用'}</span></td>
        <td>${u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : '-'}</td>
      </tr>
    `).join('');
  } catch (e) {
    container.innerHTML = `<tr><td colspan="5" class="empty-state">加载失败: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// 系统统计
async function loadSystemStats() {
  try {
    const [healthResp, metricsResp] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/metrics', { headers: _authHeaders() }),
    ]);

    if (healthResp.ok) {
      const health = await healthResp.json();
      const el = document.getElementById('systemHealth');
      if (el) {
        const status = health.status || 'unknown';
        const color = status === 'healthy' ? '#4ade80' : status === 'degraded' ? '#fbbf24' : '#f87171';
        el.innerHTML = `
          <div class="admin-stat"><span class="label">状态</span><span class="value" style="color:${color}">${escapeHtml(status)}</span></div>
          <div class="admin-stat"><span class="label">版本</span><span class="value">${escapeHtml(health.version) || '-'}</span></div>
          <div class="admin-stat"><span class="label">模式</span><span class="value">${escapeHtml(health.mode) || '-'}</span></div>
          <div class="admin-stat"><span class="label">Redis</span><span class="value">${health.components?.redis?.connected ? '✅ 连接' : '❌ 断开'}</span></div>
          <div class="admin-stat"><span class="label">熔断器</span><span class="value">${escapeHtml(health.components?.circuit_breaker?.state) || '-'}</span></div>
        `;
      }
    }

    if (metricsResp.ok) {
      const metrics = await metricsResp.json();
      const m = metrics.metrics || {};
      const el = document.getElementById('metricsStats');
      if (el) {
        el.innerHTML = `
          <div class="admin-stat"><span class="label">总请求</span><span class="value">${m.total_requests || 0}</span></div>
          <div class="admin-stat"><span class="label">错误数</span><span class="value">${m.total_errors || 0}</span></div>
          <div class="admin-stat"><span class="label">平均响应</span><span class="value">${(m.avg_response_time || 0).toFixed(2)}s</span></div>
          <div class="admin-stat"><span class="label">缓存命中率</span><span class="value">${(m.cache_hit_rate || 0).toFixed(1)}%</span></div>
          <div class="admin-stat"><span class="label">SLA 违约率</span><span class="value">${(m.sla?.violation_rate || 0).toFixed(1)}%</span></div>
        `;
      }
    }
  } catch (e) {
    console.error('加载系统统计失败:', e);
  }
}

// 反馈统计
async function loadFeedbackStats() {
  try {
    const resp = await fetch('/api/feedback/stats', { headers: _authHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    const el = document.getElementById('feedbackStats');
    if (el) {
      el.innerHTML = `
        <div class="admin-stat"><span class="label">总反馈</span><span class="value">${data.total || 0}</span></div>
        <div class="admin-stat"><span class="label">👍 好评</span><span class="value" style="color:#4ade80">${data.positive || 0}</span></div>
        <div class="admin-stat"><span class="label">👎 差评</span><span class="value" style="color:#f87171">${data.negative || 0}</span></div>
        <div class="admin-stat"><span class="label">好评率</span><span class="value">${((data.rate || 0) * 100).toFixed(1)}%</span></div>
      `;
    }
  } catch (e) {
    console.error('加载反馈统计失败:', e);
  }
}

// ===== Toast 提示 =====
function showToast(message, type = 'success') {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.style.display = 'block';
  setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

// ===== 页面自动刷新 =====
let _refreshTimer = setInterval(loadDashboard, 30000); // 每 30 秒刷新
