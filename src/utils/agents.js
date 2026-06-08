/**
 * Agent 和协作模式常量
 */

/** Agent 图标映射 */
export const AGENT_ICONS = {
  '产品专家': '🧴',
  '技术支持专家': '🔧',
  '账单专家': '💰',
  '投诉处理专家': '⚠️',
  '通用咨询专家': '📋',
  'response_agent': '📨',
  'ReAct推理专家': '🧠',
};

/** 协作模式标签 */
export const MODE_LABELS = {
  sequential: '快速通道',
  parallel: '并行处理',
  consultation: '专家会诊',
  hierarchical: '层级协作',
  react: 'ReAct 推理',
};

/** 获取 Agent 图标（带默认值） */
export function getAgentIcon(agent) {
  return AGENT_ICONS[agent] || '🤖';
}

/** 获取模式标签（带默认值） */
export function getModeLabel(mode) {
  return MODE_LABELS[mode] || mode || '快速通道';
}
