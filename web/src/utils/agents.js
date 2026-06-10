/**
 * Agent 和协作模式常量
 * 客户端界面使用业务语言，隐藏内部技术术语
 */

/** Agent 图标映射（面向客户的品牌化图标） */
export const AGENT_ICONS = {
  产品专家: '🧴',
  技术支持专家: '💬',
  账单专家: '📋',
  投诉处理专家: '🤝',
  通用咨询专家: '💬',
  response_agent: '💬',
  ReAct推理专家: '💬',
};

/** Agent 显示名称（面向客户） */
export const AGENT_DISPLAY_NAMES = {
  产品专家: '产品顾问',
  技术支持专家: '使用指导',
  账单专家: '订单服务',
  投诉处理专家: '客诉专员',
  通用咨询专家: '客服助手',
  response_agent: '客服助手',
  ReAct推理专家: '客服助手',
};

/** 协作模式标签（面向客户隐藏技术细节） */
export const MODE_LABELS = {
  sequential: '',
  parallel: '',
  consultation: '',
  hierarchical: '',
  react: '',
};

/** 获取 Agent 图标（带默认值） */
export function getAgentIcon(agent) {
  return AGENT_ICONS[agent] || '💬';
}

/** 获取 Agent 显示名称（带默认值） */
export function getAgentDisplayName(agent) {
  return AGENT_DISPLAY_NAMES[agent] || '客服助手';
}

/** 获取模式标签（带默认值）——客户端不显示模式信息 */
export function getModeLabel(mode) {
  return MODE_LABELS[mode] || '';
}
