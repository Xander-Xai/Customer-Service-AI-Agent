# 药妆智多星全面代码质量改进设计方案

## 概述

对药妆智多星多智能体客服系统进行 5 阶段全面改进，覆盖安全加固、代码瘦身、质量提升、测试覆盖和工具链补全。

**当前状态**：17,277 行 Python / 79 文件 / 覆盖率 56.75% / 6 失败测试 / 无 linter/formatter

**目标状态**：净减 ~300-400 行 / 覆盖率 ≥ 80% / 0 失败测试 / ruff 全面覆盖 / 0 Critical/High 安全问题

---

## Phase 1：安全加固（优先级最高）

### 1.1 密钥清理与轮转
- **文件**：`.env`, `.env.dev`, `.env.prod.generated`
- **动作**：
  - 将 `.env` 和 `.env.dev` 中的真实 API Key (`sk-tnwwg...`) 替换为占位符
  - 将 `.env.prod.generated` 中的真实 API Key (`sk-f179...`) 替换为占位符
  - 确认 `.gitignore` 已覆盖所有 `.env*` 文件（除 `.env.example`）
  - 删除垃圾文件 `=10.0.0`
- **严重级**：Critical

### 1.2 同步阻塞修复
- **文件**：`auth/service.py:60-78`, `cache/response_cache.py:59-74`
- **动作**：
  - `auth/service.py` 中 `_TokenDenylist.add()` 和 `contains()` 的同步 Redis 调用（`setex`, `exists`）用 `asyncio.to_thread()` 包装
  - `cache/response_cache.py` 中 `_warm_up_from_redis()` 的同步 Redis 调用（`scan_iter`, `get`）用 `asyncio.to_thread()` 包装
  - `auth/service.py` 中 `authenticate_user`, `register_user`, `refresh_access_token`, `get_current_user` 使用同步 SQLAlchemy 查询，因这些函数通过 FastAPI 的 `Depends()` 同步依赖注入调用（在 `def` 路由中而非 `async def`），同步查询不会阻塞事件循环，保持现状不改动。若未来迁移到 `async def` 路由，则需改用 `AsyncSession`。
- **严重级**：High

### 1.3 CSRF 防护
- **文件**：`api/middleware.py`
- **动作**：为浏览器客户端（widget.html, index.html）的 POST 请求添加 CSRF token 校验，采用 Double Submit Cookie 或 Synchronizer Token 模式
- **严重级**：High

### 1.4 依赖冲突清理
- **文件**：`requirements-lock.txt`
- **动作**：清理锁文件中的 `python-jose`/`joserfc`/`passlib` 等与 `PyJWT` 潜在冲突的传递依赖，确认实际运行无冲突
- **严重级**：High

### 1.5 测试密钥规范化
- **文件**：`.env.test`
- **动作**：将 `test-jwt-secret`、`test-session-secret`、`changeme` 替换为更明显的伪值（如 `TEST_JWT_SECRET_NOT_REAL_xxx`）
- **严重级**：Medium

### 1.6 JWT 配置统一
- **文件**：`auth/service.py:27`
- **动作**：删除 `_JWT_EXPIRE_HOURS = int(os.getenv(...))` 的本地定义，改为从 `config.py` 导入
- **严重级**：Medium

### 1.7 安全路径异常收窄
- **文件**：`auth/service.py`, `cache/response_cache.py`
- **动作**：安全相关代码路径中的 `except Exception` 替换为具体异常类型（`RedisError`, `SQLAlchemyError`, `JWTError` 等）
- **严重级**：Medium

### 1.8 PBKDF2 升级注释
- **文件**：`auth/service.py`
- **动作**：在密码哈希代码处添加注释，说明从 PBKDF2 升级到 Argon2id 的路径和条件（不实际改动算法）
- **严重级**：Low

---

## Phase 2：代码瘦身

### 2.1 SSE 流代码去重
- **文件**：`api/routes/chat.py`
- **动作**：提取 `_sse_stream_generator(query, session_id, user, mode)` 共享函数，`stream_chat` 和 `stream_multimodal_chat` 共用
- **预估减少**：~80 行
- **当前问题**：`stream_chat` 和 `stream_multimodal_chat` 有 ~100 行近乎相同的 SSE 流处理代码

### 2.2 LLM 重试逻辑抽取
- **文件**：`llm/client.py`
- **动作**：提取 `_retry_with_backoff(func, max_attempts, base_delay)` 装饰器/工具函数，替换 3 处重复的重试循环
- **预估减少**：~40 行

