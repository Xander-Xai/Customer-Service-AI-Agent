/**
 * 聊天页集中状态管理
 * 替代 sessions.js/input.js/messages.js 各自维护的闭包变量副本
 * 单一数据源 + getter/setter + 事件通知
 */
import { emit } from '../api/events.js';

const state = {
  sessionId: localStorage.getItem('currentSessionId') || null,
  sessionToken: localStorage.getItem('currentSessionToken') || null,
  messageHistory: [],
  isWaitingResponse: false,
  selectedFile: null,
};

export function getState() {
  return { ...state };
}
export function getSessionId() {
  return state.sessionId;
}
export function getSessionToken() {
  return state.sessionToken;
}
export function getMessageHistory() {
  return state.messageHistory;
}
export function isWaiting() {
  return state.isWaitingResponse;
}
export function getSelectedFile() {
  return state.selectedFile;
}

export function updateSession(sessionId, sessionToken) {
  if (sessionId) {
    state.sessionId = sessionId;
    localStorage.setItem('currentSessionId', sessionId);
  }
  if (sessionToken) {
    state.sessionToken = sessionToken;
    localStorage.setItem('currentSessionToken', sessionToken);
  }
  emit('sessionChanged', { sessionId: state.sessionId });
}

export function addMessage(msg) {
  state.messageHistory.push(msg);
}

export function resetSession() {
  state.sessionId = null;
  state.sessionToken = null;
  state.messageHistory = [];
  state.isWaitingResponse = false;
  state.selectedFile = null;
  localStorage.removeItem('currentSessionId');
  localStorage.removeItem('currentSessionToken');
  emit('sessionChanged', { sessionId: null });
}

export function setSession(sessionId) {
  state.sessionId = sessionId;
  state.sessionToken = null;
  state.messageHistory = [];
  localStorage.setItem('currentSessionId', sessionId);
  localStorage.removeItem('currentSessionToken');
  emit('sessionChanged', { sessionId });
}

export function setWaiting(value) {
  state.isWaitingResponse = value;
  emit('waitingChanged', { waiting: value });
}
export function setSelectedFile(file) {
  state.selectedFile = file;
}
export function resetWaiting() {
  state.isWaitingResponse = false;
  state.selectedFile = null;
}
