/**
 * Markdown 渲染（marked.js + DOMPurify）
 */
import { marked } from 'marked';
import DOMPurify from 'dompurify';

/** 轻量清洗：后端已做完整清洗，前端仅处理传输噪声 */
function sanitizeResponse(text) {
  if (!text) return '';
  text = text.replace(/^(system\s*system|system)\s*/i, '');
  text = text.replace(/\n{3,}/g, '\n\n');
  return text.trim();
}

/** 配置 marked */
const renderer = new marked.Renderer();

// 代码块：保留语言标签 + 复制按钮
renderer.code = function ({ text, lang }) {
  const escapedCode = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  const langLabel = lang
    ? `<span style="font-size:11px;color:var(--text-muted);position:absolute;top:6px;left:12px">${lang}</span>`
    : '';
  return `<div class="code-block-wrapper">${langLabel}<button class="code-copy-btn">📋</button><pre><code>${escapedCode}</code></pre></div>`;
};

// 链接：新窗口打开
renderer.link = function ({ href, text }) {
  const safeHref = escapeAttr(href || '');
  return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${text}</a>`;
};

// 表格：添加样式类
renderer.table = function ({ header, body }) {
  return `<div class="table-wrapper"><table class="markdown-table"><thead>${header}</thead><tbody>${body}</tbody></table></div>`;
};

marked.setOptions({ renderer, breaks: true, gfm: true });

/** 安全转义属性值 */
function escapeAttr(str) {
  return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * 渲染 Markdown 文本为安全 HTML
 * @param {string} text - 原始文本
 * @returns {string} 安全的 HTML 字符串
 */
export function renderMarkdown(text) {
  if (!text) return '';
  text = sanitizeResponse(text);
  const rawHtml = marked.parse(text);
  return DOMPurify.sanitize(rawHtml, {
    ADD_TAGS: ['button'],
    ADD_ATTR: ['class', 'data-lang'],
    ALLOW_DATA_ATTR: true,
  });
}
