import { getTTSVoices } from '../api/rest.js';
import { showToast } from '../utils/toast.js';
import {
  appendAssistantMessage,
  appendSystemMessage,
  appendUserMessage,
  removeTypingIndicator,
  showTypingIndicator,
} from './messages.js';
import {
  addToHistory,
  getCurrentSessionId,
  getCurrentSessionToken,
  loadSessionList,
  updateSessionInfo,
} from './sessions.js';
import { hideWelcome } from './welcome.js';

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

// TTS 语音选择
let availableVoices = {};
let selectedVoice = '';

/** 加载可用 TTS 语音列表 */
async function loadVoices() {
  try {
    const data = await getTTSVoices();
    if (data?.voices) {
      availableVoices = data.voices;
      // 默认选择第一个语音
      if (!selectedVoice && Object.keys(availableVoices).length > 0) {
        selectedVoice = Object.values(availableVoices)[0];
      }
    }
  } catch {
    /* TTS 不可用时静默失败 */
  }
}

/** 获取当前选中的 TTS 语音 */
export function getSelectedVoice() {
  return selectedVoice;
}

/** 设置 TTS 语音 */
export function setVoice(voiceId) {
  selectedVoice = voiceId;
}

/** 获取可用语音列表 */
export function getAvailableVoices() {
  return availableVoices;
}

// ===== 录音控制 =====

export async function toggleRecording() {
  if (isRecording) {
    stopRecording();
  } else {
    await startRecording();
  }
}

async function startRecording() {
  // 检查安全上下文（getUserMedia 要求 HTTPS 或 localhost）
  if (!navigator.mediaDevices?.getUserMedia) {
    const isSecure = window.isSecureContext;
    appendSystemMessage(
      isSecure
        ? '当前浏览器不支持录音功能，请使用 Chrome/Firefox/Edge 最新版'
        : '录音功能需要 HTTPS 安全连接。当前为 HTTP 访问，请使用 https:// 或 localhost 访问',
    );
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    audioChunks = [];

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => {
        t.stop();
      });
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      if (blob.size > 0) {
        _sendVoiceMessage(blob);
      }
    };

    mediaRecorder.start();
    isRecording = true;
    _updateRecordUI(true);
    showToast('正在录音...', 'info');
  } catch (err) {
    if (err.name === 'NotAllowedError') {
      appendSystemMessage('麦克风权限被拒绝。请点击浏览器地址栏的🔒图标，允许麦克风访问后重试');
    } else if (err.name === 'NotFoundError') {
      appendSystemMessage('未检测到麦克风设备。请确认麦克风已连接并启用');
    } else if (err.name === 'NotReadableError') {
      appendSystemMessage('麦克风被其他应用占用，请关闭其他录音/通话软件后重试');
    } else {
      appendSystemMessage(`麦克风访问失败: ${err.message}（${err.name}）`);
    }
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
  isRecording = false;
  _updateRecordUI(false);
}

// ===== 发送语音消息 =====

async function _sendVoiceMessage(audioBlob) {
  hideWelcome();

  appendUserMessage('[语音消息]', null);
  addToHistory({ role: 'user', content: '[语音消息]' });

  showTypingIndicator();

  const formData = new FormData();
  formData.append('audio', audioBlob, 'voice.webm');
  formData.append('session_id', getCurrentSessionId() || '');
  formData.append('session_token', getCurrentSessionToken() || '');

  const headers = {};
  const token = localStorage.getItem('token');
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  } else {
    const apiKey = localStorage.getItem('api_key') || '';
    if (apiKey) headers['X-API-Key'] = apiKey;
  }

  try {
    const resp = await fetch('/api/chat/voice', { method: 'POST', headers, body: formData });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: resp.statusText }));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();

    removeTypingIndicator();
    updateSessionInfo(result.session_id, result.session_token);

    const displayText = result.transcription
      ? `[语音] ${result.transcription}\n\n${result.response}`
      : result.response || '语音处理完成';

    appendAssistantMessage(displayText, {
      agent: result.agent || '',
      elapsed: result.elapsed || 0,
      mode: '',
      cached: false,
      agentsUsed: [],
    });
    addToHistory({ role: 'assistant', content: result.response });
    loadSessionList();
  } catch (err) {
    removeTypingIndicator();
    appendSystemMessage(`语音发送失败: ${err.message || '未知错误'}`);
  }
}

// ===== TTS 播放 =====

let currentAudio = null;

export async function playTTS(text) {
  if (!text) return;

  // 停止当前播放
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }

  // 确保语音列表已加载
  if (!Object.keys(availableVoices).length) await loadVoices();

  const headers = {};
  const token = localStorage.getItem('token');
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  } else {
    const apiKey = localStorage.getItem('api_key') || '';
    if (apiKey) headers['X-API-Key'] = apiKey;
  }

  const formData = new FormData();
  formData.append('text', text.substring(0, 2000));
  if (selectedVoice) formData.append('voice', selectedVoice);

  try {
    const resp = await fetch('/api/tts', { method: 'POST', headers, body: formData });
    if (!resp.ok) {
      return;
    }
    const audioBlob = await resp.blob();
    const audioUrl = URL.createObjectURL(audioBlob);
    currentAudio = new Audio(audioUrl);
    currentAudio.onended = () => {
      URL.revokeObjectURL(audioUrl);
      currentAudio = null;
    };
    currentAudio.play();
  } catch (_err) {}
}

// ===== UI 更新 =====

function _updateRecordUI(recording) {
  const btn = document.getElementById('btnVoice');
  if (!btn) return;
  if (recording) {
    btn.classList.add('recording');
    btn.title = '停止录音';
  } else {
    btn.classList.remove('recording');
    btn.title = '语音输入';
  }
}

export function isCurrentlyRecording() {
  return isRecording;
}

// 模块加载时预取 TTS 语音列表
loadVoices();
