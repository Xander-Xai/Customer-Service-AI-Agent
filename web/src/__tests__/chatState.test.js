import { beforeEach, describe, expect, it } from 'vitest';
import {
  addMessage,
  getMessageHistory,
  getSelectedFile,
  getSessionId,
  getSessionToken,
  isWaiting,
  resetSession,
  resetWaiting,
  setSelectedFile,
  setSession,
  setWaiting,
  updateSession,
} from '../state/chatState.js';

// Mock localStorage
const store = {};
globalThis.localStorage = {
  getItem: (k) => store[k] || null,
  setItem: (k, v) => {
    store[k] = String(v);
  },
  removeItem: (k) => {
    delete store[k];
  },
  clear: () => {
    Object.keys(store).forEach((k) => {
      delete store[k];
    });
  },
};

// Mock events
vi.mock('../api/events.js', () => ({ emit: vi.fn(), on: vi.fn(), off: vi.fn() }));

describe('chatState', () => {
  beforeEach(() => {
    localStorage.clear();
    resetSession();
  });

  it('初始状态应为空', () => {
    expect(getSessionId()).toBeNull();
    expect(getSessionToken()).toBeNull();
    expect(getMessageHistory()).toEqual([]);
    expect(isWaiting()).toBe(false);
    expect(getSelectedFile()).toBeNull();
  });

  it('updateSession 更新会话信息', () => {
    updateSession('s1', 'token1');
    expect(getSessionId()).toBe('s1');
    expect(getSessionToken()).toBe('token1');
    expect(localStorage.getItem('currentSessionId')).toBe('s1');
  });

  it('addMessage 追加消息', () => {
    addMessage({ role: 'user', content: '你好' });
    addMessage({ role: 'assistant', content: '您好' });
    expect(getMessageHistory()).toHaveLength(2);
  });

  it('resetSession 重置所有状态', () => {
    updateSession('s1', 't1');
    addMessage({ role: 'user', content: 'test' });
    setWaiting(true);
    setSelectedFile(new File([''], 'test.png'));
    resetSession();
    expect(getSessionId()).toBeNull();
    expect(getMessageHistory()).toEqual([]);
    expect(isWaiting()).toBe(false);
    expect(getSelectedFile()).toBeNull();
  });

  it('setSession 设置会话并清空历史', () => {
    addMessage({ role: 'user', content: 'old' });
    setSession('s2');
    expect(getSessionId()).toBe('s2');
    expect(getSessionToken()).toBeNull();
    expect(getMessageHistory()).toEqual([]);
  });

  it('setWaiting 控制等待状态', () => {
    setWaiting(true);
    expect(isWaiting()).toBe(true);
    setWaiting(false);
    expect(isWaiting()).toBe(false);
  });

  it('setSelectedFile 控制文件选择', () => {
    const file = new File(['content'], 'doc.pdf', { type: 'application/pdf' });
    setSelectedFile(file);
    expect(getSelectedFile()).toBe(file);
  });

  it('resetWaiting 重置等待和文件', () => {
    setWaiting(true);
    setSelectedFile(new File([''], 'x.png'));
    resetWaiting();
    expect(isWaiting()).toBe(false);
    expect(getSelectedFile()).toBeNull();
  });
});
