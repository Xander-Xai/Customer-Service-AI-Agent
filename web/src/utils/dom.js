/**
 * DOM 工具函数
 */

/** HTML 转义（防 XSS） */
export function escapeHtml(text) {
  if (text == null) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 平滑滚动到底部 */
export function scrollToBottom(container) {
  if (!container) container = document.getElementById('chatMessages');
  if (container) {
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  }
}

/** 复制文本到剪贴板（带回退） */
export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 回退方案
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  }
}

/** 安全创建 DOM 元素 */
export function createElement(tag, attributes = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === 'className') {
      el.className = value;
    } else if (key === 'style' && typeof value === 'object') {
      for (const [sKey, sValue] of Object.entries(value)) el.style[sKey] = sValue;
    } else if (key === 'style' && typeof value === 'string') {
      el.style.cssText = value;
    } else if (key === 'dataset' && typeof value === 'object') {
      for (const [dKey, dValue] of Object.entries(value)) el.dataset[dKey] = dValue;
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== null && value !== undefined) {
      el.setAttribute(key, value);
    }
  }
  for (const child of children) {
    if (!child) continue;
    if (typeof child === 'string' || typeof child === 'number') {
      el.appendChild(document.createTextNode(String(child)));
    } else if (child instanceof Node) {
      el.appendChild(child);
    }
  }
  return el;
}

/** 安全插入 HTML 字符串（通过 DOMParser，避免直接使用 innerHTML） */
export function setSafeHtml(element, htmlString) {
  const doc = new DOMParser().parseFromString(htmlString, 'text/html');
  element.replaceChildren(...doc.body.childNodes);
}
