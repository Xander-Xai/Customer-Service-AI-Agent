/**
 * 输入区模块：消息发送、图片上传、拖拽、输入自适应
 */
import { API } from '../api/index.js';
import { appendUserMessage, appendAssistantMessage, appendSystemMessage, showTypingIndicator, removeTypingIndicator, createStreamingMessage, removeProgressStatus } from './messages.js';
import { hideWelcome } from './welcome.js';
import { getCurrentSessionId, getCurrentSessionToken, updateSessionInfo, addToHistory, loadSessionList } from './sessions.js';
import { formatFileSize } from '../utils/format.js';
import { showToast } from '../utils/toast.js';

let isWaitingResponse = false;
let selectedImageFile = null;

// ===== 快捷提问 =====

const QUICK_PROMPT_MAP = {
  '产品成分查询': '请问你们的精华液含有哪些主要成分？适合敏感肌使用吗？',
  '订单物流追踪': '我想查询一下最近的订单物流状态',
  '使用方法指导': '面霜和精华液的正确使用顺序是什么？',
  '投诉与退款': '我收到的产品有质量问题，想要退货退款',
};

export function useQuickPrompt(card) {
  const title = card.querySelector('.quick-prompt-text').textContent;
  const query = QUICK_PROMPT_MAP[title] || title;
  document.getElementById('chatInput').value = query;
  updateSendButton();
  sendMessage();
}

// ===== 消息发送 =====

export function sendMessage() {
  const input = document.getElementById('chatInput');
  if (!input) return;
  const query = input.value.trim();
  if ((!query && !selectedImageFile) || isWaitingResponse) return;

  hideWelcome();

  const imageFile = selectedImageFile;
  appendUserMessage(query, imageFile);
  addToHistory({ role: 'user', content: query, hasImage: !!imageFile });

  input.value = '';
  autoResizeInput(input);
  clearImageSelection();

  // 有图片：REST API
  if (imageFile) {
    isWaitingResponse = true;
    updateSendButton();
    showTypingIndicator();

    API.sendChatWithImage(query, imageFile, getCurrentSessionId()).then(result => {
      removeTypingIndicator();
      isWaitingResponse = false;
      updateSendButton();

      updateSessionInfo(result.session_id, result.session_token);

      appendAssistantMessage(result.response || '图片分析完成', {
        agent: '', elapsed: result.elapsed || 0, mode: '', cached: false, agentsUsed: [], resolutionStatus: '',
      });
      addToHistory({ role: 'assistant', content: result.response });
      loadSessionList();
    }).catch(err => {
      removeTypingIndicator();
      isWaitingResponse = false;
      updateSendButton();
      appendSystemMessage('图片发送失败: ' + (err.message || '未知错误'));
    });

    input.focus();
    return;
  }

  // 无图片：优先 SSE 流式，降级到 WebSocket
  isWaitingResponse = true;
  updateSendButton();

  _sendViaSSE(query);
  input.value = '';
  autoResizeInput(input);
  input.focus();
}

/** SSE 流式发送（带降级） */
function _sendViaSSE(query) {
  let streaming = null;
  let hasStarted = false;

  const controller = API.sendChatStream(query, getCurrentSessionId(), getCurrentSessionToken(), {
    onChunk(content) {
      if (!hasStarted) {
        hasStarted = true;
        removeTypingIndicator();
        streaming = createStreamingMessage();
      }
      if (streaming && content) streaming.appendChunk(content);
    },
    onDone(data) {
      isWaitingResponse = false;
      updateSendButton();

      const rawText = streaming ? streaming.finalize({
        agent: data.agent || '',
        mode: data.mode || 'sequential',
        elapsed: data.elapsed || data.processing_time || 0,
      }) : data.content || '';

      updateSessionInfo(data.session_id, data.session_token);
      addToHistory({ role: 'assistant', content: rawText, agent: data.agent });
      loadSessionList();
    },
    onError(errMsg) {
      if (!hasStarted) {
        // SSE 未开始就失败 → 降级到 WebSocket
        console.log('[SSE] 失败，降级到 WebSocket:', errMsg);
        showTypingIndicator();
        API.send(query, getCurrentSessionId(), getCurrentSessionToken());
      } else {
        isWaitingResponse = false;
        updateSendButton();
        if (streaming) streaming.finalize({});
        showToast('流式传输中断', 'warning');
      }
    },
    onStatus(data) {
      if (!hasStarted) showTypingIndicator();
    },
  });
}

