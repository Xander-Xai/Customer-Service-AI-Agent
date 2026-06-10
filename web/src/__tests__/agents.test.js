import { describe, expect, it } from 'vitest';
import { AGENT_ICONS, getAgentDisplayName, getAgentIcon, getModeLabel } from '../utils/agents.js';

describe('agents', () => {
  it('getAgentIcon 返回正确图标', () => {
    expect(getAgentIcon('产品专家')).toBe('🧴');
    expect(getAgentIcon('未知Agent')).toBe('💬'); // 默认
  });

  it('getAgentDisplayName 返回业务名称', () => {
    expect(getAgentDisplayName('产品专家')).toBe('产品顾问');
    expect(getAgentDisplayName('ReAct推理专家')).toBe('客服助手');
    expect(getAgentDisplayName('未知')).toBe('客服助手');
  });

  it('getModeLabel 返回空字符串（客户端不展示模式）', () => {
    expect(getModeLabel('sequential')).toBe('');
    expect(getModeLabel('react')).toBe('');
  });

  it('所有 Agent 都有图标映射', () => {
    const agents = [
      '产品专家',
      '技术支持专家',
      '账单专家',
      '投诉处理专家',
      '通用咨询专家',
      'response_agent',
      'ReAct推理专家',
    ];
    agents.forEach((a) => {
      expect(AGENT_ICONS[a]).toBeDefined();
    });
  });
});
