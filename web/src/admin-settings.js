import {
  activatePromptVersion,
  addKnowledgeDocs,
  createPromptVersion,
  getActivePrompt,
  getAlertConfig,
  getAlertHistory,
  getAuditLog,
  getFeedbackStats,
  getHealth,
  getKnowledgeStats,
  getMetrics,
  getPromptAgents,
  getPromptVersions,
  getTokenUsage,
  seedKnowledge,
  syncKnowledge,
  testAlert as testAlertAPI,
} from './api/rest.js';
import { showToast } from './utils/toast.js';

// ── 知识库 ──
export async function loadKnowledgeStats() {
  const data = await getKnowledgeStats();
  if (!data) return;
  const el = document.getElementById('knowledgeStats');
  if (!el) return;
  if (!data.available) {
    el.innerHTML = '';
    const statEl = document.createElement('div');
    statEl.className = 'admin-stat';
    const labelSpan = document.createElement('span');
    labelSpan.className = 'label';
    labelSpan.textContent = '状态';
    const valSpan = document.createElement('span');
    valSpan.className = 'value';
    valSpan.style.color = '#ef4444';
    valSpan.textContent = '不可用';
    statEl.appendChild(labelSpan);
    statEl.appendChild(valSpan);
    el.appendChild(statEl);
    return;
  }
  const cols = data.collections || {};

  el.innerHTML = '';

  const collections = [
    { label: '产品知识', val: `${cols.product_knowledge || 0} 条`, color: '' },
    { label: 'FAQ', val: `${cols.faq || 0} 条`, color: '' },
    { label: '技术支持', val: `${cols.tech_support || 0} 条`, color: '' },
    { label: '投诉知识', val: `${cols.complaint_knowledge || 0} 条`, color: '' },
    { label: '总计', val: `${data.total || 0} 条`, color: 'var(--primary)' },
  ];

  collections.forEach((col) => {
    const statEl = document.createElement('div');
    statEl.className = 'admin-stat';

    const labelSpan = document.createElement('span');
    labelSpan.className = 'label';
    labelSpan.textContent = col.label;
    statEl.appendChild(labelSpan);

    const valSpan = document.createElement('span');
    valSpan.className = 'value';
    valSpan.textContent = col.val;
    if (col.color) valSpan.style.color = col.color;
    statEl.appendChild(valSpan);

    el.appendChild(statEl);
  });
}

export async function reseedKnowledge() {
  if (!confirm('确定重新种子？这会覆盖现有数据。')) return;
  const data = await seedKnowledge();
  showToast(data?.message || '操作完成');
  loadKnowledgeStats();
}

export async function syncFromErp() {
  showToast('正在从 ERP 同步...', 'success');
  const data = await syncKnowledge();
  showToast(data?.message || '同步完成');
  loadKnowledgeStats();
}

export async function handleAddDocs() {
  const collection = document.getElementById('addDocCollection').value;
  const text = document.getElementById('addDocText').value.trim();
  if (!text) {
    showToast('请输入文档内容', 'error');
    return;
  }
  const documents = text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!documents.length) {
    showToast('文档内容不能为空', 'error');
    return;
  }
  try {
    const result = await addKnowledgeDocs(collection, documents);
    showToast(result?.message || '添加完成', 'success');
    document.getElementById('addDocText').value = '';
    loadKnowledgeStats();
  } catch (e) {
    showToast(`添加失败: ${e.message}`, 'error');
  }
}

