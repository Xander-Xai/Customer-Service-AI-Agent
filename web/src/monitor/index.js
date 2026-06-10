/**
 * 监控仪表盘模块
 */
import { API } from '../api/index.js';
import { escapeHtml } from '../utils/dom.js';
import { formatTime } from '../utils/format.js';
import { renderAgentChart, renderModeChart } from '../utils/monitor-render.js';

let monitorRefreshTimer = null;

/** 切换页面（对话/监控） */
export function switchPage(page) {
  document.querySelectorAll('.nav-link').forEach((el) => {
    el.classList.remove('active');
  });
  const target = document.querySelector(`[data-page="${page}"]`);
  if (target) target.classList.add('active');

  const chatPage = document.getElementById('chatPage');
  const monitorPage = document.getElementById('monitorPage');

  if (page === 'chat') {
    if (chatPage) chatPage.style.display = 'flex';
    if (monitorPage) monitorPage.style.display = 'none';
    if (monitorRefreshTimer) {
      clearInterval(monitorRefreshTimer);
      monitorRefreshTimer = null;
    }
  } else {
    if (chatPage) chatPage.style.display = 'none';
    if (monitorPage) monitorPage.style.display = 'block';
    refreshMonitorData();
    monitorRefreshTimer = setInterval(refreshMonitorData, 10000);
  }
}

/** 初始化监控页面事件 */
export function initMonitor() {
  const btnRefreshMonitor = document.getElementById('btnRefreshMonitor');
  if (btnRefreshMonitor) btnRefreshMonitor.addEventListener('click', refreshMonitorData);
}

/** 刷新监控数据 */
async function refreshMonitorData() {
  try {
    const [metricsRes, kpiRes, sessionsRes, alertsRes, cacheRes] = await Promise.all([
      API.getMetrics().catch(() => null),
      API.getKPI().catch(() => null),
      API.getSessions().catch(() => ({ sessions: [] })),
      API.getAlerts().catch(() => ({ alerts: [] })),
      API.getCacheStats().catch(() => null),
    ]);

    renderMetricCards(metricsRes);
    renderAgentChart(metricsRes);
    renderModeChart(metricsRes);
    renderSLA(metricsRes);
    renderCacheStats(cacheRes);
    renderKPI(kpiRes);
    renderSessionsTable(sessionsRes.sessions || []);
    renderAlerts(alertsRes.alerts || []);
  } catch (_e) {}
}

// ===== 监控渲染函数 =====

function renderMetricCards(data) {
  if (!data?.metrics) return;
  const m = data.metrics;
  const container = document.getElementById('metricCards');
  if (!container) return;
  container.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">📨 总请求</div>
      <div class="metric-value">${m.total_requests || 0}</div>
      <div class="metric-detail">错误率: ${m.error_rate || 0}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">⚡ 平均响应</div>
      <div class="metric-value ${m.avg_response_time > 20 ? 'bad' : m.avg_response_time > 10 ? 'warn' : 'good'}">
        ${m.avg_response_time || 0}s
      </div>
      <div class="metric-detail">P95: ${m.p95_response_time || 0}s</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">🚨 SLA 违约</div>
      <div class="metric-value ${(m.sla?.violations_slow || 0) > 0 ? 'bad' : 'good'}">
        ${m.sla?.violations_slow || 0}
      </div>
      <div class="metric-detail">违约率: ${m.sla?.violation_rate || 0}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">📊 活跃会话</div>
      <div class="metric-value">${Object.keys(m.agent_call_counts || {}).length > 0 ? '活跃' : '空闲'}</div>
      <div class="metric-detail">缓存命中率: ${m.cache_hit_rate || 0}%</div>
    </div>
  `;
}

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

function renderSessionsTable(sessions) {
  const tbody = document.getElementById('sessionsTableBody');
  if (!tbody) return;
  if (!sessions.length) {
    tbody.innerHTML =
      '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px">暂无会话</td></tr>';
    return;
  }
  tbody.innerHTML = sessions
    .slice(0, 10)
    .map((s) => {
      const driftWarn = s.drift_escalation;
      return `
      <tr>
        <td style="font-family:monospace;font-size:12px">${escapeHtml((s.session_id || '').slice(0, 12))}...</td>
        <td>${s.message_count || 0}</td>
        <td>${s.drift_count || 0}</td>
        <td>${s.last_activity ? formatTime(s.last_activity) : '--'}</td>
        <td>${driftWarn ? '<span class="status-tag warning">漂移告警</span>' : '<span class="status-tag healthy">正常</span>'}</td>
      </tr>
    `;
    })
    .join('');
}

function renderAlerts(alerts) {
  const container = document.getElementById('alertsList');
  if (!container) return;
  if (!alerts.length) {
    container.innerHTML =
      '<div style="text-align:center;padding:20px;color:var(--text-muted)">✅ 暂无告警</div>';
    return;
  }
  container.innerHTML = alerts
    .map(
      (a) => `
    <div class="alert-item">
      <div class="alert-icon ${a.severity || 'warning'}">
        ${a.severity === 'critical' ? '🔴' : '🟡'}
      </div>
      <div class="alert-text">
        <div class="alert-text-title">${escapeHtml(a.message || '')}</div>
        <div class="alert-text-time">${a.timestamp ? formatTime(a.timestamp) : ''} | 窗口违约率: ${a.window_rate || 0}%</div>
      </div>
    </div>
  `,
    )
    .join('');
}
