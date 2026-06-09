/**
 * 欢迎页模板（消除 HTML/JS 重复）
 */

const WELCOME_HTML = `
  <div class="welcome-screen" id="welcomeScreen">
    <div class="welcome-icon">💬</div>
    <h2 class="welcome-title">智能客服助手</h2>
    <p class="welcome-subtitle">
      基于多智能体协作，为您提供产品咨询、技术支持、订单查询、投诉处理等一站式服务。<br>
      支持文字对话、语音输入、图片/文档/视频分析
    </p>
    <div class="quick-prompts">
      <div class="quick-prompt-card" data-title="产品成分查询">
        <div class="quick-prompt-icon">🧴</div>
        <div class="quick-prompt-text">产品成分查询</div>
        <div class="quick-prompt-desc">了解产品成分、功效和适用肤质</div>
      </div>
      <div class="quick-prompt-card" data-title="订单物流追踪">
        <div class="quick-prompt-icon">📦</div>
        <div class="quick-prompt-text">订单物流追踪</div>
        <div class="quick-prompt-desc">查询订单状态、发货和物流信息</div>
      </div>
      <div class="quick-prompt-card" data-title="使用方法指导">
        <div class="quick-prompt-icon">🔧</div>
        <div class="quick-prompt-text">使用方法指导</div>
        <div class="quick-prompt-desc">产品搭配、使用顺序和注意事项</div>
      </div>
      <div class="quick-prompt-card" data-title="投诉与退款">
        <div class="quick-prompt-icon">⚠️</div>
        <div class="quick-prompt-text">投诉与退款</div>
        <div class="quick-prompt-desc">问题反馈、退款退货流程咨询</div>
      </div>
    </div>
  </div>
`;

/** 渲染欢迎页到容器 */
export function renderWelcome(container) {
  container.innerHTML = WELCOME_HTML;
}

/** 绑定快捷提问卡片事件（由外部调用，避免循环依赖） */
export function bindQuickPrompts(onClick) {
  document.querySelectorAll('.quick-prompt-card').forEach(card => {
    card.addEventListener('click', () => onClick(card));
  });
}

/** 隐藏欢迎页 */
export function hideWelcome() {
  const el = document.getElementById('welcomeScreen');
  if (el) el.style.display = 'none';
}
