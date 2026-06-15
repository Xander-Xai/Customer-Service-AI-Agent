/**
 * 欢迎页模板
 * 面向客户的服务入口，文案来自 copy.js
 */
import { CHAT, QUICK_PROMPTS } from '../utils/copy.js';
import { setSafeHtml } from '../utils/dom.js';

const WELCOME_HTML = `
  <div class="welcome-screen" id="welcomeScreen">
    <div class="welcome-icon">💬</div>
    <h2 class="welcome-title">${CHAT.welcomeTitle}</h2>
    <p class="welcome-subtitle">${CHAT.welcomeSubtitle.replace(/\n/g, '<br>')}</p>
    <div class="quick-prompts">
      ${QUICK_PROMPTS.map(
        (p) => `
        <div class="quick-prompt-card" data-title="${p.title}">
          <div class="quick-prompt-icon">${p.icon}</div>
          <div class="quick-prompt-text">${p.title}</div>
          <div class="quick-prompt-desc">${p.desc}</div>
        </div>
      `,
      ).join('')}
    </div>
  </div>
`;

/** 渲染欢迎页到容器 */
export function renderWelcome(container) {
  setSafeHtml(container, WELCOME_HTML);
}

/** 绑定快捷提问卡片事件（由外部调用，避免循环依赖） */
export function bindQuickPrompts(onClick) {
  document.querySelectorAll('.quick-prompt-card').forEach((card) => {
    card.addEventListener('click', () => onClick(card));
  });
}

/** 隐藏欢迎页 */
export function hideWelcome() {
  const el = document.getElementById('welcomeScreen');
  if (el) el.style.display = 'none';
}
