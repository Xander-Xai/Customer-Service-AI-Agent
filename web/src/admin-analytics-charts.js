import { getHotQuestions, getQualityTrends, getSatisfaction } from './api/rest.js';

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
