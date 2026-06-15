import {
  getAlerts,
  getCacheStats,
  getHotQuestions,
  getKPI,
  getMetrics,
  getQualityTrends,
  getSatisfaction,
  getSessions,
} from './api/rest.js';
import { formatTime } from './utils/format.js';
import { renderAgentChart, renderModeChart } from './utils/monitor-render.js';

/**
 * 刷新监控页面数据
 */
export async function refreshMonitorData() {
  try {
    const [metricsRes, kpiRes, sessionsRes, alertsRes, cacheRes] = await Promise.all([
      getMetrics().catch(() => null),
      getKPI().catch(() => null),
      getSessions().catch(() => ({ sessions: [] })),
      getAlerts().catch(() => ({ alerts: [] })),
      getCacheStats().catch(() => null),
    ]);

    renderMetricCards(metricsRes);
    renderAgentChart(metricsRes);
    renderModeChart(metricsRes);
    renderSLA(metricsRes);
    renderCacheStats(cacheRes);
    renderKPI(kpiRes);
    renderSessionsTable(sessionsRes?.sessions || []);
    renderAlerts(alertsRes?.alerts || []);
  } catch (_e) {
    // 静默忽略加载错误
  }
}

/**
 * 渲染顶部指标卡片
 */
function renderMetricCards(data) {
  if (!data?.metrics) return;
  const m = data.metrics;
  const container = document.getElementById('metricCards');
  if (!container) return;

  container.replaceChildren();

  const cards = [
    {
      label: '📨 总请求',
      value: m.total_requests || 0,
      detail: `错误率: ${m.error_rate || 0}%`,
      valueClass: '',
    },
    {
      label: '⚡ 平均响应',
      value: `${m.avg_response_time || 0}s`,
      detail: `P95: ${m.p95_response_time || 0}s`,
      valueClass: m.avg_response_time > 20 ? 'bad' : m.avg_response_time > 10 ? 'warn' : 'good',
    },
    {
      label: '🚨 SLA 违约',
      value: m.sla?.violations_slow || 0,
      detail: `违约率: ${m.sla?.violation_rate || 0}%`,
      valueClass: (m.sla?.violations_slow || 0) > 0 ? 'bad' : 'good',
    },
    {
      label: '📊 活跃会话',
      value: Object.keys(m.agent_call_counts || {}).length > 0 ? '活跃' : '空闲',
      detail: `缓存命中率: ${m.cache_hit_rate || 0}%`,
      valueClass: '',
    },
  ];

  cards.forEach((card) => {
    const cardEl = document.createElement('div');
    cardEl.className = 'metric-card admin-card';

    const labelEl = document.createElement('div');
    labelEl.className = 'metric-label';
    labelEl.textContent = card.label;
    cardEl.appendChild(labelEl);

    const valEl = document.createElement('div');
    valEl.className = `metric-value ${card.valueClass}`;
    valEl.textContent = card.value;
    cardEl.appendChild(valEl);

    const detEl = document.createElement('div');
    detEl.className = 'metric-detail';
    detEl.textContent = card.detail;
    cardEl.appendChild(detEl);

    container.appendChild(cardEl);
  });
}

/**
 * 渲染 SLA 环形图
 */
function renderSLA(data) {
  if (!data?.metrics?.sla) return;
  const sla = data.metrics.sla;
  const complianceRate = 100 - (sla.violation_rate || 0);
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('slaFill');
  if (fill) {
    fill.setAttribute(
      'stroke-dasharray',
      `${(complianceRate / 100) * circumference} ${circumference}`,
    );
    fill.setAttribute(
      'stroke',
      complianceRate >= 90
        ? 'var(--success)'
        : complianceRate >= 70
          ? 'var(--warning)'
          : 'var(--error)',
    );
  }
  const slaValue = document.getElementById('slaValue');
  if (slaValue) slaValue.textContent = `${complianceRate.toFixed(1)}%`;
  const statusEl = document.getElementById('slaStatus');
  if (statusEl) {
    statusEl.textContent = complianceRate >= 90 ? '达标' : complianceRate >= 70 ? '注意' : '告警';
    statusEl.className = `status-tag ${complianceRate >= 90 ? 'healthy' : complianceRate >= 70 ? 'warning' : 'critical'}`;
  }
  const detail = document.getElementById('slaDetail');
  if (detail)
    detail.textContent = `目标: ${sla.target_min}s - ${sla.target_max}s | 违约: ${sla.violations_slow}次`;
}

