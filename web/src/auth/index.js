import { ROLE_LABELS } from '../utils/copy.js';

/**
 * 用户认证状态管理（含 Token 自动刷新）
 */

/** 解析 JWT payload（不验证签名，仅读取 exp） */
function parseJWT(token) {
  try {
    const payload = token.split('.')[1];
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    return null;
  }
}

/** 尝试刷新 access token */
async function refreshToken() {
  const rt = localStorage.getItem('refresh_token');
  if (!rt) return false;

  try {
    const resp = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!resp.ok) return false;

    const data = await resp.json();
    if (data.access_token) {
      localStorage.setItem('token', data.access_token);
      // 如果返回了新的 refresh_token，也更新
      if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token);
      scheduleTokenRefresh(); // 重新调度下一次刷新
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

/** 安排 Token 自动刷新（过期前 5 分钟） */
let refreshTimer = null;
let _isLoggingOut = false; // 防递归：防止 logout → redirect 触发再次进入过期处理

function scheduleTokenRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);

  const token = localStorage.getItem('token');
  if (!token) return;

  const payload = parseJWT(token);
  if (!payload?.exp) return;

  const expiresAt = payload.exp * 1000; // ms
  const refreshAt = expiresAt - Date.now() - 5 * 60 * 1000; // 提前 5 分钟

  if (refreshAt <= 0) {
    // 已过期或即将过期，立即刷新
    refreshToken();
    return;
  }

  refreshTimer = setTimeout(() => refreshToken(), refreshAt);
}

/**
 * 全局 401 拦截器包装 fetch
 * 当请求返回 401 时，尝试刷新 token 后重试一次
 */
export async function fetchWithAuth(url, options = {}) {
  const token = localStorage.getItem('token');
  if (token && !options.headers?.Authorization) {
    options.headers = { ...options.headers, Authorization: `Bearer ${token}` };
  }

  let resp = await fetch(url, options);

  if (resp.status === 401 && localStorage.getItem('refresh_token')) {
    const refreshed = await refreshToken();
    if (refreshed) {
      // 用新 token 重试
      const newToken = localStorage.getItem('token');
      options.headers = { ...options.headers, Authorization: `Bearer ${newToken}` };
      resp = await fetch(url, options);
    }
  }

  if (resp.status === 401 && !_isLoggingOut) {
    _isLoggingOut = true;
    window.dispatchEvent(new CustomEvent('auth:expired', { detail: '登录已过期，请重新登录' }));
    logout();
  }

  return resp;
}

/** 初始化认证状态（检查登录、显示用户信息） */
export function initAuthState() {
  const token = localStorage.getItem('token');
  const userRaw = localStorage.getItem('user');

  if (!token || !userRaw) {
    window.location.href = '/login.html';
    return null;
  }

  try {
    const user = JSON.parse(userRaw);
    const userInfo = document.getElementById('userInfo');
    const adminLink = document.getElementById('adminLink');
    const logoutBtn = document.getElementById('logoutBtn');

    if (userInfo) {
      const roleLabels = ROLE_LABELS;
      const roleLabel = roleLabels[user.role] || user.role;
      userInfo.textContent = `👤 ${user.display_name || user.username} [${roleLabel}]`;
    }
    if (adminLink && (user.role === 'admin' || user.role === 'supervisor'))
      adminLink.style.display = 'inline';
    if (logoutBtn) logoutBtn.style.display = 'inline-block';

    // 安排 Token 自动刷新
    scheduleTokenRefresh();

    return user;
  } catch (_e) {
    window.location.href = '/login.html';
    return null;
  }
}

/** 退出登录 */
export function logout() {
  if (refreshTimer) clearTimeout(refreshTimer);

  const token = localStorage.getItem('token');
  if (token) {
    fetch('/api/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
      .catch(() => {})
      .finally(() => {
        _clearAuth();
      });
  } else {
    _clearAuth();
  }
}

function _clearAuth() {
  localStorage.removeItem('token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('user');
  localStorage.removeItem('currentSessionId');
  localStorage.removeItem('currentSessionToken');
  window.location.href = '/login.html';
}

/** 获取当前用户的能力列表（基于角色） */
export function getCapabilities() {
  try {
    const userRaw = localStorage.getItem('user');
    if (!userRaw) return [];
    const user = JSON.parse(userRaw);
    const role = user.role || 'customer';

    const capabilities = ['chat']; // 所有角色都有聊天能力

    if (role === 'supervisor') {
      capabilities.push('monitoring', 'alert_management');
    }
    if (role === 'admin') {
      capabilities.push(
        'monitoring',
        'user_management',
        'knowledge_management',
        'alert_management',
        'system_config',
      );
    }

    return capabilities;
  } catch {
    return ['chat'];
  }
}

/** 页面权限守卫：检查当前用户是否有权访问页面 */
export function guardPage(requiredCapability) {
  const user = initAuthState();
  if (!user) return false;

  const caps = getCapabilities();
  if (!caps.includes(requiredCapability)) {
    // 安全构建无权限提示（不使用 innerHTML）
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.alignItems = 'center';
    wrap.style.justifyContent = 'center';
    wrap.style.height = '100vh';
    wrap.style.background = 'var(--bg-base,#F7F6F3)';
    wrap.style.color = 'var(--text-secondary,#6B6B6B)';
    wrap.style.fontSize = '16px';
    wrap.style.fontFamily = 'SF Pro Display,-apple-system,BlinkMacSystemFont,sans-serif';

    const inner = document.createElement('div');
    inner.style.textAlign = 'center';

    const icon = document.createElement('div');
    icon.style.fontSize = '48px';
    icon.style.marginBottom = '16px';
    icon.style.opacity = '0.3';
    icon.textContent = '◆';
    inner.appendChild(icon);

    const msg = document.createElement('div');
    msg.textContent = '您没有权限访问此页面';
    inner.appendChild(msg);

    const link = document.createElement('a');
    link.href = '/';
    link.style.color = 'var(--primary,#1A1A1A)';
    link.style.marginTop = '12px';
    link.style.display = 'inline-block';
    link.style.textDecoration = 'underline';
    link.textContent = '返回首页';
    inner.appendChild(link);

    wrap.appendChild(inner);

    // 替换 body 内容
    document.body.replaceChildren(wrap);
    return false;
  }
  return true;
}
