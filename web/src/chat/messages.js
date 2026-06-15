/**
 * 消息渲染模块
 * 包含用户消息、AI 回复、系统消息、打字指示器、进度状态、反馈
 */

import { getSessionId } from '../state/chatState.js';
import { getAgentDisplayName, getAgentIcon } from '../utils/agents.js';
import { copyToClipboard, escapeHtml, scrollToBottom, createElement, setSafeHtml } from '../utils/dom.js';
import { renderMarkdown } from '../utils/markdown.js';

let progressStatusEl = null;
// 会话 ID 从 chatState 读取
let _feedbackSubmitter = null; // v5.0: 解耦反馈提交（由外部注入）
let messageIndex = 0; // 追踪 AI 回复序号（用于 feedback message_index）

/** 注入反馈提交函数（由 chat/index.js 调用，避免直接依赖 API 层） */
export function setFeedbackHandler(submitter) {
  _feedbackSubmitter = submitter;
}

/** 设置当前会话 ID（由 session 模块调用） */
let _localSessionId = null; // 本地缓存用于 messageIndex 重置判断
export function setSessionId(id) {
  _localSessionId = id;
  messageIndex = 0;
}

// ===== 消息渲染 =====

/** 追加用户消息 */
export function appendUserMessage(content, imageFile) {
  const container = document.getElementById('chatMessages');
  
  const contentChildren = [];
  if (imageFile) {
    const objectUrl = URL.createObjectURL(imageFile);
    contentChildren.push(
      createElement('div', { className: 'message-image-preview' }, [
        createElement('img', { src: objectUrl, alt: '用户图片' })
      ])
    );
  }
  if (content) {
    contentChildren.push(createElement('div', { className: 'message-bubble' }, [content]));
  }
  
  const msgEl = createElement('div', { className: 'message user' }, [
    createElement('div', { className: 'message-avatar' }, ['👤']),
    createElement('div', { className: 'message-content' }, contentChildren)
  ]);
  
  container.appendChild(msgEl);
  scrollToBottom(container);
}

/** 追加 AI 回复 */
export function appendAssistantMessage(content, meta = {}) {
  const container = document.getElementById('chatMessages');
  removeTypingIndicator();

  const agentIcon = getAgentIcon(meta.agent);

  const metaParts = [];
  if (meta.elapsed) {
    metaParts.push(createElement('span', { className: 'meta-item' }, [`⏱ ${(meta.elapsed).toFixed(1)}s`]));
  }

  const contentChildren = [
    createElement('div', { className: 'message-bubble' }), // Will setSafeHtml
    // Agent 流转轨迹（客户端不展示）
    createElement('div', { className: 'message-agent-tag' }, [
      createElement('span', { className: 'agent-icon' }, [agentIcon]),
      ` ${getAgentDisplayName(meta.agent)}`
    ]),
    createElement('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' }, [
      createElement('span', { className: 'message-meta' }, metaParts)
    ]),
    createElement('div', { className: 'feedback-bar' }, [
      createElement('button', { className: 'btn-msg-action btn-copy', title: '复制回复内容' }, ['📋 复制']),
      createElement('button', { className: 'btn-feedback positive', title: '有帮助' }, ['👍 有帮助']),
      createElement('button', { className: 'btn-feedback negative', title: '没帮助' }, ['👎 没帮助'])
    ])
  ];

  const msgEl = createElement('div', { className: 'message assistant' }, [
    createElement('div', { className: 'message-avatar' }, [agentIcon]),
    createElement('div', { className: 'message-content' }, contentChildren)
  ]);
  
  const bubble = msgEl.querySelector('.message-bubble');
  setSafeHtml(bubble, renderMarkdown(content));
  
  container.appendChild(msgEl);

  // 绑定反馈和复制按钮事件
  msgEl.dataset.messageIndex = messageIndex++;
  const positiveBtn = msgEl.querySelector('.btn-feedback.positive');
  const negativeBtn = msgEl.querySelector('.btn-feedback.negative');
  const copyBtn = msgEl.querySelector('.btn-copy');
  if (positiveBtn) positiveBtn.addEventListener('click', () => sendFeedback(positiveBtn, true));
  if (negativeBtn) negativeBtn.addEventListener('click', () => sendFeedback(negativeBtn, false));
  if (copyBtn) copyBtn.addEventListener('click', () => copyMessageText(copyBtn, content));

  scrollToBottom(container);
}

/** 追加系统消息 */
export function appendSystemMessage(content) {
  const container = document.getElementById('chatMessages');
  const msgEl = createElement('div', { className: 'message system' }, [
    createElement('div', { className: 'message-avatar' }, ['ℹ️']),
    createElement('div', { className: 'message-content' }, [
      createElement('div', { className: 'message-bubble' }, [content])
    ])
  ]);
  container.appendChild(msgEl);
  scrollToBottom(container);
}

// ===== 打字指示器 =====

