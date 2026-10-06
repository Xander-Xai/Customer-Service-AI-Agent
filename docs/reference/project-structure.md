> **文档定位**：CURRENT —— 随 `main` 同步的有效参考。历史快照在 `docs/reports/`，不作为当前事实。
> 首屏与总索引见 [README.md](../../README.md)；当前事实唯一入口是 [current-state.md](current-state.md)。

# 项目结构

仓库目录与职责。架构视角见 [architecture-design.md](../design/architecture-design.md)。


```
customer-service-ai-agent/
├── agents/            # 9 个 AI Agent 角色（7 领域 + ReAct + Response）+ Evaluator
├── core/              # 核心基础设施（配置/DI容器/图构建/消息总线/监控/会话/漂移检测/Prompt管理/A/B测试/Token追踪/Token配额/黑板）
│   └── session/       # 会话管理器 + 漂移检测器 + Token 计数器
├── api/               # FastAPI 服务层（工厂/中间件/路由/SSE/WebSocket/依赖注入）
│   └── routes/        # 路由模块（chat/sessions/monitoring/ws/chat_multimodal/prompts/runs）
├── auth/              # JWT 认证（Argon2id + Redis 黑名单 + Refresh Token + RBAC）
├── router/            # 双层查询路由（LLM + 规则并行 + 熔断器降级）
├── collaboration/     # 5 种协作模式 + 模式选择器 + 升级重试
├── rag/               # RAG 知识库（Qdrant v6.0 + 查询改写 + BM25 混合检索 + ApiReranker 重排 + RRF 融合 + 种子数据）
├── cache/             # Response Cache 三层（L1 Redis MD5 + L2 Qdrant 向量 + L3 Jaccard 回退）；与 Tool Result Cache/Store 分离
├── db/                # SQLAlchemy 模型 + Alembic 迁移（业务表 + AgentRun/DeadLetter/ToolSideEffect）
├── runtime/           # 分布式 Agent Runtime（AgentRun 状态机/thread lock/Celery worker/retry/DLQ）
├── erp/               # 金蝶 ERP 适配器（Mock + Real API + HMAC 认证 + 重试 + 分页）
├── tools/             # Function Calling 工具注册（OpenAI 格式 + 4 个 ERP 工具）
├── llm/               # LLM 客户端（重试 + 熔断 + FC + SSE 流式 + 连接池 + Token 配额）+ 规则兜底 LLM
├── media/             # 多模态处理（图片/音频/视频/文档/TTS 5 个处理器）
├── alerts/            # 告警通知（Webhook 钉钉/企微/飞书 + SMTP）
├── knowledge/         # 知识库管理路由
├── web/               # 前端（原生 JS + Vite 8 构建 + 34 模块 + 14 CSS + 5 页面）
│   ├── src/           # 34 JS 模块（聊天/API/Auth/工具/管理后台/测试）
│   ├── styles/        # 14 CSS 文件（变量/布局/组件/5 种主题/无障碍/管理/响应式/动画/登录）
│   └── *.html         # 5 页面（聊天/登录/管理/Widget/主题预览）
├── deploy/            # 部署资产根目录（Compose 变体 + 反代 + 日志/链路配置）
│   ├── compose/       # Docker Compose 变体（base / prod / override / canary / scale / monitoring / otel）
│   ├── nginx/         # Nginx 反向代理（Dockerfile + nginx.conf：TLS + WebSocket + canary 流量分割）
│   ├── loki/          # 日志聚合配置（loki-config.yaml + promtail-config.yaml）
│   └── otel/          # OpenTelemetry Collector 配置（traces only）
├── monitoring/        # Prometheus + Grafana + Alertmanager 配置
├── tests/             # 测试套件（pytest unit/integration/e2e/stress + eval 资产；数量以 pytest --collect-only -q 为准）
├── docs/              # 文档（active/archive/decisions + ADR）
├── alembic/           # 数据库迁移脚本（7 个版本）
└── scripts/           # 运维脚本（部署/备份/RAG 评估/密钥生成）
```

---
