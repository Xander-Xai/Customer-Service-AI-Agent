# 变更日志 (CHANGELOG)

> 本文档记录药妆智多星多智能体客服系统的所有版本变更。

---

## v4.4 (2026-06-08) — 安全加固 + 代码重构 + 测试覆盖率 + 文档完善

### 安全加固 (6 项)
- **PyJWT 替换自研 JWT**：使用 PyJWT 成熟库 + 算法白名单 HS256，防止 `alg:none` 攻击
- **JWT denylist 大小限制**：内存回退上限 10,000 条，生产无 Redis 时打印警告
- **WebSocket JWT 认证修复**：JWT 不再通过 URL 参数传递，改用首条消息认证，防止 token 泄露到日志
- **WebSocket 连接计数清理**：定期清理零连接 IP 记录，防止内存泄漏
- **CSP 安全加固**：`script-src` 移除 `unsafe-inline`，仅保留 nonce + `unsafe-hashes`
- **清理 .env.dev 真实 API Key**：替换为占位符，防止意外泄露

### 代码质量重构 (4 项)
- **session_manager.py 拆分**：→ `token_counter.py`（Token 计数 + jieba 分词）+ `drift_detector.py`（4 种漂移检测）+ 核心会话管理
- **AgentState 统一定义**：新建 `core/state.py` 单一定义点，消除 3 处重复 TypedDict
- **图构建优化**：`build_graph()` 与 `make_graph()` 共享节点逻辑，消除代码重复
- **API Key 验证增强**：占位符值更全面的检测（覆盖已知旧格式）

### 测试与 CI (3 项)
- **pytest 覆盖率门槛**：`.coveragerc` 配置 `fail_under=60`，强制覆盖率达标
- **CI 覆盖率报告**：GitHub Actions 生成 HTML 报告并上传 artifact（保留 14 天）
- **测试 fixture 修复**：`graph_app` 从 session 作用域改为 function，消除跨测试状态泄漏

### 文档 (2 项)
- **SECURITY.md**：完整的安全模型文档（认证架构 / 输入净化 / 限流策略 / LLM 安全 / 基础设施加固）
- **README 更新**：与代码实现对齐，修正过时描述

---

## v4.3 (2026-06-07) — 生产验收 + 密钥自动生成 + 低分重试

- **生产验收**：四维审查（安全 7.5 / 代码 8.0 / 测试 / 生产就绪性 7.0），综合评分 8.1/10
- **Feedback 类型修复**：`Feedback.created_at` 从 `time.time()` (float) 修正为 `datetime.now(timezone.utc)` (DateTime)
- **async 调用修复**：`container.py` 中 `create_session()` 添加 `await`
- **Prometheus 端点认证**：`/metrics/prometheus` 加入 admin 认证保护
- **密钥自动生成脚本**：`scripts/generate_prod_env.py` 生成 JWT Secret / Session Secret / API Key / Redis/PG 密码
- **低分重试机制**：ResponseAgent 评分低于 `EVAL_RETRY_THRESHOLD` 自动升级协作模式重试
- **漂移检测拆分**：`drift_detector.py` 独立模块，支持 4 种漂移类型
- **LLM Router 超时优化**：从 8s 降至 4s，配合熔断器快速 fallback
- **ReAct 迭代优化**：从 5 次降至 3 次，控制延迟在 20s 内

---

## v4.2 (2026-06-06) — SSE 真流式 + ServiceContainer 完全迁移

- **SSE 真流式输出**：`OpenAICompatibleClient.async_invoke_stream()` 使用 `httpx.stream()` 逐 token 推送
- **Agent 自动流式**：`BaseAgent._process_with_llm()` 检测 `stream_callback` 自动切换，所有 Agent 零改动
- **ServiceContainer 纯容器模式**：`api/app_factory.py` 完全使用 DI 容器，消除模块级全局变量
- **真实 LLM E2E 测试**：5/5 全部通过（硅基流动 Qwen2.5-7B-Instruct）
- **注入泄露修复**：输出层正则检测系统提示泄露 + 安全回复替换
- **RAG 检索质量评估**：30 条测试查询，Hit Rate@3 = 63.3%

---

## v4.1 (2026-06-05) — DeepSeek LLM + 依赖注入 + 全栈增强

- **DeepSeek LLM 接入**：默认模型切换为 deepseek-chat，支持 `LLM_PROVIDER` 环境变量切换
- **依赖注入容器**：`core/container.py` ServiceContainer 管理所有服务生命周期（两阶段：sync 基础设施 + async 组件）
- **SSE 流式输出**：`POST /api/chat/stream` 端点
- **Redis JWT 黑名单**：Token 吊销持久化到 Redis，重启不丢失
- **PostgreSQL 支持**：`DATABASE_URL` 环境变量切换 SQLite / PostgreSQL
- **Alembic 数据库迁移**：版本化 schema 管理
- **满意度反馈系统**：Feedback 模型 + 👍/👎 按钮 + 统计面板
- **自我评估闭环**：ResponseEvaluator 五维评分（完整性/准确性/简洁性/礼貌性/相关性）
- **A/B 测试框架**：SHA-256 确定性分流 + 统计分析
- **多模态图片识别**：`POST /api/chat/image` 端点
- **Loki 日志聚合**：Docker Compose + Promtail
- **灰度发布**：canary 服务 + Nginx 90/10 流量分割

---

## v4.0 (2026-06-05) — 用户认证 + 知识库管理 + 告警通知

- **用户认证体系**：SQLAlchemy ORM + JWT + PBKDF2-SHA256（600K 迭代）
- **双认证模式**：API Key（系统间）+ JWT Bearer（终端用户）
- **管理后台**：用户管理 / 知识库管理 / 告警配置 / 审计日志
- **知识库管理 API**：查看统计 / 重新种子 / ERP 同步 / 添加文档
- **告警通知**：Webhook（钉钉/企微/飞书）+ SMTP 邮件（含 SSRF 防护）
- **审计日志**：记录注册/登录等关键操作

---

## v3.9 (2026-06-03) — 生产就绪

- Nginx 反向代理 + TLS + WebSocket 支持
- Gunicorn 多 Worker（UvicornWorker）
- Prometheus + Grafana + Alertmanager 全栈监控
- 文件日志轮转（10MB / 5 份 / gzip 压缩）
- CI/CD 流水线（GitHub Actions：Python 3.10/3.11/3.12 + 安全扫描 + Docker 构建）

---

## v3.5 ~ v3.8 (2026-06-02 ~ 2026-06-03)

- **v3.8**：安全审计修复 + 测试合并
- **v3.7**：安全加固（监控端点 Token + WebSocket 限制 + 会话令牌签名 + TLS）
- **v3.6**：前端 WebSocket 修复 + 暗色主题 + 并发安全
- **v3.5**：RAG 知识库（ChromaDB）+ Function Calling + ReAct 推理模式

---

## v3.3 ~ v3.4 (2026-05-30 ~ 2026-06-01)

- **v3.4**：并发安全 + 安全加固 + 中文缓存（jieba + Jaccard）
- **v3.3**：缓存前置 + L1/L2 二级缓存
