/**
 * 主聊天页入口
 * import 所有 CSS → Vite 自动合并
 */
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/theme-dark.css';
import '../styles/theme-a11y.css';
import '../styles/theme-panel.css';
import '../styles/layout.css';
import '../styles/components.css';
import '../styles/animations.css';
import '../styles/responsive.css';

import { init } from './chat/index.js';
import { getAvailableVoices, getSelectedVoice, setVoice } from './chat/voice.js';
import { initSettingsPanel, initTheme } from './utils/theme.js';

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initSettingsPanel();
  init();

  // TTS 语音选择器初始化（延迟等待语音列表加载）
  const voiceSelect = document.getElementById('selectTTSVoice');
  if (voiceSelect) {
    const VOICE_LABELS = {
      'zh-CN-XiaoxiaoNeural': '晓晓（女声·温暖自然）',
      'zh-CN-YunxiNeural': '云希（男声·年轻）',
      'zh-CN-YunjianNeural': '云健（男声·成熟）',
      'zh-CN-XiaoyiNeural': '晓伊（女声·活泼）',
    };
    const savedVoice = localStorage.getItem('ttsVoice') || '';
    if (savedVoice) setVoice(savedVoice);

    function populateVoices() {
      const voices = getAvailableVoices();
      const entries = Object.entries(voices);
      if (!entries.length) return;
      voiceSelect.replaceChildren();
      entries.forEach(([, id]) => {
        const opt = document.createElement('option');
        opt.value = id;
        if (id === getSelectedVoice()) opt.selected = true;
        opt.textContent = VOICE_LABELS[id] || id;
        voiceSelect.appendChild(opt);
      });
    }

    // 轮询等待语音列表加载（voice.js 模块级异步请求）
    let attempts = 0;
    const poll = setInterval(() => {
      populateVoices();
      attempts++;
      if (Object.keys(getAvailableVoices()).length > 0 || attempts > 20) clearInterval(poll);
    }, 500);

    voiceSelect.addEventListener('change', () => {
      setVoice(voiceSelect.value);
      localStorage.setItem('ttsVoice', voiceSelect.value);
    });
  }
});
