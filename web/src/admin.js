/**
 * 管理后台入口
 */
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/theme-dark.css';
import '../styles/theme-a11y.css';
import '../styles/theme-panel.css';
import '../styles/layout.css';
import '../styles/components.css';
import '../styles/monitor.css';
import '../styles/admin.css';
import '../styles/animations.css';

import {
  loadHotQuestions,
  loadQualityTrends,
  loadSatisfaction,
  refreshMonitorData,
} from './admin-analytics.js';
import {
  handleAddDocs,
  handleCreatePrompt,
  loadAlertConfig,
  loadAlertHistory,
  loadAuditLog,
  loadFeedbackStats,
  loadKnowledgeStats,
  loadMetricsStats,
  loadPromptAgents,
  loadPromptVersions,
  loadSystemHealth,
  loadTokenUsage,
  reseedKnowledge,
  syncFromErp,
  testAlert,
} from './admin-settings.js';
// 导入子模块功能
import { loadUsers } from './admin-users.js';
import { guardPage } from './auth/index.js';
import { initSettingsPanel, initTheme } from './utils/theme.js';
import { showToast } from './utils/toast.js';

// 页面权限守卫：admin 和 supervisor 可访问
if (!guardPage('monitoring')) {
  throw new Error('无权限');
}

let _currentUserRole = null;
let _monitorRefreshTimer = null;

/**
 * 获取认证 Headers
 */
function getHeaders() {
  const token = localStorage.getItem('token') || '';
  const headers = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

/**
 * 退出登录
 */
function logout() {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  window.location.href = '/login.html';
}

/**
 * 加载用户信息并根据角色配置界面
 */
async function loadUserInfo() {
  const headers = getHeaders();
  try {
    const resp = await fetch('/api/auth/me', { headers });
    if (resp.status === 401) {
      logout();
      return null;
    }
    const data = await resp.json();
    const role = data?.role || 'customer';
    _currentUserRole = role;

    // 显示用户标志
    const displayEl = document.getElementById('userDisplay');
    if (displayEl) {
      displayEl.textContent = `${data.username} (${role})`;
    }

    // 根据角色显示/隐藏导航
    document.querySelectorAll('[data-roles]').forEach((el) => {
      const allowed = el.dataset.roles.split(',');
      if (allowed.includes(role)) {
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    });

    return data;
  } catch (_e) {
    showToast('加载用户信息失败', 'error');
    logout();
    return null;
  }
}

/**
 * 切换后台 Section
 */
function switchSection(sectionId) {
  // 隐藏所有 section
  document.querySelectorAll('.admin-section').forEach((el) => {
    el.style.display = 'none';
  });
  // 取消所有导航按钮 active
  document.querySelectorAll('.admin-nav-btn').forEach((btn) => {
    btn.classList.remove('active');
  });

  // 显示目标 section
  const sectionMap = {
    monitor: 'sectionMonitor',
    users: 'sectionUsers',
    knowledge: 'sectionKnowledge',
    prompts: 'sectionPrompts',
    alerts: 'sectionAlerts',
    system: 'sectionSystem',
  };
  const targetId = sectionMap[sectionId];
  if (targetId) {
    const targetEl = document.getElementById(targetId);
    if (targetEl) targetEl.style.display = 'block';
  }

  const activeBtn = document.querySelector(`.admin-nav-btn[data-section="${sectionId}"]`);
  if (activeBtn) activeBtn.classList.add('active');
}

/**
 * 初始化后台导航切换
 */
function initSectionNav() {
  document.querySelectorAll('.admin-nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const sec = btn.dataset.section;
      switchSection(sec);
    });
  });
  switchSection('monitor');
}

const TOKEN = localStorage.getItem('token');

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  initSettingsPanel();

  if (!TOKEN) {
    window.location.href = '/login.html';
    return;
  }

  // 绑定静态操作点击事件
  document.getElementById('logoutBtn')?.addEventListener('click', logout);
  document.getElementById('btnReseedKnowledge')?.addEventListener('click', reseedKnowledge);
  document.getElementById('btnSyncFromErp')?.addEventListener('click', syncFromErp);
  document.getElementById('btnRefreshKnowledge')?.addEventListener('click', loadKnowledgeStats);
  document.getElementById('btnAddDocs')?.addEventListener('click', handleAddDocs);
  document.getElementById('btnTestAlert')?.addEventListener('click', testAlert);
  document.getElementById('btnRefreshMonitor')?.addEventListener('click', refreshMonitorData);
  document.getElementById('btnRefreshAlertHistory')?.addEventListener('click', loadAlertHistory);
  document.getElementById('btnRefreshTokens')?.addEventListener('click', loadTokenUsage);
  document.getElementById('btnRefreshPrompts')?.addEventListener('click', loadPromptVersions);
  document.getElementById('btnCreatePrompt')?.addEventListener('click', handleCreatePrompt);
  document.getElementById('promptAgentSelect')?.addEventListener('change', loadPromptVersions);

  // 初始化 section 导航
  initSectionNav();

  // 加载用户信息并根据角色配置界面
  const me = await loadUserInfo();
  const role = me?.role || 'customer';

  // 按角色分组加载数据
  if (role === 'admin') {
    // admin 加载全部
    await Promise.all([
      loadUsers(),
      loadKnowledgeStats(),
      loadAlertConfig(),
      loadAuditLog(),
      loadPromptAgents(),
    ]);
    await Promise.all([
      loadSystemHealth(),
      loadMetricsStats(),
      loadFeedbackStats(),
      loadQualityTrends(),
      loadHotQuestions(),
      loadSatisfaction(),
      loadAlertHistory(),
      loadTokenUsage(),
    ]);
  } else if (role === 'supervisor') {
    // supervisor 只加载监控和质量指标相关
    await Promise.all([
      loadSystemHealth(),
      loadMetricsStats(),
      loadFeedbackStats(),
      loadAlertConfig(),
    ]);
    await Promise.all([
      loadQualityTrends(),
      loadHotQuestions(),
      loadSatisfaction(),
      loadAlertHistory(),
    ]);
  }

  // 开始定期刷新监控视图
  refreshMonitorData();
  _monitorRefreshTimer = setInterval(refreshMonitorData, 10000);
});
