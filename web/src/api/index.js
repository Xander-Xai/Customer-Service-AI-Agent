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
  addKnowledgeDocs: rest.addKnowledgeDocs,
  getAlertConfig: rest.getAlertConfig,
  getAlertHistory: rest.getAlertHistory,
  testAlert: rest.testAlert,
  refreshToken: rest.refreshToken,
  getUserMe: rest.getUserMe,
  updateUserRole: rest.updateUserRole,
  getPromptAgents: rest.getPromptAgents,
  getPromptVersions: rest.getPromptVersions,
  createPromptVersion: rest.createPromptVersion,
  activatePromptVersion: rest.activatePromptVersion,
  getActivePrompt: rest.getActivePrompt,

  // 监控 API
  getQualityTrends: rest.getQualityTrends,
  getHotQuestions: rest.getHotQuestions,
  getSatisfaction: rest.getSatisfaction,
  getTokenQuota: rest.getTokenQuota,
  getTokenUsage: rest.getTokenUsage,
  getPrometheusMetrics: rest.getPrometheusMetrics,
  getTTSVoices: rest.getTTSVoices,
  sendVoiceForm: rest.sendVoiceForm,
  sendTTS: rest.sendTTS,

  // SSE
  sendChatStream,
  sendChatStreamWithImage,
};
