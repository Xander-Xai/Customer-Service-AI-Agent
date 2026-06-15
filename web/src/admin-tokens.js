/**
 * Token 用量统计模块
 * 从 admin-settings.js 提取
 */
import { getTokenUsage } from './api/rest.js';

/** 加载 Token 用量统计 */
export async function loadTokenUsage() {
  try {
    const data = await getTokenUsage();
    const el = document.getElementById('tokenUsageStats');
    if (!el) return;
    if (data?.error) {
      el.replaceChildren();
      const errEl = document.createElement('div');
      errEl.style.color = 'var(--text-muted)';
      errEl.style.textAlign = 'center';
      errEl.style.padding = '20px';
      errEl.textContent = data.error;
      el.appendChild(errEl);
      return;
    }
    const g = data?.global || {};

    el.replaceChildren();

    const stats = [
      { label: '总请求数', val: String(g.total_requests || 0) },
      { label: '总 Token', val: (g.total_tokens || 0).toLocaleString() },
      { label: 'Prompt Token', val: (g.total_prompt_tokens || 0).toLocaleString() },
      { label: 'Completion Token', val: (g.total_completion_tokens || 0).toLocaleString() },
      { label: '平均延迟', val: `${(g.avg_latency || 0).toFixed(2)}s` },
    ];

    stats.forEach((stat) => {
      const statEl = document.createElement('div');
      statEl.className = 'admin-stat';

      const labelSpan = document.createElement('span');
      labelSpan.className = 'label';
      labelSpan.textContent = stat.label;
      statEl.appendChild(labelSpan);

      const valSpan = document.createElement('span');
      valSpan.className = 'value';
      valSpan.textContent = stat.val;
      statEl.appendChild(valSpan);

      el.appendChild(statEl);
    });

    const byAgent = data?.by_agent || {};
    if (Object.keys(byAgent).length > 0) {
      const header = document.createElement('div');
      header.style.marginTop = '12px';
      header.style.fontSize = '13px';
      header.style.fontWeight = '600';
      header.textContent = '按 Agent 分布';
      el.appendChild(header);

      Object.entries(byAgent).forEach(([name, info]) => {
        const statEl = document.createElement('div');
        statEl.className = 'admin-stat';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'label';
        labelSpan.textContent = name;
        statEl.appendChild(labelSpan);

        const valSpan = document.createElement('span');
        valSpan.className = 'value';
        valSpan.textContent = `${(info.total_tokens || 0).toLocaleString()} (${info.requests || 0}次)`;
        statEl.appendChild(valSpan);

        el.appendChild(statEl);
      });
    }
  } catch (_e) {
    // 忽略加载异常
  }
}
