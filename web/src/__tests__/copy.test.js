import { describe, expect, it } from 'vitest';
import { CHAT, COMMON, PROGRESS, QUICK_PROMPTS, ROLE_LABELS, UPLOAD } from '../utils/copy.js';

describe('copy', () => {
  it('COMMON 包含必要文案', () => {
    expect(COMMON.appName).toBe('客服助手');
    expect(COMMON.loading).toBeTruthy();
    expect(COMMON.error).toBeTruthy();
    expect(COMMON.noPermission).toBeTruthy();
  });

  it('CHAT 包含聊天页文案', () => {
    expect(CHAT.welcomeTitle).toBe('您好，有什么可以帮您？');
    expect(CHAT.inputPlaceholder).toBeTruthy();
    expect(CHAT.agentDefault).toBe('客服助手');
  });

  it('QUICK_PROMPTS 是 4 个卡片', () => {
    expect(QUICK_PROMPTS).toHaveLength(4);
    QUICK_PROMPTS.forEach((p) => {
      expect(p.title).toBeTruthy();
      expect(p.icon).toBeTruthy();
      expect(p.desc).toBeTruthy();
    });
  });

  it('PROGRESS 有阶段消息', () => {
    expect(PROGRESS.stages.length).toBeGreaterThanOrEqual(3);
  });

  it('UPLOAD 有错误文案', () => {
    expect(UPLOAD.imageFailed).toBeTruthy();
    expect(UPLOAD.fileFailed).toBeTruthy();
    expect(UPLOAD.sizeExceeded(10, 15)).toContain('10');
    expect(UPLOAD.sizeExceeded(10, 15)).toContain('15');
  });

  it('ROLE_LABELS 覆盖 4 种角色', () => {
    expect(Object.keys(ROLE_LABELS)).toEqual(
      expect.arrayContaining(['customer', 'agent', 'supervisor', 'admin']),
    );
  });
});
