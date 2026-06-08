/**
 * 主聊天页入口 - 组装所有模块
 */
import { API } from '../api/index.js';
import { initAuthState, logout } from '../auth/index.js';
import { showToast } from '../utils/toast.js';
import { appendSystemMessage, showProgressStatus, removeProgressStatus, setSessionId, initCodeCopyDelegate, appendAssistantMessage, setFeedbackHandler } from './messages.js';
import { getCurrentSessionId, getCurrentSessionToken, updateSessionInfo, addToHistory, startNewChat, loadSessionList, selectSession } from './sessions.js';
import { sendMessage, initInputEvents, initDragAndDrop, updateSendButton, useQuickPrompt } from './input.js';
import { initShortcuts } from './shortcuts.js';
import { bindQuickPrompts } from './welcome.js';
import { initSearch } from './search.js';
import { switchPage, initMonitor } from '../monitor/index.js';

export function init() {
  console.log('[Init] DOM loaded, setting up...');

  // 初始化代码块复制事件委托
  initCodeCopyDelegate();

  // 注入反馈提交函数（解耦 messages.js 对 API 的直接依赖）
  setFeedbackHandler(API.submitRating);

  // 检查登录状态
  const user = initAuthState();
  if (!user) return;

  // 初始化输入事件
  initInputEvents();

  // 初始化快捷键
  initShortcuts();

  // 初始化消息搜索
  initSearch();

  // 初始化拖拽上传
  initDragAndDrop();

  // 注册 WebSocket 事件
  API.on('connected', handleWSConnected);
  API.on('disconnected', handleWSDisconnected);
  API.on('ws_error', handleWSError);
  API.on('status', handleStatus);
  API.on('progress', handleProgress);
  API.on('response', handleResponse);
  API.on('error', handleError);
  API.on('pending', handlePending);

  // 连接 WebSocket
  API.connect();

  // 加载会话列表
  loadSessionList();

  // 初始化导航事件
  document.querySelectorAll('.nav-link[data-page]').forEach(btn => {
    btn.addEventListener('click', () => switchPage(btn.dataset.page));
  });

  // 初始化快捷提问卡片事件
  bindQuickPrompts(useQuickPrompt);

  // 退出按钮
  const logoutBtn = document.getElementById('logoutBtn');
  if (logoutBtn) logoutBtn.addEventListener('click', logout);

  // 新建对话
  const btnNewChat = document.getElementById('btnNewChat');
  if (btnNewChat) btnNewChat.addEventListener('click', startNewChat);

  // 聚焦输入框
  const input = document.getElementById('chatInput');
  if (input) {
    input.focus();
    console.log('[Init] Input focused, ready to type');
  }

  // 设置会话 ID
  setSessionId(getCurrentSessionId());

  // 初始化移动端侧边栏抽屉
  initMobileSidebar();
}

// ===== 移动端侧边栏抽屉 =====

function initMobileSidebar() {
  const menuBtn = document.getElementById('navMenuBtn');
  const sidebar = document.querySelector('.sidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!menuBtn || !sidebar || !backdrop) return;

  function openSidebar() {
    sidebar.classList.add('open');
    backdrop.classList.add('open');
  }
  function closeSidebar() {
    sidebar.classList.remove('open');
    backdrop.classList.remove('open');
  }

  menuBtn.addEventListener('click', openSidebar);
  backdrop.addEventListener('click', closeSidebar);

  // 选择会话时关闭抽屉
  sidebar.addEventListener('click', (e) => {
    if (e.target.closest('.session-item')) closeSidebar();
  });
}

// ===== WebSocket 事件处理 =====

function handleWSConnected() {
  const dot = document.getElementById('wsStatusDot');
  const text = document.getElementById('wsStatusText');
  if (dot) dot.classList.remove('disconnected');
  if (text) text.textContent = '已连接';
}

function handleWSDisconnected(data) {
  const dot = document.getElementById('wsStatusDot');
  const text = document.getElementById('wsStatusText');
  if (dot) dot.classList.add('disconnected');
  if (text) text.textContent = data.code === 1000 ? '已断开' : '连接断开，重连中...';
}

function handleWSError() {
  const dot = document.getElementById('wsStatusDot');
  const text = document.getElementById('wsStatusText');
  if (dot) dot.classList.add('disconnected');
  if (text) text.textContent = '连接失败，重连中...';
}

function handlePending() {
  const text = document.getElementById('wsStatusText');
  if (text) text.textContent = '消息已暂存，等待连接...';
}

function handleStatus(data) {
  showProgressStatus(data.content);
}

function handleProgress(data) {
  showProgressStatus(data.content);
}

function handleResponse(data) {
  removeProgressStatus();

  const content = data.content || '';
  const elapsed = data.elapsed || data.processing_time || 0;
  const mode = data.mode || 'sequential';
  const cached = data.cached || false;
  const agentsUsed = data.agents_used || [];

  // 同步 session 信息
  updateSessionInfo(data.session_id, data.session_token);

  // 渲染 AI 回复
  appendAssistantMessage(content, {
    agent: data.agent || '', elapsed, mode, cached, agentsUsed,
    resolutionStatus: data.resolution_status || '',
  });

  addToHistory({ role: 'assistant', content, agent: data.agent, elapsed, mode, cached });
  loadSessionList();
}

function handleError(data) {
  removeProgressStatus();
  appendSystemMessage(data.content || '发生未知错误');
}