### 2.3 Token 创建去重
- **文件**：`auth/service.py`
- **动作**：3 个构建 JWT payload 的函数合并为 `_create_token(user_id, role, token_type, expires_delta)` 通用函数
- **预估减少**：~30 行

### 2.4 可选依赖延迟导入
- **文件**：`media/video_processor.py`, `media/tts_processor.py`, `media/audio_processor.py`, `media/document_processor.py`
- **动作**：将 `opencv-python-headless`, `edge-tts`, `pdfplumber`, `python-docx` 改为 `try/except ImportError` 延迟导入，缺失时提供明确错误提示
- **附带**：评估是否将这些包从 `requirements.txt` 移至 `requirements-optional.txt`

### 2.5 魔法数字常量化
- **文件**：`agents/evaluator.py`, `agents/base_agent.py`, `api/routes/chat.py`, `core/monitoring.py`
- **动作**：提取为模块级常量，带注释说明取值依据
- **涉及数字**：评估器评分阈值（50, 60, 30, 15）、响应长度阈值（15, 500, 1000）、缓冲区大小（200, 100）、通知延迟（0.3, 0.5, 1.0, 1.5）

### 2.6 API 路由验证抽取
- **文件**：`api/routes/chat.py`
- **动作**：提取 FastAPI 依赖 `get_authenticated_session()` 封装重复的认证+会话验证逻辑，7 个端点共用
- **预估减少**：~30 行

### 2.7 chat_with_file 拆分
- **文件**：`api/routes/chat.py:466-569`
- **动作**：按媒体类型（图片/音频/文档）拆分为 `_handle_image_upload()`, `_handle_audio_upload()`, `_handle_document_upload()` 子函数
- **效果**：降低嵌套深度，提升可读性

---

## Phase 3：代码质量提升

### 3.1 自定义异常体系
- **文件**：新建 `exceptions.py`
- **动作**：定义分层异常类
  ```
  AppError (基类)
  ├── AuthError          — 认证/授权失败
  ├── RateLimitError     — 限流触发
  ├── ValidationError    — 输入校验失败
  ├── LLMError           — LLM 调用失败
  │   ├── LLMTimeoutError
  │   └── LLMRateLimitError
  ├── KnowledgeError     — RAG/知识库错误
  ├── SessionError       — 会话管理错误
  └── ERPError           — ERP 系统错误
  ```

### 3.2 宽泛异常修复（非安全路径）
- **文件**：全局（重点：`api/routes/`, `core/`, `agents/`, `collaboration/`）
- **动作**：逐个替换 `except Exception` 为具体异常类型，配合新的自定义异常体系；对确实无法预见的异常保留但添加 `logger.exception()` 记录

### 3.3 _dual_mode 优化
- **文件**：`session_manager.py:69-94`
- **动作**：采用方案 A — 缓存共享事件循环，避免每次 `asyncio.run()` 创建新循环。具体实现：在模块级别维护一个 `_loop` 变量，首次调用时创建并缓存，后续调用复用。此方案对现有同步调用方无侵入性改动，同时避免重复创建事件循环的开销。

### 3.4 session_manager 拆分
- **文件**：`session_manager.py`（591 行）
- **动作**：进一步拆分为：
  - `session_storage.py` — 消息存储后端（memory/file/Redis）
  - `session_manager.py` — 会话生命周期管理（精简后 ~300 行）

### 3.5 Agent 处理流程去重
- **文件**：`agents/base_agent.py`
- **动作**：提取 `_process_with_llm` 和 `_process_with_tools` 的公共流程框架为 `_execute_pipeline()`，差异部分通过策略/回调注入

### 3.6 MetricsCollector 评估
- **文件**：`core/monitoring.py`
- **动作**：评估 MetricsCollector（220 行）是否需要拆分；如果职责单一则保持现状，仅做代码清理

---

## Phase 4：测试覆盖提升

### 4.1 修复失败测试
- **文件**：`tests/test_v4_production.py`
- **动作**：修复 `TestAuthService` 和 `TestAPIIntegration` 中的 6 个失败测试（auth service 初始化和 DB setup 问题）
- **优先级**：最高（阻塞 CI）

### 4.2 补充 0% 覆盖模块测试

