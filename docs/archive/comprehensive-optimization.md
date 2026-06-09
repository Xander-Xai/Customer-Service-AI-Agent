# 项目全面优化设计文档

**日期**: 2026-06-08
**版本**: v4.4
**状态**: 已批准

## 概述

基于三份专业评估（代码审查 + 安全审计 + 测试评估），对项目进行 15 项系统性优化。

## Phase 1：安全加固（6项）

### 1.1 清理 .env.dev API Key
- 替换真实 SiliconFlow API Key 为占位符
- 添加 pre-commit hook 防止 .env* 文件被提交

### 1.2 WebSocket JWT 认证修复
- 前端 (api.js): JWT 不再作为 URL query 参数传递
- 后端 (app.py): 统一使用首条 WebSocket 消息传递 token

### 1.3 替换自研 JWT 为 PyJWT
- auth/service.py: 使用 PyJWT 库替代手写实现
- 保持 API 接口不变（create_token / verify_token / decode_token）
- 添加算法白名单防止 alg:none 攻击

### 1.4 JWT denylist 大小限制
- 内存 fallback 添加 MAX_DENYLIST_SIZE = 10000
- 生产环境无 Redis 时记录警告日志

### 1.5 WebSocket 连接计数清理
- 添加定期清理定时任务，移除零连接 IP 记录
- 每 5 分钟执行一次

### 1.6 CSP 移除 unsafe-inline
- script-src 仅保留 nonce，移除 unsafe-inline
- 保留 unsafe-hashes（前端有少量内联事件处理器）

## Phase 2：代码质量（4项）

### 2.1 拆分 session_manager.py
- session_store.py: SessionManager 核心（CRUD + 滑动窗口 + Redis持久化）
- drift_detector.py: DriftDetector（4种漂移检测 + jieba/tiktoken）
- token_counter.py: TokenCounter（tiktoken 计数 + 预算管理）

### 2.2 拆分 api/app.py
- api/routes/chat.py: REST 聊天端点
- api/routes/ws.py: WebSocket 处理
- api/routes/admin.py: 管理端点（知识库、会话、反馈）
- api/routes/metrics.py: Prometheus 指标
- api/middleware.py: 中间件栈（CORS、限流、安全头、认证）
- api/app.py: 仅保留 create_app 工厂函数

### 2.3 统一图构建
- multi_agent_customer_service.py: 保留 build_graph(container) 为唯一实现
- make_graph() 改为兼容包装器，内部调用 build_graph
- ServiceContainer._build_graph() 委托给 build_graph

### 2.4 统一 AgentState
- 新建 core/state.py 定义 AgentState TypedDict
- 所有引用点改为 from core.state import AgentState

## Phase 3：测试与文档（5项）

### 3.1 pytest 覆盖率门槛
- pytest.ini 添加 --cov-fail-under=80

### 3.2 CI 覆盖率报告
- ci.yml 添加 coverage job，上传报告为 artifact

### 3.3 修复 fixture 泄漏
- test_all.py graph_app fixture 从 scope="session" 改为 scope="function"

### 3.4 架构时序图
- README.md 添加 Mermaid 时序图：请求流经 4 层的完整流程

### 3.5 SECURITY.md
- 记录项目安全模型：认证架构、输入净化、CSP、LLM安全、已知限制