// ===== 图片上传 =====

export function triggerImageUpload() {
  const input = document.getElementById('imageFileInput');
  if (input) input.click();
}

function handleImageSelect(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  validateAndSetImage(file);
  event.target.value = '';
}

function validateAndSetImage(file) {
  const allowedTypes = ['image/jpeg', 'image/png', 'image/webp'];
  if (!allowedTypes.includes(file.type)) {
    appendSystemMessage('不支持的图片格式，请上传 JPEG、PNG 或 WebP 格式的图片');
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    appendSystemMessage(`图片大小超过限制（最大 5MB），当前大小: ${(file.size / 1024 / 1024).toFixed(1)}MB`);
    return;
  }
  selectedImageFile = file;
  showImagePreview(file);
  updateSendButton();
}

function showImagePreview(file) {
  const area = document.getElementById('imagePreviewArea');
  const thumb = document.getElementById('imagePreviewThumb');
  const nameEl = document.getElementById('imagePreviewName');
  const sizeEl = document.getElementById('imagePreviewSize');
  if (!area || !thumb) return;
  thumb.src = URL.createObjectURL(file);
  if (nameEl) nameEl.textContent = file.name || '图片';
  if (sizeEl) sizeEl.textContent = formatFileSize(file.size);
  area.style.display = 'block';
}

export function clearImageSelection() {
  selectedImageFile = null;
  const area = document.getElementById('imagePreviewArea');
  const thumb = document.getElementById('imagePreviewThumb');
  if (area) area.style.display = 'none';
  if (thumb && thumb.src) { URL.revokeObjectURL(thumb.src); thumb.src = ''; }
  updateSendButton();
}

// ===== 拖拽上传 =====

export function initDragAndDrop() {
  const wrapper = document.getElementById('chatInputWrapper');
  const chatArea = document.querySelector('.chat-area');
  if (!wrapper || !chatArea) return;

  const preventDefaults = (e) => { e.preventDefault(); e.stopPropagation(); };
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(ev => {
    chatArea.addEventListener(ev, preventDefaults, false);
  });
  ['dragenter', 'dragover'].forEach(ev => {
    chatArea.addEventListener(ev, () => wrapper.classList.add('drag-over'), false);
  });
  ['dragleave', 'drop'].forEach(ev => {
    chatArea.addEventListener(ev, () => wrapper.classList.remove('drag-over'), false);
  });
  chatArea.addEventListener('drop', (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length > 0) validateAndSetImage(files[0]);
  }, false);
}

// ===== 输入自适应 =====

export function handleInputKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

export function autoResizeInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  updateSendButton();
}

export function updateSendButton() {
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('btnSend');
  if (btn) btn.disabled = (!input || (!input.value.trim() && !selectedImageFile)) || isWaitingResponse;
}

// ===== 初始化 =====

export function initInputEvents() {
  const btnAttach = document.getElementById('btnAttach');
  const imagePreviewRemove = document.getElementById('imagePreviewRemove');
  const btnSend = document.getElementById('btnSend');
  const chatInput = document.getElementById('chatInput');
  const imageFileInput = document.getElementById('imageFileInput');

  if (btnAttach) btnAttach.addEventListener('click', triggerImageUpload);
  if (imagePreviewRemove) imagePreviewRemove.addEventListener('click', clearImageSelection);
  if (btnSend) btnSend.addEventListener('click', sendMessage);
  if (chatInput) {
    chatInput.addEventListener('keydown', handleInputKeydown);
    chatInput.addEventListener('input', () => autoResizeInput(chatInput));
  }
  if (imageFileInput) imageFileInput.addEventListener('change', handleImageSelect);
}

/** 获取 isWaitingResponse 状态（供外部检查） */
export function getIsWaiting() { return isWaitingResponse; }
