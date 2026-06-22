/**
 * 输入区模块：消息发送、文件上传（图片/文档/视频）、拖拽、输入自适应
 */
import { API } from '../api/index.js';
import {
  addMessage,
  getSelectedFile,
  getSessionId,
  getSessionToken,
  isWaiting,
  setSelectedFile,
  setWaiting,
} from '../state/chatState.js';
import { UPLOAD } from '../utils/copy.js';
import { formatFileSize } from '../utils/format.js';
import { showToast } from '../utils/toast.js';
import {
  appendAssistantMessage,
  appendSystemMessage,
  appendUserMessage,
  createStreamingMessage,
  removeTypingIndicator,
  showTypingIndicator,
} from './messages.js';
import { loadSessionList, updateSessionInfo } from './sessions.js';
import { hideWelcome } from './welcome.js';

// 状态从 chatState 读取
const ALLOWED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);
const MAX_UPLOAD_SIZE_MB = 5;

/** 判断文件是否为图片类型 */
function isImageFile(file) {
  return ALLOWED_IMAGE_TYPES.has(file.type);
}

/** 获取文件类型图标 */
function getFileIcon(file) {
  if (file.type.startsWith('image/')) return '🖼️';
  if (file.type.startsWith('video/')) return '🎬';
  if (file.type === 'application/pdf') return '📄';
  if (file.name?.endsWith('.docx') || file.name?.endsWith('.doc')) return '📝';
  return '📎';
}

// ===== 快捷提问 =====

const QUICK_PROMPT_MAP = {
  产品成分查询: '请问你们的精华液含有哪些主要成分？适合敏感肌使用吗？',
  订单物流追踪: '我想查询一下最近的订单物流状态',
  使用方法指导: '面霜和精华液的正确使用顺序是什么？',
  投诉与退款: '我收到的产品有质量问题，想要退货退款',
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
  if ((!query && !getSelectedFile()) || isWaiting()) return;

  hideWelcome();

  const file = getSelectedFile();
  const isImage = file && isImageFile(file);
  const displayQuery = file && !isImage ? `${getFileIcon(file)} ${file.name}\n${query}` : query;
  appendUserMessage(displayQuery, isImage ? file : null);
  addMessage({ role: 'user', content: query, hasFile: !!file, fileName: file?.name });

  input.value = '';
  autoResizeInput(input);
  clearFileSelection();

  // 有文件：图片走 SSE 流式，其他文件走 REST
  if (file) {
    setWaiting(true);
    updateSendButton();

    if (isImage) {
      _sendViaSSEWithImage(query, file);
    } else {
      _sendFileViaREST(query, file);
    }
    input.focus();
    return;
  }

  // 无文件：优先 SSE 流式，降级到 WebSocket
  setWaiting(true);
  updateSendButton();

  _sendViaSSE(query);
  input.focus();
}

/** SSE 流式发送（带降级） */
function _sendViaSSE(query) {
  let streaming = null;
  let hasStarted = false;
  let _isErrorHandled = false; // 防止重复错误处理

  const _controller = API.sendChatStream(query, getSessionId(), getSessionToken(), {
    onChunk(content) {
      if (!hasStarted) {
        hasStarted = true;
        removeTypingIndicator();
        streaming = createStreamingMessage();
      }
      if (streaming && content) streaming.appendChunk(content);
    },
    onContentComplete(data) {
      if (!streaming) return;
      streaming.completeContent(data?.content || '');
    },
    onDone(data) {
      setWaiting(false);
      updateSendButton();

      const rawText = streaming
        ? streaming.finalize({
            agent: data.agent || '',
            mode: data.mode || 'sequential',
            elapsed: data.elapsed || data.processing_time || 0,
            cached: data.cached || false,
            agentsUsed: data.agents_used || [],
            resolutionStatus: data.resolution_status || '',
          })
        : data.content || '';

      updateSessionInfo(data.session_id, data.session_token);
      addMessage({ role: 'assistant', content: rawText, agent: data.agent });
      loadSessionList();
    },
    onError(errMsg) {
      if (_isErrorHandled) return;
      _isErrorHandled = true;

      if (!hasStarted) {
        // SSE 未开始就失败 → 降级到 REST，同步释放等待态，避免 WS pending 卡死
        console.log('[SSE] 失败，降级到 REST:', errMsg);
        showTypingIndicator();
        API.sendChat(query, getSessionId())
          .then((result) => {
            removeTypingIndicator();
            setWaiting(false);
            updateSendButton();
            updateSessionInfo(result.session_id, result.session_token);
            appendAssistantMessage(result.response || '处理完成', {
              agent: result.agent || '',
              elapsed: result.elapsed || 0,
              mode: result.mode || '',
              cached: result.cached || false,
              agentsUsed: result.agents_used || [],
              resolutionStatus: result.resolution_status || '',
            });
            addMessage({ role: 'assistant', content: result.response, agent: result.agent });
            loadSessionList();
          })
          .catch((err) => {
            removeTypingIndicator();
            setWaiting(false);
            updateSendButton();
            appendSystemMessage(`消息发送失败。详情: ${err.message || errMsg || '未知错误'}`);
          });
      } else {
        setWaiting(false);
        updateSendButton();
        if (streaming) streaming.finalize({});
        showToast('流式传输中断', 'warning');
      }
    },
    onStatus(_data) {
      if (!hasStarted) showTypingIndicator();
    },
    // v6.0: 全链路流式新事件
    onThinking(data) {
      if (!hasStarted) {
        hasStarted = true;
        removeTypingIndicator();
        streaming = createStreamingMessage();
      }
      if (streaming) streaming.appendThinking(data.content);
    },
    onToolCall(data) {
      if (streaming) streaming.appendToolCall(data);
    },
    onToolResult(data) {
      if (streaming) streaming.appendToolResult(data);
    },
    onRagStatus(data) {
      if (streaming) streaming.updateRagStatus(data);
    },
    onAgentSwitch(data) {
      if (streaming) streaming.updateAgentTag(data.to);
    },
  });
}