// ── 告警配置 ──
export async function loadAlertConfig() {
  const data = await getAlertConfig();
  if (!data) return;
  const el = document.getElementById('alertConfig');
  if (!el) return;

  el.innerHTML = '';

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

export async function testAlert() {
  const data = await testAlertAPI('测试告警', '这是来自管理后台的测试告警', 'info');
  showToast(data?.message || '测试告警已发送');
}

// ── 审计日志 ──
export async function loadAuditLog() {
  const data = await getAuditLog(30);
  if (!data) return;
  const el = document.getElementById('auditTableBody');
  if (!el) return;

  el.innerHTML = '';

  data.logs.forEach((l) => {
    const tr = document.createElement('tr');

    const tdTime = document.createElement('td');
    tdTime.textContent = new Date(l.timestamp * 1000).toLocaleString('zh-CN');
    tr.appendChild(tdTime);

    const tdAction = document.createElement('td');
    tdAction.textContent = l.action;
    tr.appendChild(tdAction);

    const tdDetail = document.createElement('td');
    tdDetail.style.maxWidth = '300px';
    tdDetail.style.overflow = 'hidden';
    tdDetail.style.textOverflow = 'ellipsis';
    tdDetail.textContent = l.detail || '';
    tr.appendChild(tdDetail);

    const tdIp = document.createElement('td');
    tdIp.textContent = l.ip_address || '';
    tr.appendChild(tdIp);

    el.appendChild(tr);
  });
}

// ── 系统健康 ──
export async function loadSystemHealth() {
  try {
    const data = await getHealth();
    const el = document.getElementById('systemHealth');
    if (!el) return;
    const s = data.status || 'unknown';
    const color = s === 'healthy' ? '#4ade80' : s === 'degraded' ? '#fbbf24' : '#f87171';
    const redisInfo = data.components?.redis;
    const dbInfo = data.components?.database;

    el.innerHTML = '';

    const stats = [
      { label: '状态', val: s, color },
      { label: '版本', val: data.version || '-' },
      { label: '模式', val: data.mode || '-' },
      { label: 'Redis', val: redisInfo?.connected ? `✅ ${redisInfo.latency_ms || ''}ms` : '❌' },
      { label: '数据库', val: dbInfo?.connected ? `✅ ${dbInfo.latency_ms || ''}ms` : '❌' },
      { label: 'ChromaDB', val: data.components?.chromadb?.connected ? '✅' : '❌' },
      { label: '熔断器', val: data.components?.circuit_breaker?.state || '-' },
      {
        label: 'LLM',
        val: data.components?.llm?.configured
          ? `✅ ${data.components.llm.provider || ''}`
          : '❌ 未配置',
      },
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
      if (stat.color) valSpan.style.color = stat.color;
      statEl.appendChild(valSpan);

      el.appendChild(statEl);
    });
  } catch (_e) {
    // 忽略加载异常
  }
}

