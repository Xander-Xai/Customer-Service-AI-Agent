/**
 * API 层统一导出
 */
import { on, off, emit } from './events.js';
import * as ws from './websocket.js';
import * as rest from './rest.js';
import { sendChatStream } from './sse.js';

export const API = {
  // WebSocket
  connect: ws.connect,
  send: ws.send,
  disconnect: ws.disconnect,
  isConnected: ws.isConnected,

  // 事件系统
  on,
  off,
  emit,

  // REST API
  getHealth: rest.getHealth,
  getMetrics: rest.getMetrics,
  getKPI: rest.getKPI,
  getCacheStats: rest.getCacheStats,
  getSessions: rest.getSessions,
  getSession: rest.getSession,
  deleteSession: rest.deleteSession,
  getAlerts: rest.getAlerts,
  getCircuitBreaker: rest.getCircuitBreaker,
  submitFeedback: rest.submitFeedback,
  submitRating: rest.submitRating,
  getFeedbackStats: rest.getFeedbackStats,
  getHistory: rest.getHistory,
  getHistoryMessages: rest.getHistoryMessages,
  sendChatWithImage: rest.sendChatWithImage,
  sendChat: rest.sendChat,

  // SSE
  sendChatStream,
};
