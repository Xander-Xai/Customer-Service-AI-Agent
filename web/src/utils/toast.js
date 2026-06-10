/**
 * Toast 通知系统
 */

let _container = null;

function getOrCreateContainer() {
  if (_container && document.body.contains(_container)) return _container;
  _container = document.createElement('div');
  _container.id = 'toastContainer';
  _container.style.cssText =
    'position:fixed;top:70px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:380px';
  _container.setAttribute('aria-live', 'polite');
  document.body.appendChild(_container);
  return _container;
}

function typeIcon(type) {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  return icons[type] || 'ℹ️';
}

function typeBg(type) {
  const bgs = {
    success: '#346538',
    error: '#9F2F2D',
    warning: '#956400',
    info: '#1F6C9F',
  };
  return bgs[type] || bgs.info;
}

function removeToast(toast) {
  toast.style.opacity = '0';
  toast.style.transform = 'translateX(100%)';
  setTimeout(() => toast.remove(), 200);
}

/**
 * 显示 Toast 通知
 * @param {string} message - 消息文本
 * @param {'info'|'success'|'warning'|'error'} type - 类型
 * @param {number} duration - 自动消失时间（ms），0 表示不自动消失
 */
export function showToast(message, type = 'info', duration = 3000) {
  const container = getOrCreateContainer();
  const toast = document.createElement('div');
  toast.className = 'toast-item';
  toast.setAttribute('role', 'alert');
  toast.setAttribute('aria-live', 'assertive');
  toast.style.cssText = `padding:12px 16px;border-radius:10px;color:#fff;font-size:13px;display:flex;align-items:center;gap:8px;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:all 250ms cubic-bezier(0.16,1,0.3,1);background:${typeBg(type)}`;

  toast.innerHTML = `
    <span>${typeIcon(type)}</span>
    <span style="flex:1">${escapeHtml(message)}</span>
    <button style="background:none;border:none;color:rgba(255,255,255,0.6);cursor:pointer;font-size:14px;padding:0 2px" aria-label="关闭">✕</button>
  `;

  toast.querySelector('button').addEventListener('click', () => removeToast(toast));
  container.appendChild(toast);
  if (duration > 0) setTimeout(() => removeToast(toast), duration);

  return toast;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