/**
 * 渲染缓存情况
 */
function renderCacheStats(data) {
  if (!data) return;
  const hitRate = parseFloat(data.hit_rate) || 0;
  const circumference = 2 * Math.PI * 42;
  const fill = document.getElementById('cacheFill');
  if (fill)
    fill.setAttribute('stroke-dasharray', `${(hitRate / 100) * circumference} ${circumference}`);
  const cacheValue = document.getElementById('cacheValue');
  if (cacheValue) cacheValue.textContent = `${hitRate}%`;
  const detail = document.getElementById('cacheDetail');
  if (detail)
    detail.textContent = `L1: ${data.l1_hits || 0}次 / L2: ${data.l2_hits || 0}次 | 大小: L1=${data.l1_size || 0} L2=${data.l2_size || 0}`;
}

/**
 * 渲染总体 KPI
 */
function renderKPI(data) {
  if (!data?.kpi) return;
  const kpi = data.kpi;
  const circumference = 2 * Math.PI * 42;

  const firstRate = parseFloat(kpi.first_resolution_rate) || 0;
  const resFill = document.getElementById('resolutionFill');
  if (resFill)
    resFill.setAttribute(
      'stroke-dasharray',
      `${(firstRate / 100) * circumference} ${circumference}`,
    );
  const resValue = document.getElementById('resolutionValue');
  if (resValue) resValue.textContent = kpi.first_resolution_rate || '--';
  const resDetail = document.getElementById('resolutionDetail');
  if (resDetail)
    resDetail.textContent = `单轮解决: ${kpi.total_single_turn_resolved || 0} | 多轮: ${kpi.total_multi_turn || 0}`;

  const aiRate = parseFloat(kpi.ai_handled_rate) || 0;
  const aiFill = document.getElementById('aiFill');
  if (aiFill)
    aiFill.setAttribute('stroke-dasharray', `${(aiRate / 100) * circumference} ${circumference}`);
  const aiValue = document.getElementById('aiValue');
  if (aiValue) aiValue.textContent = kpi.ai_handled_rate || '--';
  const aiDetail = document.getElementById('aiDetail');
  if (aiDetail)
    aiDetail.textContent = `AI 处理: ${kpi.total_ai_handled || 0} | 转人工: ${kpi.total_escalated || 0}`;
}

/**
 * 渲染最近会话列表
 */
function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessionsTableBody');
  if (!tbody) return;

  tbody.replaceChildren();

  if (!sessions.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.style.textAlign = 'center';
    td.style.color = 'var(--text-muted)';
    td.style.padding = '20px';
    td.textContent = '暂无会话';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  sessions.slice(0, 10).forEach((s) => {
    const tr = document.createElement('tr');

    const tdId = document.createElement('td');
    tdId.style.fontFamily = 'monospace';
    tdId.style.fontSize = '12px';
    tdId.textContent = `${(s.session_id || '').slice(0, 12)}...`;
    tr.appendChild(tdId);

    const tdMsg = document.createElement('td');
    tdMsg.textContent = String(s.message_count || 0);
    tr.appendChild(tdMsg);

    const tdDrift = document.createElement('td');
    tdDrift.textContent = String(s.drift_count || 0);
    tr.appendChild(tdDrift);

    const tdTime = document.createElement('td');
    tdTime.textContent = s.last_activity ? formatTime(s.last_activity) : '--';
    tr.appendChild(tdTime);

    const tdStatus = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = `status-tag ${s.drift_escalation ? 'warning' : 'healthy'}`;
    tag.textContent = s.drift_escalation ? '漂移告警' : '正常';
    tdStatus.appendChild(tag);
    tr.appendChild(tdStatus);

    tbody.appendChild(tr);
  });
}

/**
 * 渲染 SLA 告警列表
 */
