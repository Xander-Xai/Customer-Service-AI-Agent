/**
 * 消息渲染模块
 * 包含用户消息、AI 回复、系统消息、打字指示器、进度状态、反馈
 */
import { renderMarkdown } from '../utils/markdown.js';
import { escapeHtml, scrollToBottom, copyToClipboard } from '../utils/dom.js';
import { getAgentIcon, getModeLabel } from '../utils/agents.js';

let progressStatusEl = null;
let currentSessionId = null;
let _feedbackSubmitter = null;  // v5.0: 解耦反馈提交（由外部注入）

/** 注入反馈提交函数（由 chat/index.js 调用，避免直接依赖 API 层） */
export function setFeedbackHandler(submitter) {
  _feedbackSubmitter = submitter;
}

/** 设置当前会话 ID（由 session 模块调用） */
export function setSessionId(id) { currentSessionId = id; }

// ===== 消息渲染 =====

/** 追加用户消息 */
export function appendUserMessage(content, imageFile) {
  const container = document.getElementById('chatMessages');
  let imageHtml = '';
  if (imageFile) {
    const objectUrl = URL.createObjectURL(imageFile);
    imageHtml = `<div class="message-image-preview"><img src="${objectUrl}" alt="用户图片"></div>`;
  }
  const html = `
    <div class="message user">
      <div class="message-avatar">👤</div>
      <div class="message-content">
        ${imageHtml}
        ${content ? `<div class="message-bubble">${escapeHtml(content)}</div>` : ''}
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);
  scrollToBottom(container);
}

/** 追加 AI 回复 */
export function appendAssistantMessage(content, meta = {}) {
  const container = document.getElementById('chatMessages');
  removeTypingIndicator();

  const agentIcon = getAgentIcon(meta.agent);

  // Agent 流转轨迹
  let agentFlowHtml = '';
  if (meta.agentsUsed && meta.agentsUsed.length > 1) {
    const steps = meta.agentsUsed.map((a, i) => {
      const icon = getAgentIcon(a);
      const cls = i === meta.agentsUsed.length - 1 ? 'active' : 'completed';
      return `<div class="agent-flow-step ${cls}">${icon} ${escapeHtml(a)}</div>`;
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
          ${escapeHtml(meta.agent || 'AI 助手')}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="mode-badge ${meta.mode || 'sequential'}">
            ${escapeHtml(getModeLabel(meta.mode))}
          </span>
          ${meta.cached ? '<span class="cached-badge">⚡ 缓存命中</span>' : ''}
          <span class="message-meta">
            ${meta.elapsed ? `<span class="meta-item">⏱ ${(meta.elapsed).toFixed(1)}s</span>` : ''}
          </span>
        </div>
        <div class="feedback-bar">
          <button class="btn-msg-action btn-copy" title="复制回复内容">📋 复制</button>
          <button class="btn-feedback positive" title="有帮助">👍 有帮助</button>
          <button class="btn-feedback negative" title="没帮助">👎 没帮助</button>
        </div>
      </div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', html);

  // 绑定反馈和复制按钮事件
  const lastMsg = container.lastElementChild;
  if (lastMsg) {
    const positiveBtn = lastMsg.querySelector('.btn-feedback.positive');
    const negativeBtn = lastMsg.querySelector('.btn-feedback.negative');
    const copyBtn = lastMsg.querySelector('.btn-copy');
    if (positiveBtn) positiveBtn.addEventListener('click', () => sendFeedback(positiveBtn, true));
    if (negativeBtn) negativeBtn.addEventListener('click', () => sendFeedback(negativeBtn, false));
    if (copyBtn) copyBtn.addEventListener('click', () => copyMessageText(copyBtn, content));
  }

  scrollToBottom(container);
}

/** 追加系统消息 */
export function appendSystemMessage(content) {
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
  scrollToBottom(container);
}

// ===== 打字指示器 =====

export function showTypingIndicator() {
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
  scrollToBottom(container);
}

export function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

// ===== 进度状态 =====

export function showProgressStatus(text) {
  removeTypingIndicator();
  if (!progressStatusEl) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'progress-status';
    div.innerHTML = `<div class="progress-spinner"></div><span id="progressText">${escapeHtml(text)}</span>`;
    container.appendChild(div);
    progressStatusEl = div;
    scrollToBottom(container);
  } else {
    const textEl = progressStatusEl.querySelector('#progressText');
    if (textEl) textEl.textContent = text;
  }
}

export function removeProgressStatus() {
  if (progressStatusEl) { progressStatusEl.remove(); progressStatusEl = null; }
}

// ===== SSE 流式消息 =====

/**
 * 创建流式消息气泡（SSE 模式）
 * @returns {{ bubble: HTMLElement, cursor: HTMLElement, setContent: Function, finalize: Function }}
 */
export function createStreamingMessage() {
  const container = document.getElementById('chatMessages');
  removeTypingIndicator();

  const agentIcon = '🤖';
  const wrapper = document.createElement('div');
  wrapper.className = 'message assistant';
  wrapper.innerHTML = `
    <div class="message-avatar">${agentIcon}</div>
    <div class="message-content">
      <div class="message-bubble"><span class="streaming-text"></span><span class="streaming-cursor">▊</span></div>
      <div class="message-agent-tag"><span class="agent-icon">${agentIcon}</span> AI 助手</div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="mode-badge sequential">快速通道</span>
        <span class="message-meta"></span>
      </div>
      <div class="feedback-bar">
        <button class="btn-msg-action btn-copy" title="复制回复内容">📋 复制</button>
        <button class="btn-feedback positive" title="有帮助">👍 有帮助</button>
        <button class="btn-feedback negative" title="没帮助">👎 没帮助</button>
      </div>
    </div>
  `;
  container.appendChild(wrapper);
  scrollToBottom(container);

  const textSpan = wrapper.querySelector('.streaming-text');
  const cursorSpan = wrapper.querySelector('.streaming-cursor');
  const bubble = wrapper.querySelector('.message-bubble');
  let rawText = '';

  // 绑定反馈按钮
  const positiveBtn = wrapper.querySelector('.btn-feedback.positive');
  const negativeBtn = wrapper.querySelector('.btn-feedback.negative');
  const copyBtn = wrapper.querySelector('.btn-copy');

  return {
    appendChunk(chunk) {
      rawText += chunk;
      textSpan.textContent = rawText;
      scrollToBottom(container);
    },
    finalize(meta = {}) {
      // 移除光标，渲染完整 Markdown
      cursorSpan.remove();
      bubble.innerHTML = renderMarkdown(rawText);

      // 更新元数据
      const agentTag = wrapper.querySelector('.message-agent-tag');
      if (meta.agent) {
        const icon = getAgentIcon(meta.agent);
        agentTag.innerHTML = `<span class="agent-icon">${icon}</span> ${escapeHtml(meta.agent)}`;
      }

      const modeBadge = wrapper.querySelector('.mode-badge');
      if (meta.mode) {
        modeBadge.className = `mode-badge ${meta.mode}`;
        modeBadge.textContent = getModeLabel(meta.mode);
      }

      const metaSpan = wrapper.querySelector('.message-meta');
      if (meta.elapsed) {
        metaSpan.innerHTML = `<span class="meta-item">⏱ ${(meta.elapsed).toFixed(1)}s</span>`;
      }

      // 绑定事件
      if (positiveBtn) positiveBtn.addEventListener('click', () => sendFeedback(positiveBtn, true));
      if (negativeBtn) negativeBtn.addEventListener('click', () => sendFeedback(negativeBtn, false));
      if (copyBtn) copyBtn.addEventListener('click', () => copyMessageText(copyBtn, rawText));

      // 绑定代码块复制
      bubble.querySelectorAll('.code-copy-btn').forEach(btn => {
        btn.addEventListener('click', () => copyCodeBlock(btn));
      });

      scrollToBottom(container);
      return rawText;
    },
    getText() { return rawText; },
  };
}

// ===== 反馈 =====

function sendFeedback(btn, resolved) {
  if (!currentSessionId || !_feedbackSubmitter) return;
  const rating = resolved ? 1 : -1;
  _feedbackSubmitter(currentSessionId, rating).then(() => {
    const bar = btn.parentElement;
    bar.innerHTML = '<span style="font-size:11px;color:var(--text-muted)">✅ 感谢反馈</span>';
  }).catch(() => {});
}

// ===== 复制 =====

function copyMessageText(btn, text) {
  copyToClipboard(text).then(ok => {
    if (ok) {
      btn.classList.add('copied');
      btn.textContent = '✅ 已复制';
      setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 复制'; }, 2000);
    }
  });
}

export function copyCodeBlock(btn) {
  const code = btn.parentElement.querySelector('code');
  if (!code) return;
  copyToClipboard(code.textContent).then(ok => {
    if (ok) {
      btn.classList.add('copied');
      btn.textContent = '✅';
      setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋'; }, 2000);
    }
  });
}

/** 初始化代码块复制按钮事件委托 */
export function initCodeCopyDelegate() {
  const container = document.getElementById('chatMessages');
  if (!container) return;
  container.addEventListener('click', (e) => {
    const btn = e.target.closest('.code-copy-btn');
    if (btn) copyCodeBlock(btn);
  });
}
