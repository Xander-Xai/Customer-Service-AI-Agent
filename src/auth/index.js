/**
 * 用户认证状态管理
 */

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

    if (userInfo) userInfo.textContent = `👤 ${user.display_name || user.username}`;
    if (adminLink && user.role === 'admin') adminLink.style.display = 'inline';
    if (logoutBtn) logoutBtn.style.display = 'inline-block';

    return user;
  } catch (e) {
    console.error('[Auth] 用户信息解析失败:', e);
    window.location.href = '/login.html';
    return null;
  }
}

/** 退出登录 */
export function logout() {
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
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    window.location.href = '/login.html';
  }
}