function renderAlerts(alerts) {
  const container = document.getElementById('alertsList');
  if (!container) return;

  container.replaceChildren();

  if (!alerts.length) {
    const emptyEl = document.createElement('div');
    emptyEl.style.textAlign = 'center';
    emptyEl.style.padding = '20px';
    emptyEl.style.color = 'var(--text-muted)';
    emptyEl.textContent = '暂无告警';
    container.appendChild(emptyEl);
    return;
  }

  alerts.forEach((a) => {
    const alertEl = document.createElement('div');
    alertEl.className = 'alert-item';

    const iconEl = document.createElement('div');
    iconEl.className = `alert-icon ${a.severity || 'warning'}`;
    iconEl.textContent = a.severity === 'critical' ? '🔴' : '🟡';
    alertEl.appendChild(iconEl);

    const textEl = document.createElement('div');
    textEl.className = 'alert-text';

    const titleEl = document.createElement('div');
    titleEl.className = 'alert-text-title';
    titleEl.textContent = a.message || '';
    textEl.appendChild(titleEl);

    const timeEl = document.createElement('div');
    timeEl.className = 'alert-text-time';
    timeEl.textContent = `${a.timestamp ? formatTime(a.timestamp) : ''} | 窗口违约率: ${a.window_rate || 0}%`;
    textEl.appendChild(timeEl);

    alertEl.appendChild(textEl);
    container.appendChild(alertEl);
  });
}

/**
 * 渲染质量趋势图表
 */
function renderQualityTrends(data) {
  const el = document.getElementById('qualityTrendsChart');
  if (!el || !data?.trends?.length) return;

  el.replaceChildren();

  const maxQueries = Math.max(...data.trends.map((t) => t.total_queries), 1);

  data.trends.forEach((t) => {
    const pct = Math.max((t.total_queries / maxQueries) * 100, 5);
    const score = t.avg_score;
    const color = score > 80 ? '#4ade80' : score >= 60 ? '#fbbf24' : '#f87171';
    const dayLabel = t.date.slice(5);

    const itemEl = document.createElement('div');
    itemEl.style.flex = '1';
    itemEl.style.display = 'flex';
    itemEl.style.flexDirection = 'column';
    itemEl.style.alignItems = 'center';
    itemEl.style.gap = '4px';

    const scoreEl = document.createElement('span');
    scoreEl.style.fontSize = '11px';
    scoreEl.style.color = 'var(--text-secondary)';
    scoreEl.textContent = String(score);
    itemEl.appendChild(scoreEl);

    const barEl = document.createElement('div');
    barEl.style.width = '100%';
    barEl.style.height = `${pct}%`;
    barEl.style.minHeight = '4px';
    barEl.style.background = color;
    barEl.style.borderRadius = '4px 4px 0 0';
    barEl.style.transition = 'height .3s';
    itemEl.appendChild(barEl);

    const labelEl = document.createElement('span');
    labelEl.style.fontSize = '11px';
    labelEl.style.color = 'var(--text-muted)';
    labelEl.textContent = dayLabel;
    itemEl.appendChild(labelEl);

    el.appendChild(itemEl);
  });
}

/**
 * 加载质量趋势数据
 */
export async function loadQualityTrends() {
  try {
    const data = await getQualityTrends();
    if (data) renderQualityTrends(data);
  } catch (_e) {
    // 静默忽略
  }
}

/**
 * 渲染热门问题 TOP10
 */
