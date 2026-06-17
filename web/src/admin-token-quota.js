/**
 * Token Quota 监控模块（v5.3+ 后端）
 * - 当前用户的 Token 消耗限额（每日/每月）
 * - 进度条 + 阈值告警
 */
import { getTokenQuota } from './api/rest.js';
import { showToast } from './utils/toast.js';

/** 加载并渲染 Token Quota 状态 */
export async function loadTokenQuota() {
  const el = document.getElementById('tokenQuotaStats');
  if (!el) return;
  try {
    const data = await getTokenQuota();
    if (!data) return;

    el.replaceChildren();

    const dailyPct = computePct(data.daily_used, data.daily_limit);
    const monthlyPct = computePct(data.monthly_used, data.monthly_limit);

    const items = [
      {
        label: '今日已用',
        val: formatTokens(data.daily_used),
        sub: `限额 ${formatTokens(data.daily_limit)}`,
      },
      {
        label: '本月已用',
        val: formatTokens(data.monthly_used),
        sub: `限额 ${formatTokens(data.monthly_limit)}`,
      },
      { label: '今日使用率', val: `${dailyPct.toFixed(1)}%`, color: pctColor(dailyPct) },
      { label: '本月使用率', val: `${monthlyPct.toFixed(1)}%`, color: pctColor(monthlyPct) },
    ];

    items.forEach((item) => {
      const statEl = document.createElement('div');
      statEl.className = 'admin-stat';
      const labelSpan = document.createElement('span');
      labelSpan.className = 'label';
      labelSpan.textContent = item.label;
      const valSpan = document.createElement('span');
      valSpan.className = 'value';
      valSpan.textContent = item.val;
      if (item.color) valSpan.style.color = item.color;
      statEl.appendChild(labelSpan);
      statEl.appendChild(valSpan);
      if (item.sub) {
        const subSpan = document.createElement('span');
        subSpan.className = 'admin-stat-sub';
        subSpan.style.fontSize = '11px';
        subSpan.style.color = 'var(--text-muted)';
        subSpan.textContent = item.sub;
        statEl.appendChild(subSpan);
      }
      el.appendChild(statEl);
    });
  } catch (e) {
    if (e?.message?.includes('401')) {
      showToast('请登录以查看 Token Quota', 'warning');
    }
  }
}

function computePct(used, limit) {
  if (!limit || limit <= 0) return 0;
  return Math.min(100, (used / limit) * 100);
}

function pctColor(pct) {
  if (pct >= 90) return '#f87171';
  if (pct >= 70) return '#fbbf24';
  return '#4ade80';
}

function formatTokens(n) {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}
