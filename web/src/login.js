/**
 * 登录页入口
 */
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/theme-dark.css';
import '../styles/theme-a11y.css';
import '../styles/theme-panel.css';
import '../styles/login.css';
import '../styles/animations.css';
import { initSettingsPanel, initTheme } from './utils/theme.js';

let isLogin = true;

/** 从 cookie 中读取指定名称的值 */
function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
  return match ? match[1] : '';
}

function toggleMode() {
  isLogin = !isLogin;
  document.getElementById('formTitle').textContent = isLogin
    ? '智能客服系统 · 登录'
    : '智能客服系统 · 注册';
  document.getElementById('submitBtn').textContent = isLogin ? '登录' : '注册';
  document.getElementById('displayNameGroup').style.display = isLogin ? 'none' : 'block';
  document.getElementById('switchText').textContent = isLogin ? '没有账号？' : '已有账号？';
  document.getElementById('switchLink').textContent = isLogin ? '注册' : '登录';
  document.getElementById('errorMsg').style.display = 'none';
  document.getElementById('password').value = '';
}

async function handleSubmit(e) {
  e.preventDefault();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  const errorMsg = document.getElementById('errorMsg');
  const submitBtn = document.getElementById('submitBtn');

  errorMsg.style.display = 'none';
  submitBtn.disabled = true;
  submitBtn.textContent = isLogin ? '登录中...' : '注册中...';

  try {
    const endpoint = isLogin ? '/api/auth/login' : '/api/auth/register';
    const body = { username, password };
    if (!isLogin) body.display_name = document.getElementById('displayName').value.trim();

    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': getCsrfToken(),
      },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || data.error || '操作失败');

    if (isLogin) {
      localStorage.setItem('token', data.token);
      if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token);
      localStorage.setItem(
        'user',
        JSON.stringify({
          user_id: data.user_id,
          username: data.username,
          role: data.role,
          display_name: data.display_name,
        }),
      );
      const redirectMap = {
        customer: '/',
        agent: '/',
        supervisor: '/admin.html',
        admin: '/admin.html',
      };
      window.location.href = redirectMap[data.role] || '/';
    } else {
      toggleMode();
      document.getElementById('username').value = username;
      document.getElementById('password').value = password;
      errorMsg.textContent = '注册成功！请登录';
      errorMsg.style.color = '#22c55e';
      errorMsg.style.display = 'block';
    }
  } catch (err) {
    errorMsg.textContent = err.message;
    errorMsg.style.color = '#ef4444';
    errorMsg.style.display = 'block';
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = isLogin ? '登录' : '注册';
  }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initSettingsPanel();

  // 已登录则跳转
  const token = localStorage.getItem('token');
  const userRaw = localStorage.getItem('user');
  if (token) {
    try {
      const u = userRaw ? JSON.parse(userRaw) : {};
      const redirectMap = {
        customer: '/',
        agent: '/',
        supervisor: '/admin.html',
        admin: '/admin.html',
      };
      window.location.href = redirectMap[u.role] || '/';
    } catch {
      window.location.href = '/';
    }
    return;
  }

  document.getElementById('authForm').addEventListener('submit', handleSubmit);
  document.getElementById('switchLink').addEventListener('click', toggleMode);

  // 测试账号提示始终显示
});