// ── 告警历史 ──
export async function loadAlertHistory() {
  try {
    const data = await getAlertHistory(20);
    const el = document.getElementById('alertHistoryList');
    if (!el) return;

    el.innerHTML = '';

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

// ── Token 用量 ──
export async function loadTokenUsage() {
  try {
    const data = await getTokenUsage();
    const el = document.getElementById('tokenUsageStats');
    if (!el) return;
    if (data?.error) {
      el.innerHTML = '';
      const errEl = document.createElement('div');
      errEl.style.color = 'var(--text-muted)';
      errEl.style.textAlign = 'center';
      errEl.style.padding = '20px';
      errEl.textContent = data.error;
      el.appendChild(errEl);
      return;
    }
    const g = data?.global || {};

    el.innerHTML = '';

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

// ── Prompt 管理 ──
export async function loadPromptAgents() {
  try {
    const data = await getPromptAgents();
    const select = document.getElementById('promptAgentSelect');
    if (!select || !data?.agents) return;

    select.innerHTML = '';

    if (!data.agents.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '暂无 Agent';
      select.appendChild(option);
      return;
    }

    data.agents.forEach((a) => {
      const option = document.createElement('option');
      option.value = a;
      option.textContent = a;
      select.appendChild(option);
    });

    loadPromptVersions();
  } catch (_e) {
    // 忽略加载异常
  }
}

export async function loadPromptVersions() {
  const agentName = document.getElementById('promptAgentSelect')?.value;
  if (!agentName) return;
  const el = document.getElementById('promptVersionsList');
  if (!el) return;
  try {
    const [versionsData, activeData] = await Promise.all([
      getPromptVersions(agentName).catch(() => null),
      getActivePrompt(agentName).catch(() => null),
    ]);
    const versions = versionsData?.versions || [];
    const activeVersion = activeData?.version || '';

    el.innerHTML = '';

    if (!versions.length) {
      const emptyEl = document.createElement('div');
      emptyEl.style.textAlign = 'center';
      emptyEl.style.padding = '20px';
      emptyEl.style.color = 'var(--text-muted)';
      emptyEl.textContent = '暂无版本';
      el.appendChild(emptyEl);
      return;
    }

    versions.forEach((v) => {
      const isActive = v.version === activeVersion || v.is_active;

      const verEl = document.createElement('div');
      verEl.style.display = 'flex';
      verEl.style.justifyContent = 'space-between';
      verEl.style.alignItems = 'center';
      verEl.style.padding = '8px';
      verEl.style.border = '1px solid var(--border)';
      verEl.style.borderRadius = '6px';
      verEl.style.marginBottom = '6px';
      if (isActive) {
        verEl.style.borderColor = 'var(--primary)';
        verEl.style.background = 'var(--bg-elevated)';
      }

      const infoContainer = document.createElement('div');

      const titleSpan = document.createElement('span');
      titleSpan.style.fontWeight = '600';
      titleSpan.style.fontSize = '13px';
      titleSpan.textContent = v.version;
      infoContainer.appendChild(titleSpan);

      if (isActive) {
        const activeTag = document.createElement('span');
        activeTag.className = 'status-tag healthy';
        activeTag.style.marginLeft = '8px';
        activeTag.textContent = '活跃';
        infoContainer.appendChild(activeTag);
      }

      const timeEl = document.createElement('div');
      timeEl.style.fontSize = '11px';
      timeEl.style.color = 'var(--text-muted)';
      timeEl.style.marginTop = '2px';
      timeEl.textContent = `创建: ${v.created_at ? new Date(v.created_at * 1000).toLocaleString('zh-CN') : '-'}`;
      infoContainer.appendChild(timeEl);
      verEl.appendChild(infoContainer);

      if (!isActive) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-sm btn-activate-prompt';
        button.dataset.version = v.version;
        button.style.padding = '2px 8px';
        button.style.fontSize = '11px';
        button.textContent = '激活';
        button.addEventListener('click', async () => {
          try {
            await activatePromptVersion(agentName, v.version);
            showToast('版本已激活', 'success');
            loadPromptVersions();
          } catch (e) {
            showToast(`激活失败: ${e.message}`, 'error');
          }
        });
        verEl.appendChild(button);
      }

      el.appendChild(verEl);
    });
  } catch (_e) {
    // 忽略加载异常
  }
}

export async function handleCreatePrompt() {
  const agentName = document.getElementById('promptAgentSelect')?.value;
  if (!agentName) {
    showToast('请先选择 Agent', 'error');
    return;
  }
  const version = document.getElementById('newPromptVersion').value.trim();
  const promptText = document.getElementById('newPromptText').value.trim();
  const activate = document.getElementById('newPromptActivate').checked;
  if (!version) {
    showToast('请输入版本号', 'error');
    return;
  }
  if (!promptText || promptText.length < 10) {
    showToast('Prompt 内容至少 10 个字符', 'error');
    return;
  }
  try {
    const result = await createPromptVersion(agentName, promptText, version, activate);
    showToast(result?.status === 'ok' ? '版本创建成功' : '操作完成', 'success');
    document.getElementById('newPromptVersion').value = '';
    document.getElementById('newPromptText').value = '';
    loadPromptVersions();
  } catch (e) {
    showToast(`创建失败: ${e.message}`, 'error');
  }
}

// ── 运行指标 ──
export async function loadMetricsStats() {
  try {
    const resp = await getMetrics();
    if (!resp) return;
    const m = resp.metrics || {};
    const el = document.getElementById('metricsStats');
    if (!el) return;

    el.innerHTML = '';

    const stats = [
      { label: '总请求', val: String(m.total_requests || 0) },
      { label: '错误数', val: String(m.total_errors || 0) },
      { label: '平均响应', val: `${(m.avg_response_time || 0).toFixed(2)}s` },
      { label: '缓存命中率', val: `${(m.cache_hit_rate || 0).toFixed(1)}%` },
      { label: 'SLA 违约率', val: `${(m.sla?.violation_rate || 0).toFixed(1)}%` },
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
  } catch (_e) {
    // 忽略加载异常
  }
}

// ── 反馈统计 ──
export async function loadFeedbackStats() {
  try {
    const resp = await getFeedbackStats();
    if (!resp) return;
    const el = document.getElementById('feedbackStats');
    if (!el) return;

    el.innerHTML = '';

    const stats = [
      { label: '总反馈', val: String(resp.total || 0), color: '' },
      { label: '👍 好评', val: String(resp.positive || 0), color: '#4ade80' },
      { label: '👎 差评', val: String(resp.negative || 0), color: '#f87171' },
      { label: '好评率', val: `${((resp.rate || 0) * 100).toFixed(1)}%`, color: '' },
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
      if (stat.color) valSpan.style.color = stat.color;
      statEl.appendChild(valSpan);

      el.appendChild(statEl);
    });
  } catch (_e) {
    // 忽略加载异常
  }
}
