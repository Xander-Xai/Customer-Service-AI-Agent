/**
 * 消息搜索模块
 */

let _debounceTimer = null;

/** 初始化消息搜索 */
export function initSearch() {
  const input = document.getElementById('searchInput');
  if (!input) return;

  input.addEventListener('input', () => {
    clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(() => highlightMatches(input.value.trim()), 200);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      input.value = '';
      clearHighlights();
      input.blur();
    }
  });
}

function highlightMatches(query) {
  clearHighlights();
  if (!query) return;

  const lowerQuery = query.toLowerCase();
  const bubbles = document.querySelectorAll('.message-bubble');
  let firstMatch = null;

  bubbles.forEach((bubble) => {
    const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT, null);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);

    textNodes.forEach((node) => {
      const text = node.textContent;
      const lowerText = text.toLowerCase();
      if (!lowerText.includes(lowerQuery)) return;

      const fragment = document.createDocumentFragment();
      let lastIndex = 0;
      let idx = lowerText.indexOf(lowerQuery);

      while (idx !== -1) {
        // 前面的普通文本
        if (idx > lastIndex) {
          fragment.appendChild(document.createTextNode(text.slice(lastIndex, idx)));
        }
        // 匹配的高亮文本
        const mark = document.createElement('mark');
        mark.className = 'search-highlight';
        mark.textContent = text.slice(idx, idx + query.length);
        fragment.appendChild(mark);
        if (!firstMatch) firstMatch = mark;

        lastIndex = idx + query.length;
        idx = lowerText.indexOf(lowerQuery, lastIndex);
      }

      // 剩余文本
      if (lastIndex < text.length) {
        fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
      }

      node.parentNode.replaceChild(fragment, node);
    });
  });

  // 滚动到第一个匹配
  if (firstMatch) {
    firstMatch.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function clearHighlights() {
  document.querySelectorAll('.search-highlight').forEach((mark) => {
    const text = document.createTextNode(mark.textContent);
    mark.parentNode.replaceChild(text, mark);
  });
  // 合并相邻文本节点
  document.querySelectorAll('.message-bubble').forEach((bubble) => {
    bubble.normalize();
  });
}
