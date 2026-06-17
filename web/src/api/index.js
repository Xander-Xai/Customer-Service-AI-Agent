/**
 * API 层统一导出
 */
import { emit, off, on } from './events.js';
import * as rest from './rest.js';
import { sendChatStream, sendChatStreamWithImage } from './sse.js';
import * as ws from './websocket.js';

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
  getHistory: rest.getHistory,
  getHistoryMessages: rest.getHistoryMessages,
  getSessionCheckpoint: rest.getSessionCheckpoint,
  getAlerts: rest.getAlerts,
  getCircuitBreaker: rest.getCircuitBreaker,
  submitFeedback: rest.submitFeedback,
  submitRating: rest.submitRating,
  getFeedbackStats: rest.getFeedbackStats,
  sendChatWithImage: rest.sendChatWithImage,
  sendChatWithFile: rest.sendChatWithFile,
  sendChat: rest.sendChat,

  // 管理后台 API
  getUsers: rest.getUsers,
  getAuditLog: rest.getAuditLog,
  getKnowledgeStats: rest.getKnowledgeStats,
  seedKnowledge: rest.seedKnowledge,
  syncKnowledge: rest.syncKnowledge,
  getAlertConfig: rest.getAlertConfig,
  testAlert: rest.testAlert,
  refreshToken: rest.refreshToken,

  // 监控 API
  getQualityTrends: rest.getQualityTrends,
  getHotQuestions: rest.getHotQuestions,
  getSatisfaction: rest.getSatisfaction,
  getTokenQuota: rest.getTokenQuota,
  getTokenUsage: rest.getTokenUsage,
  getPrometheusMetrics: rest.getPrometheusMetrics,

  // SSE
  sendChatStream,
  sendChatStreamWithImage,
};