/** v5.1: 多模态 SSE 流式发送（图片 + 文字，降级到 REST） */
function _sendViaSSEWithImage(query, imageFile) {
  let streaming = null;
  let hasStarted = false;

  const _controller = API.sendChatStreamWithImage(
    query,
    imageFile,
    getSessionId(),
    getSessionToken(),
    {
      onChunk(content) {
        if (!hasStarted) {
          hasStarted = true;
          removeTypingIndicator();
          streaming = createStreamingMessage();
        }
        if (streaming && content) streaming.appendChunk(content);
      },
      onContentComplete(data) {
        if (!streaming) return;
        streaming.completeContent(data?.content || '');
      },
      onDone(data) {
        setWaiting(false);
        updateSendButton();

        const rawText = streaming
          ? streaming.finalize({
              agent: data.agent || '',
              mode: data.mode || 'sequential',
              elapsed: data.elapsed || data.processing_time || 0,
              cached: data.cached || false,
              agentsUsed: data.agents_used || [],
              resolutionStatus: data.resolution_status || '',
            })
          : data.content || '';

        updateSessionInfo(data.session_id, data.session_token);
        addMessage({ role: 'assistant', content: rawText, agent: data.agent });
        loadSessionList();
      },
      onError(errMsg) {
        if (!hasStarted) {
          // SSE 流式失败 → 降级到 REST 同步
          console.log('[SSE-MM] 流式失败，降级到 REST:', errMsg);
          showTypingIndicator();
          API.sendChatWithImage(query, imageFile, getSessionId())
            .then((result) => {
              removeTypingIndicator();
              setWaiting(false);
              updateSendButton();
              updateSessionInfo(result.session_id, result.session_token);
              appendAssistantMessage(result.response || '图片分析完成', {
                agent: result.agent || '',
                elapsed: result.elapsed || 0,
                mode: result.mode || '',
                cached: false,
                agentsUsed: result.agents_used || [],
              });
              addMessage({ role: 'assistant', content: result.response });
              loadSessionList();
            })
            .catch((err) => {
              removeTypingIndicator();
              setWaiting(false);
              updateSendButton();
              appendSystemMessage(UPLOAD.imageFailed + (err.message ? `详情: ${err.message}` : ''));
            });
        } else {
          setWaiting(false);
          updateSendButton();
          if (streaming) streaming.finalize({});
          showToast('图片分析中断', 'warning');
        }
      },
      onStatus(_data) {
        if (!hasStarted) showTypingIndicator();
      },
      // v6.0: 全链路流式新事件
      onThinking(data) {
        if (!hasStarted) {
          hasStarted = true;
          removeTypingIndicator();
          streaming = createStreamingMessage();
        }
        if (streaming) streaming.appendThinking(data.content);
      },
      onToolCall(data) {
        if (streaming) streaming.appendToolCall(data);
      },
      onToolResult(data) {
        if (streaming) streaming.appendToolResult(data);
      },
      onRagStatus(data) {
        if (streaming) streaming.updateRagStatus(data);
      },
      onAgentSwitch(data) {
        if (streaming) streaming.updateAgentTag(data.to);
      },
    },
  );
}

/** 非图片文件上传（视频/PDF/DOCX/文本）走 REST /api/chat/file */
async function _sendFileViaREST(query, file) {
  showTypingIndicator();
  try {
    const result = await API.sendChatWithFile(query, file, getSessionId());
    removeTypingIndicator();
    setWaiting(false);
    updateSendButton();
    updateSessionInfo(result.session_id, result.session_token);
    appendAssistantMessage(result.response || '文件分析完成', {
      agent: result.agent || '',
      elapsed: result.elapsed || 0,
      mode: result.mode || '',
      cached: false,
      agentsUsed: result.agents_used || [],
    });
    addMessage({ role: 'assistant', content: result.response });
    loadSessionList();
  } catch (err) {
    removeTypingIndicator();
    setWaiting(false);
    updateSendButton();
    appendSystemMessage(UPLOAD.fileFailed + (err.message ? `详情: ${err.message}` : ''));
  }
}

