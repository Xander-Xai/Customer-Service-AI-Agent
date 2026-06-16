import { createElement } from './dom.js';

/**
 * 渲染 Agent 负载分布图表
 * @param {Object} data 监控指标数据
 * @param {string} containerId 容器 ID 默认 'agentChart'
 */
export function renderAgentChart(data, containerId = 'agentChart') {
  if (!data?.metrics) return;
  const counts = data.metrics.agent_call_counts || {};
  const container = document.getElementById(containerId);
  if (!container) return;
  const colors = ['primary', 'success', 'warning', 'info', 'error'];
  const entries = Object.entries(counts);
  if (entries.length === 0) {
    container.replaceChildren(
      createElement(
        'div',
        { style: 'text-align:center;padding:40px;color:var(--text-muted);width:100%' },
        ['暂无数据'],
      ),
    );
    return;
  }
  const maxVal = Math.max(...entries.map((e) => e[1]), 1);
  const items = entries.map(([name, count], i) => {
    return createElement('div', { className: 'bar-item' }, [
      createElement('div', { className: 'bar-value' }, [count]),
      createElement('div', {
        className: `bar-fill ${colors[i % colors.length]}`,
        style: `height:${(count / maxVal) * 100}%`,
      }),
      createElement('div', { className: 'bar-label' }, [name]),
    ]);
  });
  container.replaceChildren(...items);
}

/**
 * 渲染智能体协作模式分布图表
 * @param {Object} data 监控指标数据
 * @param {string} containerId 容器 ID 默认 'modeChart'
 */
export function renderModeChart(data, containerId = 'modeChart') {
  if (!data?.metrics) return;
  const counts = data.metrics.mode_counts || {};
  const container = document.getElementById(containerId);
  if (!container) return;
  const modeColors = {
    sequential: 'info',
    parallel: 'success',
    consultation: 'warning',
    hierarchical: 'error',
    react: 'primary',
  };
  const modeLabels = {
    sequential: '快速通道',
    parallel: '并行处理',
    consultation: '专家会诊',
    hierarchical: '层级协作',
    react: 'ReAct 推理',
  };
  const entries = Object.entries(counts);
  if (entries.length === 0) {
    container.replaceChildren(
      createElement(
        'div',
        { style: 'text-align:center;padding:40px;color:var(--text-muted);width:100%' },
        ['暂无数据'],
      ),
    );
    return;
  }
  const maxVal = Math.max(...entries.map((e) => e[1]), 1);
  const items = entries.map(([name, count]) => {
    return createElement('div', { className: 'bar-item' }, [
      createElement('div', { className: 'bar-value' }, [count]),
      createElement('div', {
        className: `bar-fill ${modeColors[name] || 'primary'}`,
        style: `height:${(count / maxVal) * 100}%`,
      }),
      createElement('div', { className: 'bar-label' }, [modeLabels[name] || name]),
    ]);
  });
  container.replaceChildren(...items);
}