export function showTypingIndicator() {
  removeTypingIndicator();
  const container = document.getElementById('chatMessages');
  const typingEl = createElement('div', { className: 'typing-indicator', id: 'typingIndicator' }, [
    createElement('div', { className: 'message-avatar', style: 'background:var(--bg-hover)' }, ['···']),
    createElement('div', { className: 'typing-dots' }, [
      createElement('div', { className: 'typing-dot' }),
      createElement('div', { className: 'typing-dot' }),
      createElement('div', { className: 'typing-dot' })
    ])
  ]);
  container.appendChild(typingEl);
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
    progressStatusEl = createElement('div', { className: 'progress-status' }, [
      createElement('div', { className: 'progress-spinner' }),
      createElement('span', { id: 'progressText' }, [text])
    ]);
    container.appendChild(progressStatusEl);
    scrollToBottom(container);
  } else {
    const textEl = progressStatusEl.querySelector('#progressText');
    if (textEl) textEl.textContent = text;
  }
}

export function removeProgressStatus() {
  if (progressStatusEl) {
    progressStatusEl.remove();
    progressStatusEl = null;
  }
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
  const wrapper = createElement('div', { className: 'message assistant' }, [
    createElement('div', { className: 'message-avatar' }, [agentIcon]),
    createElement('div', { className: 'message-content' }, [
      createElement('div', { className: 'message-bubble' }, [
        createElement('span', { className: 'streaming-text' }),
        createElement('span', { className: 'streaming-cursor' }, ['▊'])
      ]),
      createElement('div', { className: 'message-agent-tag' }, [
        createElement('span', { className: 'agent-icon' }, [agentIcon]),
        ' 客服助手'
      ]),
      createElement('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' }, [
        createElement('span', { className: 'message-meta' })
      ]),
      createElement('div', { className: 'feedback-bar' }, [
        createElement('button', { className: 'btn-msg-action btn-copy', title: '复制回复内容' }, ['📋 复制']),
        createElement('button', { className: 'btn-feedback positive', title: '有帮助' }, ['👍 有帮助']),
        createElement('button', { className: 'btn-feedback negative', title: '没帮助' }, ['👎 没帮助'])
      ])
    ])
  ]);
  
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
      if (cursorSpan.parentNode) cursorSpan.remove();
      setSafeHtml(bubble, renderMarkdown(rawText));

      // 更新元数据
      const agentTag = wrapper.querySelector('.message-agent-tag');
      if (meta.agent) {
        const icon = getAgentIcon(meta.agent);
        agentTag.replaceChildren(
          createElement('span', { className: 'agent-icon' }, [icon]),
          ` ${getAgentDisplayName(meta.agent)}`
        );
      }

      // 更新 meta 行：耗时 + 缓存 + 解决状态
      const metaContainer =
        wrapper.querySelector('.mode-badge')?.parentElement ||
        wrapper.querySelector('.message-content');
      const metaSpan = metaContainer.querySelector('.message-meta');
      if (meta.elapsed && metaSpan) {
        metaSpan.replaceChildren(
          createElement('span', { className: 'meta-item' }, [`⏱ ${(meta.elapsed).toFixed(1)}s`])
        );
      }

      // 追踪 messageIndex
      wrapper.dataset.messageIndex = messageIndex++;

      // 绑定事件
      if (positiveBtn) positiveBtn.addEventListener('click', () => sendFeedback(positiveBtn, true));
      if (negativeBtn)
        negativeBtn.addEventListener('click', () => sendFeedback(negativeBtn, false));
      if (copyBtn) copyBtn.addEventListener('click', () => copyMessageText(copyBtn, rawText));

      // 绑定代码块复制
      bubble.querySelectorAll('.code-copy-btn').forEach((btn) => {
        btn.addEventListener('click', () => copyCodeBlock(btn));
      });

      scrollToBottom(container);
      return rawText;
    },
    getText() {
      return rawText;
    },
  };
}

// ===== 反馈 =====

function sendFeedback(btn, resolved) {
  const sid = _localSessionId || getSessionId();
  if (!sid || !_feedbackSubmitter) return;
  const rating = resolved ? 1 : -1;
  const idx = parseInt(btn.closest('.message')?.dataset.messageIndex || '0', 10);
  _feedbackSubmitter(sid, rating, idx)
    .then(() => {
      const bar = btn.parentElement;
      bar.replaceChildren(createElement('span', { style: 'font-size:11px;color:var(--text-muted)' }, ['✅ 感谢反馈']));
    })
    .catch(() => {});
}

// ===== 复制 =====

function copyMessageText(btn, text) {
  copyToClipboard(text).then((ok) => {
    if (ok) {
      btn.classList.add('copied');
      btn.textContent = '✅ 已复制';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = '📋 复制';
      }, 2000);
    }
  });
}

export function copyCodeBlock(btn) {
  const code = btn.parentElement.querySelector('code');
  if (!code) return;
  copyToClipboard(code.textContent).then((ok) => {
    if (ok) {
      btn.classList.add('copied');
      btn.textContent = '✅';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = '📋';
      }, 2000);
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
