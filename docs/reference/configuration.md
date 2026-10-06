> **文档定位**：CURRENT —— 随 `main` 同步的有效参考。历史快照在 `docs/reports/`，不作为当前事实。
> 首屏与总索引见 [README.md](../../README.md)；当前事实唯一入口是 [current-state.md](current-state.md)。

# 配置参考

配置项全表。模板见仓库根 [`.env.example`](../../.env.example)；默认值的唯一真相源是 `core/config.py`（支持环境变量覆盖）。

生产启动会做 fail-fast 校验（凭据长度、分布式运行时旋钮组合），不是 warning。


> 表中"默认值"是 `core/config.py` 的 **runtime fallback**；`.env.example` /
> Compose 模板可能提供 deployment recommended value（差异处均已注释），两类值不混用。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| **路由** | | |
| `ROUTING_COMPLEXITY_THRESHOLD` | 50 | 复杂度阈值（<50 快速通道 Sequential） |
| `LLM_ROUTER_TIMEOUT` | 4.0 | 路由 LLM 调用超时（秒；.env.example/Compose 模板推荐 8.0） |
| `REACT_COMPLEXITY_THRESHOLD` | 60 | ReAct 触发复杂度阈值 |
| **缓存** | | |
| `CACHE_QDRANT_COLLECTION` | response_cache | L2 Qdrant 语义缓存集合名 |
| `CACHE_QDRANT_MAX_POINTS` | 10000 | L2 Qdrant 最大点数 |
| `CACHE_VECTOR_SCORE_THRESHOLD` | 0.85 | L2 Qdrant 向量相似度阈值 |
| `CACHE_TTL_POLICY` | intent_type→TTL | 按意图类型的 TTL 策略（knowledge_qa=7d, pricing_stock=5min 等） |
| `CACHE_FALLBACK_ENABLED` | true | L3 Jaccard 降级开关 |
| `CACHE_FALLBACK_THRESHOLD` | 0.6 | L3 Jaccard 相似度阈值 |
| `CACHE_CLEANUP_INTERVAL` | 3600 | 后台清理间隔（秒） |
| **会话** | | |
| `SESSION_WINDOW_SIZE` | 10 | 滑动窗口大小 |
| `SESSION_MAX_TOKENS` | 4000 | 上下文最大 token 数（tiktoken 计数） |
| `SESSION_STORAGE_BACKEND` | memory | 会话存储后端（memory / file / redis） |
| `SESSION_SUMMARY_MAX_CHARS` | 500 | 历史摘要最大字符数 |
| `SESSION_IDLE_TTL` | 3600 | 会话空闲过期时间（秒） |
| `MAX_SESSIONS` | 10000 | 最大内存会话数 |
| **LangGraph Checkpoint** | | |
| `LANGGRAPH_CHECKPOINT_BACKEND` | 空（自动） | `memory` / `postgres`；留空开发→memory、生产→postgres |
| `LANGGRAPH_CHECKPOINT_DATABASE_URL` | 空 | checkpoint 专用 PG DSN；留空安全复用 `DATABASE_URL`（仅 postgres 协议） |
| `LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE` | 1 | psycopg 异步连接池最小连接数 |
| `LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE` | 10 | psycopg 异步连接池最大连接数 |
| `LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT` | 15.0 | 首次 `setup()`/连接池 open 超时（秒） |
| **漂移检测** | | |
| `DRIFT_TOPIC_JACCARD_THRESHOLD` | 0.15 | 话题漂移阈值（jieba Jaccard） |
| `DRIFT_REPETITION_THRESHOLD` | 0.8 | 重复提问阈值 |
| `DRIFT_ESCALATION_THRESHOLD` | 5 | 漂移升级阈值（累计次数建议转人工） |
| **熔断器** | | |
| `CIRCUIT_BREAKER_FAIL_THRESHOLD` | 5 | 熔断触发失败次数 |
| `CIRCUIT_BREAKER_RECOVERY_TIME` | 60 | 熔断恢复探测时间（秒） |
| **SLA** | | |
| `SLA_ALERT_WINDOW` | 50 | SLA 滑动窗口大小 |
| `SLA_ALERT_THRESHOLD` | 30.0 | SLA 违约率告警阈值（%） |
| `SLA_ALERT_COOLDOWN` | 300 | SLA 告警冷却时间（秒） |
| `RESPONSE_TIME_TARGET_MIN` | 5.0 | 最小响应时间 SLA（秒） |
| `RESPONSE_TIME_TARGET_MAX` | 20.0 | 最大响应时间 SLA（秒） |
| `SLA_SEQUENTIAL_MAX` | 15.0 | Sequential 模式 SLA 超时（秒） |
| `SLA_PARALLEL_MAX` | 20.0 | Parallel 模式 SLA 超时（秒） |
| `SLA_CONSULTATION_MAX` | 25.0 | Consultation 模式 SLA 超时（秒） |
| `SLA_HIERARCHICAL_MAX` | 30.0 | Hierarchical 模式 SLA 超时（秒） |
| `SLA_REACT_MAX` | 30.0 | ReAct 模式 SLA 超时（秒） |
| **重试** | | |
| `RETRY_MAX_ATTEMPTS` | 3 | 最大重试次数（仅瞬态错误，指数退避 1s/2s/4s） |
| `RETRY_BASE_DELAY` | 1.0 | 基础退避延迟（秒，指数退避 max 10s） |
| **连接池** | | |
| `HTTPX_MAX_CONNECTIONS` | 100 | httpx 最大连接数 |
| `HTTPX_KEEPALIVE_CONNECTIONS` | 20 | httpx 保活连接数 |
| **ReAct** | | |
| `REACT_MAX_ITERATIONS` | 3 | ReAct 最大推理步数 |
| `TOOL_MAX_ROUNDS` | 3 | Function Calling 最大轮数 |
| **安全** | | |
| `MAX_QUERY_LENGTH` | 2000 | 用户查询最大字符数 |
| `WS_MAX_CONNECTIONS_PER_IP` | 5 | 每 IP 最大 WebSocket 连接数 |
| `WS_MESSAGE_RATE_LIMIT` | 10 | 每分钟每连接最大消息数 |
| `WS_IDLE_TIMEOUT` | 300 | WebSocket 空闲超时（秒） |
| `JWT_SECRET` | - | JWT 签名密钥（生产必改，≥32 字符） |
| `SESSION_TOKEN_SECRET` | - | 会话令牌签名密钥（生产必改） |
| `ADMIN_PASSWORD` | - | 引导 `admin` 账号口令（仅 `app`/`canary` 首次启动需要；缺失则 crash loop） |
| `JWT_EXPIRE_HOURS` | 72 | JWT token 有效期（小时） |
| `JWT_ACCESS_EXPIRE_HOURS` | 2 | access_token 有效期（小时） |
| `JWT_REFRESH_EXPIRE_HOURS` | 168 | refresh_token 有效期（小时，默认 7 天） |
| **Token Quota** | | |
| `TOKEN_QUOTA_ENABLED` | true | Token 配额检查开关 |
| `TOKEN_QUOTA_DAILY` | 100000 | 每日 Token 配额上限 |
| `TOKEN_QUOTA_MONTHLY` | 2000000 | 每月 Token 配额上限 |
| `TOKEN_QUOTA_REDIS_PREFIX` | csai:quota: | Token Quota Redis 键前缀 |
| **会话加密** | | |
| `SESSION_ENCRYPTION_KEY` | - | 会话数据加密密钥（留空则明文存储） |
| **评估** | | |
| `EVAL_RETRY_THRESHOLD` | 30 | 低分重试触发阈值 |
| `EVAL_LOW_SCORE_THRESHOLD` | 40 | 低分告警触发阈值 |
| `MODE_UPGRADE_ENABLED` | true | 低分自动升级协作模式 |
| **LLM** | | |
| `LLM_PROVIDER` | siliconflow | LLM 提供商（siliconflow / deepseek / openai / custom） |
| **SSE** | | |
| `SSE_ENABLED` | true | SSE 流式输出开关 |
| `SSE_CHUNK_SIZE` | 50 | 每次发送的字符数 |
| **多模态** | | |
| `MULTIMODAL_ENABLED` | false | 多模态图片识别开关 |
| `MAX_IMAGE_SIZE_MB` | 5 | 最大图片大小（MB） |
| `VISION_MODEL` | - | 多模态视觉模型（留空复用 OPENAI_MODEL） |
| `CLIP_ENABLED` | false | CLIP 多模态图片检索开关 |
| **A/B 测试** | | |
| `AB_TEST_ENABLED` | false | A/B 测试开关 |
| **数据库** | | |
| `DATABASE_URL` | - | 数据库 URL（空 = SQLite，生产建议 PostgreSQL） |
| `DB_DIR` | data | SQLite 数据库目录 |
| **ERP** | | |
| `ERP_MODE` | mock | ERP 模式（mock / real） |
| `ERP_BASE_URL` | - | 金蝶 API 地址（real 模式必填） |
| `ERP_APP_ID` | - | 金蝶应用 ID |
| `ERP_APP_SECRET` | - | 金蝶应用密钥 |
| `ERP_DB_ID` | - | 金蝶数据库 ID |
| **告警** | | |
| `ALERT_WEBHOOKS` | - | 告警 Webhook（JSON 数组，支持钉钉/企微/飞书） |
| `SMTP_HOST` | - | 邮件 SMTP 服务器 |
| `ALERT_EMAIL_TO` | - | 告警邮件收件人（逗号分隔） |
| **RAG** | | |
| `RAG_PERSIST_DIRECTORY` | - | RAG 持久化目录（空 = 内存模式） |
| `RAG_N_RESULTS` | 3 | RAG 检索返回文档数 |
| `RAG_QUERY_REWRITING` | false | 查询改写开关（同义词扩展 + 多问题拆分） |
| `EMBEDDING_MODEL` | BAAI/bge-large-zh-v1.5 | Embedding 模型（HTTP API 计算，`EMBEDDING_DIM=1024`） |
| `RERANKER_MODEL` | BAAI/bge-reranker-v2-m3 | API 重排模型 |
| `HYBRID_SEARCH_ENABLED` | true | 向量 + BM25 混合检索开关 |
| `HYBRID_RRF_K` | 60 | RRF 融合平滑常数 |
| `VECTOR_DB_MODE` | qdrant_only | 向量库模式（仅 qdrant_only） |
| `QDRANT_HOST` / `QDRANT_PORT` | localhost / 6333 | Qdrant REST 连接 |
| `REACT_SELF_REFLECTION` | false | ReAct 自反思开关（工具调用后 LLM 质量自检） |
| **Tool Result Context V2** | | |
| `TOOL_RESULT_OPTIMIZATION_ENABLED` | false | V1/V2 确定性 Tool Result 压缩开关 |
| `TOOL_RESULT_OFFLOAD_ENABLED` | false | 大结果 TTL Store offload 开关 |
| `TOOL_RESULT_OFFLOAD_MIN_TOKENS` | 1200 | 触发 offload 的本地估算 token 阈值 |
| `TOOL_RESULT_STORE_TTL_SECONDS` | 900 | 外部 Tool Result 最大存活时间 |
| `TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED` | false | 可选 semantic summary fallback，默认关闭 |
| `TOOL_RESULT_CACHE_ENABLED` | false | Exact scoped Tool Result execution cache，默认关闭 |
| **Redis 键前缀** | | |
| `REDIS_JWT_PREFIX` | csai:jwt:blacklist: | JWT 黑名单键前缀 |
| `REDIS_RATE_PREFIX` | csai:rate: | 限流键前缀 |
| `REDIS_SESSION_PREFIX` | csai:session: | 会话键前缀 |

---
