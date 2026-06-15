/**
 * 告警管理模块
 * 从 admin-settings.js 提取
 */
import { getAlertConfig, getAlertHistory, testAlert as testAlertAPI } from './api/rest.js';
import { showToast } from './utils/toast.js';

/** 加载告警配置 */
export async function loadAlertConfig() {
  const data = await getAlertConfig();
  if (!data) return;
  const el = document.getElementById('alertConfig');
  if (!el) return;

  el.replaceChildren();

  const items = [
    { label: 'Webhook 数量', val: `${data.webhooks.length} 个` },
    { label: '邮件通知', val: data.email_enabled ? '✅ 已配置' : '❌ 未配置' },
  ];

  if (data.email_to?.length) {
    items.push({ label: '通知邮箱', val: data.email_to.join(', ') });
  }

  items.forEach((item) => {
    const statEl = document.createElement('div');
    statEl.className = 'admin-stat';

    const labelSpan = document.createElement('span');
    labelSpan.className = 'label';
    labelSpan.textContent = item.label;
    statEl.appendChild(labelSpan);

    const valSpan = document.createElement('span');
    valSpan.className = 'value';
    valSpan.textContent = item.val;
    statEl.appendChild(valSpan);

    el.appendChild(statEl);
  });
}

/** 发送测试告警 */
export async function testAlert() {
  const data = await testAlertAPI('测试告警', '这是来自管理后台的测试告警', 'info');
  showToast(data?.message || '测试告警已发送');
}

/** 加载告警历史 */
export async function loadAlertHistory() {
  try {
    const data = await getAlertHistory(20);
    const el = document.getElementById('alertHistoryList');
    if (!el) return;

    el.replaceChildren();

    const alerts = data?.alerts || [];
    if (!alerts.length) {
      const emptyEl = document.createElement('div');
      emptyEl.style.textAlign = 'center';
      emptyEl.style.padding = '20px';
      emptyEl.style.color = 'var(--text-muted)';
      emptyEl.textContent = '暂无告警历史';
      el.appendChild(emptyEl);
      return;
    }

    alerts.forEach((a) => {
      const alertEl = document.createElement('div');
      alertEl.style.display = 'flex';
      alertEl.style.gap = '8px';
      alertEl.style.alignItems = 'flex-start';
      alertEl.style.padding = '8px 0';
      alertEl.style.borderBottom = '1px solid var(--border)';

      const dotEl = document.createElement('span');
      dotEl.style.fontSize = '16px';
      dotEl.textContent = a.severity === 'critical' ? '🔴' : a.severity === 'warning' ? '🟡' : '🔵';
      alertEl.appendChild(dotEl);

      const contentEl = document.createElement('div');
      contentEl.style.flex = '1';

      const titleEl = document.createElement('div');
      titleEl.style.fontSize = '13px';
      titleEl.style.fontWeight = '500';
      titleEl.textContent = a.title || a.message || '';
      contentEl.appendChild(titleEl);

      const descEl = document.createElement('div');
      descEl.style.fontSize = '12px';
      descEl.style.color = 'var(--text-muted)';
      descEl.style.marginTop = '2px';
      descEl.textContent = a.content || '';
      contentEl.appendChild(descEl);

      const timeEl = document.createElement('div');
      timeEl.style.fontSize = '11px';
      timeEl.style.color = 'var(--text-muted)';
      timeEl.style.marginTop = '2px';
      timeEl.textContent = a.timestamp ? new Date(a.timestamp * 1000).toLocaleString('zh-CN') : '';
      contentEl.appendChild(timeEl);

      alertEl.appendChild(contentEl);
      el.appendChild(alertEl);
    });
  } catch (_e) {
    // 忽略加载异常
  }
}
