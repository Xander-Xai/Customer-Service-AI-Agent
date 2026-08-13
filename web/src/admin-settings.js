/**
 * 管理后台设置模块（核心）
 * 已提取子模块：admin-knowledge.js, admin-alerts.js, admin-tokens.js
 */
import {
  activatePromptVersion,
  createPromptVersion,
  getActivePrompt,
  getAuditLog,
  getCircuitBreaker,
  getFeedbackStats,
  getHealth,
  getMetrics,
  getPromptAgents,
  getPrometheusMetrics,
  getPromptVersions,
} from './api/rest.js';
import { showToast } from './utils/toast.js';

// ── 审计日志 ──
export async function loadAuditLog() {
  const data = await getAuditLog(30);
  if (!data) return;
  const el = document.getElementById('auditTableBody');
  if (!el) return;

  el.replaceChildren();

  data.logs.forEach((l) => {
    const tr = document.createElement('tr');

    const tdTime = document.createElement('td');
    tdTime.textContent = l.timestamp ? new Date(l.timestamp).toLocaleString('zh-CN') : '';
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
    const [data, circuitData, prometheusText] = await Promise.all([
      getHealth(),
      getCircuitBreaker().catch(() => null),
      getPrometheusMetrics().catch(() => ''),
    ]);
    const el = document.getElementById('systemHealth');
    if (!el) return;
    const s = data.status || 'unknown';
    const color = s === 'healthy' ? '#4ade80' : s === 'degraded' ? '#fbbf24' : '#f87171';
    const redisInfo = data.components?.redis;
    const dbInfo = data.components?.database;
    const circuitEl = document.getElementById('circuitBreakerDetails');
    const prometheusEl = document.getElementById('prometheusPreview');

    el.replaceChildren();

    const stats = [
      { label: '状态', val: s, color },
      { label: '版本', val: data.version || '-' },
      { label: '模式', val: data.mode || '-' },
      { label: 'Redis', val: redisInfo?.connected ? `✅ ${redisInfo.latency_ms || ''}ms` : '❌' },
      { label: '数据库', val: dbInfo?.connected ? `✅ ${dbInfo.latency_ms || ''}ms` : '❌' },
      { label: 'Qdrant', val: data.components?.qdrant?.connected ? '✅' : '❌' },
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

    if (circuitEl) {
      circuitEl.replaceChildren();
      const circuitItems = [
        {
          label: '当前状态',
          value: circuitData?.state || data.components?.circuit_breaker?.state || 'unknown',
        },
        {
          label: '连续失败次数',
          value: String(
            circuitData?.consecutive_failures ??
              data.components?.circuit_breaker?.consecutive_failures ??
              0,
          ),
        },
        {
          label: '恢复时间',
          value: circuitData?.recovery_time ? `${circuitData.recovery_time}s` : '-',
        },
      ];

      circuitItems.forEach((item) => {
        const statEl = document.createElement('div');
        statEl.className = 'admin-stat';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'label';
        labelSpan.textContent = item.label;
        statEl.appendChild(labelSpan);

        const valSpan = document.createElement('span');
        valSpan.className = 'value';
        valSpan.textContent = item.value;
        statEl.appendChild(valSpan);

        circuitEl.appendChild(statEl);
      });
    }

    if (prometheusEl) {
      const preview = String(prometheusText || '')
        .trim()
        .split('\n')
        .slice(0, 16)
        .join('\n');
      prometheusEl.textContent = preview || '# 暂无 Prometheus 指标输出';
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

    select.replaceChildren();

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

    el.replaceChildren();

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
      timeEl.textContent = `创建: ${v.created_at ? new Date(v.created_at).toLocaleString('zh-CN') : '-'}`;
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

    el.replaceChildren();

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

    el.replaceChildren();

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

// ── 语音设置 ──

const VOICE_STORAGE_KEYS = {
  language: 'csai_voice_language',
  autoSend: 'csai_voice_auto_send',
  format: 'csai_audio_format',
};

/** 从 localStorage 加载语音设置并应用到页面控件 */
export function loadVoiceSettings() {
  const languageEl = document.getElementById('voiceLanguage');
  const autoSendEl = document.getElementById('voiceAutoSend');
  const formatEl = document.getElementById('audioFormat');

  if (languageEl) {
    languageEl.value = localStorage.getItem(VOICE_STORAGE_KEYS.language) || 'zh-CN';
  }
  if (autoSendEl) {
    autoSendEl.checked = localStorage.getItem(VOICE_STORAGE_KEYS.autoSend) === 'true';
  }
  if (formatEl) {
    formatEl.value = localStorage.getItem(VOICE_STORAGE_KEYS.format) || 'webm';
  }
}

/** 将当前页面控件值保存到 localStorage */
export function saveVoiceSettings() {
  const languageEl = document.getElementById('voiceLanguage');
  const autoSendEl = document.getElementById('voiceAutoSend');
  const formatEl = document.getElementById('audioFormat');

  if (languageEl) {
    localStorage.setItem(VOICE_STORAGE_KEYS.language, languageEl.value);
  }
  if (autoSendEl) {
    localStorage.setItem(VOICE_STORAGE_KEYS.autoSend, String(autoSendEl.checked));
  }
  if (formatEl) {
    localStorage.setItem(VOICE_STORAGE_KEYS.format, formatEl.value);
  }
}

/** 初始化语音设置：加载值 + 绑定变更事件 */
export function initVoiceSettings() {
  loadVoiceSettings();

  ['voiceLanguage', 'voiceAutoSend', 'audioFormat'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('change', saveVoiceSettings);
    }
  });
}
