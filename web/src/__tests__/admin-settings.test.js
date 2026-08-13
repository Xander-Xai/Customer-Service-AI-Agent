/**
 * admin-settings: 语音设置面板功能测试
 */
import { describe, expect, it, beforeEach, vi } from 'vitest';

// 模拟 localStorage
const localStorageMock = (() => {
  let store = {};
  return {
    getItem: vi.fn((key) => store[key] ?? null),
    setItem: vi.fn((key, value) => { store[key] = String(value); }),
    removeItem: vi.fn((key) => { delete store[key]; }),
    clear: vi.fn(() => { store = {}; }),
  };
})();
Object.defineProperty(globalThis, 'localStorage', { value: localStorageMock });

describe('Voice Settings', () => {
  beforeEach(() => {
    localStorageMock.clear();
    vi.clearAllMocks();
    document.body.innerHTML = '';
  });

  function setupVoiceSettingsHTML() {
    document.body.innerHTML = `
      <select id="voiceLanguage" class="settings-select">
        <option value="zh-CN">中文</option>
        <option value="en-US">English</option>
        <option value="ja-JP">日本語</option>
      </select>
      <input type="checkbox" id="voiceAutoSend" role="switch" aria-checked="false">
      <select id="audioFormat" class="settings-select">
        <option value="webm">WebM</option>
        <option value="mp3">MP3</option>
        <option value="ogg">OGG</option>
      </select>
    `;
  }

  it('loadVoiceSettings 从 localStorage 加载默认值', async () => {
    const { loadVoiceSettings } = await import('../admin-settings.js');
    setupVoiceSettingsHTML();

    // 默认无 localStorage → 应使用默认值
    loadVoiceSettings();

    expect(document.getElementById('voiceLanguage').value).toBe('zh-CN');
    expect(document.getElementById('voiceAutoSend').checked).toBe(false);
    expect(document.getElementById('audioFormat').value).toBe('webm');
  });

  it('loadVoiceSettings 从 localStorage 读取已保存值', async () => {
    const { loadVoiceSettings } = await import('../admin-settings.js');
    setupVoiceSettingsHTML();

    localStorageMock.setItem('csai_voice_language', 'en-US');
    localStorageMock.setItem('csai_voice_auto_send', 'true');
    localStorageMock.setItem('csai_audio_format', 'mp3');

    loadVoiceSettings();

    expect(document.getElementById('voiceLanguage').value).toBe('en-US');
    expect(document.getElementById('voiceAutoSend').checked).toBe(true);
    expect(document.getElementById('audioFormat').value).toBe('mp3');
  });

  it('saveVoiceSettings 将当前值写入 localStorage', async () => {
    const { saveVoiceSettings } = await import('../admin-settings.js');
    setupVoiceSettingsHTML();

    document.getElementById('voiceLanguage').value = 'ja-JP';
    document.getElementById('voiceAutoSend').checked = true;
    document.getElementById('audioFormat').value = 'ogg';

    saveVoiceSettings();

    expect(localStorageMock.setItem).toHaveBeenCalledWith('csai_voice_language', 'ja-JP');
    expect(localStorageMock.setItem).toHaveBeenCalledWith('csai_voice_auto_send', 'true');
    expect(localStorageMock.setItem).toHaveBeenCalledWith('csai_audio_format', 'ogg');
  });

  it('voiceAutoSend change 触发 saveVoiceSettings', async () => {
    const { initVoiceSettings } = await import('../admin-settings.js');
    setupVoiceSettingsHTML();

    initVoiceSettings();

    const checkbox = document.getElementById('voiceAutoSend');
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change'));

    expect(localStorageMock.setItem).toHaveBeenCalledWith('csai_voice_auto_send', 'true');
  });
});