// ===== 文件上传 =====

export function triggerFileUpload() {
  const input = document.getElementById('fileInput');
  if (input) input.click();
}

function handleFileSelect(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  validateAndSetFile(file);
  event.target.value = '';
}

function validateAndSetFile(file) {
  // 按类型分组校验
  const isImage = isImageFile(file);
  const isVideo = file.type.startsWith('video/');
  const isPdf = file.type === 'application/pdf';
  const isDoc = file.name?.endsWith('.docx') || file.name?.endsWith('.doc');
  const isText = file.type === 'text/plain' || file.name?.endsWith('.md');

  if (!isImage && !isVideo && !isPdf && !isDoc && !isText) {
    appendSystemMessage('不支持的文件格式。支持: 图片(JPEG/PNG/WebP)、视频、PDF、DOCX、TXT/MD');
    return;
  }

  // 与后端 auth/multimodal 限制保持一致：所有 /api/* 上传请求统一上限 5MB
  if (file.size > MAX_UPLOAD_SIZE_MB * 1024 * 1024) {
    appendSystemMessage(
      `文件大小超过限制（最大 ${MAX_UPLOAD_SIZE_MB}MB），当前大小: ${(file.size / 1024 / 1024).toFixed(1)}MB`,
    );
    return;
  }

  setSelectedFile(file);
  showFilePreview(file);
  updateSendButton();
}

function showFilePreview(file) {
  const area = document.getElementById('imagePreviewArea');
  const thumb = document.getElementById('imagePreviewThumb');
  const nameEl = document.getElementById('imagePreviewName');
  const sizeEl = document.getElementById('imagePreviewSize');
  if (!area) return;

  if (isImageFile(file) && thumb) {
    thumb.src = URL.createObjectURL(file);
    thumb.style.display = 'block';
  } else if (thumb) {
    thumb.style.display = 'none';
  }

  if (nameEl) nameEl.textContent = `${getFileIcon(file)} ${file.name || '文件'}`;
  if (sizeEl) sizeEl.textContent = formatFileSize(file.size);
  area.style.display = 'block';
}

export function clearFileSelection() {
  setSelectedFile(null);
  const area = document.getElementById('imagePreviewArea');
  const thumb = document.getElementById('imagePreviewThumb');
  if (area) area.style.display = 'none';
  if (thumb?.src) {
    URL.revokeObjectURL(thumb.src);
    thumb.src = '';
  }
  updateSendButton();
}

// 向后兼容别名
export const clearImageSelection = clearFileSelection;
export const triggerImageUpload = triggerFileUpload;

// ===== 拖拽上传 =====

export function initDragAndDrop() {
  const wrapper = document.getElementById('chatInputWrapper');
  const chatArea = document.querySelector('.chat-area');
  if (!wrapper || !chatArea) return;

  const preventDefaults = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach((ev) => {
    chatArea.addEventListener(ev, preventDefaults, false);
  });
  ['dragenter', 'dragover'].forEach((ev) => {
    chatArea.addEventListener(ev, () => wrapper.classList.add('drag-over'), false);
  });
  ['dragleave', 'drop'].forEach((ev) => {
    chatArea.addEventListener(ev, () => wrapper.classList.remove('drag-over'), false);
  });
  chatArea.addEventListener(
    'drop',
    (e) => {
      const files = e.dataTransfer?.files;
      if (files && files.length > 0) validateAndSetFile(files[0]);
    },
    false,
  );
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
  el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  updateSendButton();
}

export function updateSendButton() {
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('btnSend');
  if (btn) btn.disabled = !input || (!input.value.trim() && !getSelectedFile()) || isWaiting();
}

// ===== 初始化 =====

export function initInputEvents() {
  const btnAttach = document.getElementById('btnAttach');
  const imagePreviewRemove = document.getElementById('imagePreviewRemove');
  const btnSend = document.getElementById('btnSend');
  const chatInput = document.getElementById('chatInput');
  const fileInput = document.getElementById('fileInput');

  if (btnAttach) btnAttach.addEventListener('click', triggerFileUpload);
  if (imagePreviewRemove) imagePreviewRemove.addEventListener('click', clearFileSelection);
  if (btnSend) btnSend.addEventListener('click', sendMessage);
  if (chatInput) {
    chatInput.addEventListener('keydown', handleInputKeydown);
    chatInput.addEventListener('input', () => autoResizeInput(chatInput));
  }
  if (fileInput) fileInput.addEventListener('change', handleFileSelect);
}

/** 获取 isWaitingResponse 状态（供外部检查） */
export function getIsWaiting() {
  return isWaiting();
}

/** 重置等待状态（新建对话时调用） */
export function resetWaitingState() {
  setWaiting(false);
  updateSendButton();
}