| 模块 | 当前 | 目标 | 测试策略 |
|------|------|------|---------|
| `media/audio_processor.py` | 0% | 80% | Mock 音频文件处理 |
| `media/document_processor.py` | 0% | 80% | Mock PDF/DOCX 解析 |
| `media/tts_processor.py` | 0% | 80% | Mock edge-tts 调用 |
| `media/video_processor.py` | 0% | 80% | Mock OpenCV 帧提取 |
| `api/app_factory.py` | 0% | 80% | TestClient 启动测试 |

### 4.3 补充低覆盖模块测试

| 模块 | 当前 | 目标 | 测试策略 |
|------|------|------|---------|
| `api/routes/chat.py` | 16% | 75% | Mock SSE 流 + 多模态 + 语音 |
| `api/routes/ws.py` | 11% | 75% | WebSocket 连接/消息/超时 |
| `api/routes/sessions.py` | 31% | 75% | 会话 CRUD 全路径 |
| `api/routes/feedback.py` | 25% | 75% | 反馈提交/统计 |
| `knowledge/router.py` | 16% | 75% | 知识管理 CRUD |
| `core/ab_testing.py` | 15% | 75% | A/B 分流逻辑 |
| `core/tracing.py` | 20% | 75% | 链路追踪 |
| `alerts/notifier.py` | 34% | 75% | 告警触发/发送 |
| `llm/rule_based_llm.py` | 17% | 75% | 规则引擎匹配 |
| `llm/client.py` | 45% | 75% | 重试/熔断/降级 |

### 4.4 测试质量保障
- 所有新测试使用 MockLLM，不依赖真实 API Key
- 异步测试使用 `pytest-asyncio` 自动模式
- 对外部依赖（Redis, DB, ChromaDB）统一使用 mock
- 测试命名遵循 `test_<模块>_<场景>_<预期结果>` 模式

---

## Phase 5：工具链补全

### 5.1 引入 ruff
- **动作**：
  - 在 `pyproject.toml` 中配置 ruff（替代 flake8 + isort + black）
  - 配置规则集：E/W（pycodestyle）、F（pyflakes）、I（isort）、UP（pyupgrade）、B（bugbear）、SIM（simplify）
  - 设置行宽 100（与 CLAUDE.md 规范一致）
  - 配置 `target-version = "py310"`

### 5.2 pre-commit hooks 增强
- **文件**：`.pre-commit-config.yaml`
- **动作**：添加 hooks：
  - `ruff check --fix`（自动修复 lint 问题）
  - `ruff format`（自动格式化）
  - `mypy`（类型检查，渐进式严格）
  - 保留现有 `.env` 提交防护 hook

### 5.3 mypy 严格化
- **文件**：`setup.cfg`
- **动作**：
  - 对核心模块（`config.py`, `exceptions.py`, `db/models.py`）开启 `disallow_untyped_defs = True`
  - 保持全局 `ignore_missing_imports = True`（第三方库类型不全）
  - 将 CI 中 mypy 改为阻塞模式（移除 `continue-on-error: true`），但仅对已严格化的模块

### 5.4 Makefile 增强
- **文件**：`Makefile`
- **动作**：用 `ruff check` + `ruff format --check` 替代 `py_compile` 语法检查

### 5.5 CI 质量门禁
- **文件**：`.github/workflows/ci.yml`
- **动作**：
  - 添加 `ruff check` 步骤（阻塞模式）
  - 添加 `ruff format --check` 步骤（阻塞模式）
  - mypy 仅对严格化模块阻塞

---

## 执行顺序与依赖关系

```
Phase 1 (安全加固) ──→ Phase 2 (代码瘦身) ──→ Phase 3 (质量提升)
                                                  │
                                                  ├─→ Phase 4 (测试覆盖)
                                                  │
                                                  └─→ Phase 5 (工具链)
```

- Phase 1 必须最先执行（安全优先）
- Phase 2 在 Phase 1 之后（瘦身减少后续审查范围）
- Phase 3 在 Phase 2 之后（在精简代码上做质量提升）
- Phase 4 和 Phase 5 可并行（无强依赖）

## 验收标准

| 阶段 | 验收标准 |
|------|---------|
| Phase 1 | `bandit` 0 High/Critical, `.env` 无真实密钥, Redis/DB 异步无阻塞 |
| Phase 2 | 无 >50 行重复代码, 无 >500 行单文件（除测试）, 无硬编码魔法数字 |
| Phase 3 | 所有异常使用自定义类型, `session_manager.py` < 350 行 |
| Phase 4 | `pytest --cov` 覆盖率 ≥ 80%, 0 失败测试 |
| Phase 5 | `ruff check` 0 错误, `ruff format --check` 通过, pre-commit hooks 正常 |
