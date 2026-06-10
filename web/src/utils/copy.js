/**
 * UI 文案常量
 * 集中管理所有面向用户的文案，保持一致性
 */

/** 通用文案 */
export const COMMON = {
  appName: '客服助手',
  loading: '正在为您查询...',
  loadingDetailed: '正在处理中，请稍候...',
  error: '处理遇到问题，请重试',
  networkError: '网络连接异常，请检查网络后重试',
  noPermission: '您没有权限访问此页面',
  backToHome: '返回首页',
};

/** 聊天页文案 */
export const CHAT = {
  welcomeTitle: '您好，有什么可以帮您？',
  welcomeSubtitle:
    '我可以帮您查询产品信息、追踪订单进度、解答使用疑问、处理售后问题。\n支持文字、语音、图片和文档。',
  inputPlaceholder: '输入您的问题...',
  inputHint: '按 Enter 发送，Shift + Enter 换行 · 支持图片/文档/视频上传',
  emptySessionList: '还没有对话记录，开始新的对话吧',
  emptySession: '该会话暂无消息',
  newChat: '＋ 新建对话',
  sessionHistory: '会话历史',
  agentDefault: '客服助手',
  copySuccess: '已复制',
  copyFailed: '复制失败',
};

/** 快捷提问卡片 */
export const QUICK_PROMPTS = [
  { title: '查产品', icon: '🧴', desc: '成分、功效、适用肤质', query: '查产品' },
  { title: '查订单', icon: '📦', desc: '订单状态、物流进度', query: '查订单' },
  { title: '使用建议', icon: '💡', desc: '搭配方案、使用顺序', query: '使用建议' },
  { title: '售后帮助', icon: '🤝', desc: '退换货、投诉、退款', query: '售后帮助' },
];

/** 进度消息（WebSocket/SSE 阶段性提示） */
export const PROGRESS = {
  stages: ['正在为您查询...', '正在处理中...', '请稍候...', '即将完成...'],
};

/** 文件上传文案 */
export const UPLOAD = {
  imageFailed: '图片发送失败，请检查网络后重试。',
  fileFailed: '文件处理失败，请检查文件格式后重试。',
  unsupportedFormat: '不支持的文件格式。支持: 图片(JPEG/PNG/WebP)、视频、PDF、DOCX/TXT/MD',
  sizeExceeded: (maxMB, currentMB) =>
    `文件大小超过限制（最大 ${maxMB}MB），当前大小: ${currentMB}MB`,
};

/** 认证文案 */
export const AUTH = {
  loginTitle: '登录',
  registerTitle: '注册',
  loginFailed: '登录失败，请检查账号密码',
  registerFailed: '注册失败，请稍后重试',
  sessionExpired: '会话已过期，请重新登录',
};

/** 管理后台文案 */
export const ADMIN = {
  title: '管理后台',
  sections: {
    monitor: '监控概览',
    users: '用户管理',
    knowledge: '知识库',
    alerts: '告警通知',
    system: '系统状态',
  },
};

/** 角色显示名称 */
export const ROLE_LABELS = {
  customer: '客户',
  agent: '客服',
  supervisor: '主管',
  admin: '管理员',
};