function renderHotQuestions(data) {
  const el = document.getElementById('hotQuestionsList');
  if (!el || !data?.questions?.length) return;

  el.replaceChildren();

  const maxCount = Math.max(...data.questions.map((q) => q.count), 1);

  data.questions.forEach((q, i) => {
    const pct = (q.count / maxCount) * 100;

    const rowEl = document.createElement('div');
    rowEl.style.marginBottom = '10px';

    const textRow = document.createElement('div');
    textRow.style.display = 'flex';
    textRow.style.justifyContent = 'space-between';
    textRow.style.fontSize = '12px';
    textRow.style.marginBottom = '3px';

    const nameEl = document.createElement('span');
    nameEl.textContent = `${i + 1}. ${q.query}`;
    textRow.appendChild(nameEl);

    const countEl = document.createElement('span');
    countEl.style.color = 'var(--text-muted)';
    countEl.textContent = `${q.count}次 · ${q.category}`;
    textRow.appendChild(countEl);
    rowEl.appendChild(textRow);

    const trackEl = document.createElement('div');
    trackEl.style.height = '6px';
    trackEl.style.background = 'var(--bg-elevated)';
    trackEl.style.borderRadius = '3px';
    trackEl.style.overflow = 'hidden';

    const fillEl = document.createElement('div');
    fillEl.style.width = `${pct}%`;
    fillEl.style.height = '100%';
    fillEl.style.background = 'var(--primary)';
    fillEl.style.borderRadius = '3px';
    fillEl.style.transition = 'width .3s';

    trackEl.appendChild(fillEl);
    rowEl.appendChild(trackEl);
    el.appendChild(rowEl);
  });
}

/**
 * 加载热门问题
 */
export async function loadHotQuestions() {
  try {
    const data = await getHotQuestions();
    if (data) renderHotQuestions(data);
  } catch (_e) {
    // 静默忽略
  }
}

/**
 * 渲染客户满意度
 */
function renderSatisfaction(data) {
  const el = document.getElementById('satisfactionStats');
  if (!el || !data) return;

  el.replaceChildren();

  const rate = ((data.overall_rate || 0) * 100).toFixed(1);
  const rateColor =
    data.overall_rate >= 0.8 ? '#4ade80' : data.overall_rate >= 0.6 ? '#fbbf24' : '#f87171';

  const summaryEl = document.createElement('div');
  summaryEl.style.textAlign = 'center';
  summaryEl.style.marginBottom = '16px';

  const valEl = document.createElement('div');
  valEl.style.fontSize = '48px';
  valEl.style.fontWeight = '700';
  valEl.style.color = rateColor;
  valEl.textContent = `${rate}%`;
  summaryEl.appendChild(valEl);

  const descEl = document.createElement('div');
  descEl.style.fontSize = '13px';
  descEl.style.color = 'var(--text-muted)';
  descEl.textContent = `好评 ${data.positive || 0} / 总计 ${data.total || 0}`;
  summaryEl.appendChild(descEl);
  el.appendChild(summaryEl);

  if (data.by_category) {
    Object.entries(data.by_category).forEach(([cat, info]) => {
      const pct = ((info.rate || 0) * 100).toFixed(0);
      const barColor = info.rate >= 0.8 ? '#4ade80' : info.rate >= 0.6 ? '#fbbf24' : '#f87171';

      const catEl = document.createElement('div');
      catEl.style.marginBottom = '10px';

      const textRow = document.createElement('div');
      textRow.style.display = 'flex';
      textRow.style.justifyContent = 'space-between';
      textRow.style.fontSize = '12px';
      textRow.style.marginBottom = '3px';

      const labelEl = document.createElement('span');
      labelEl.textContent = cat;
      textRow.appendChild(labelEl);

      const countEl = document.createElement('span');
      countEl.style.color = 'var(--text-muted)';
      countEl.textContent = `${pct}% · ${info.count}次`;
      textRow.appendChild(countEl);
      catEl.appendChild(textRow);

      const trackEl = document.createElement('div');
      trackEl.style.height = '6px';
      trackEl.style.background = 'var(--bg-elevated)';
      trackEl.style.borderRadius = '3px';
      trackEl.style.overflow = 'hidden';

      const fillEl = document.createElement('div');
      fillEl.style.width = `${pct}%`;
      fillEl.style.height = '100%';
      fillEl.style.background = barColor;
      fillEl.style.borderRadius = '3px';
      fillEl.style.transition = 'width .3s';

      trackEl.appendChild(fillEl);
      catEl.appendChild(trackEl);
      el.appendChild(catEl);
    });
  }
}

/**
 * 加载客户满意度
 */
export async function loadSatisfaction() {
  try {
    const data = await getSatisfaction();
    if (data) renderSatisfaction(data);
  } catch (_e) {
    // 静默忽略
  }
}
