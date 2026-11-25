# 工业客服运行时发布契约（2026-11-25，候选）

## 基线和项目定位

审查基线 main `a1f4a66e43e138e4b2c8c51aa3cd35dd3644b919`（#81 已合并）。#80 reviewed-gold、#82 provider attempt metering 未合并；核查时 CI 仍待执行。此次候选独立于这两个 PR。公开仓库的增量工程不是早期任职生产交付的证明。

本仓负责客服业务路由、工具权限、人工审批、耐久任务与真实业务回执；复杂文档知识工程属于同任职阶段 Beauty RAG，通用受控分析沙箱属于另一任职阶段 Gaobao。不要为技术覆盖复制 Runtime。

## 唯一主架构

```mermaid
flowchart TD
 U[Web / Authenticated API]
 N[Nginx]
 A[FastAPI identity / session admission]
 F[Inline read-only graph]
 R[AgentRun PostgreSQL]
 Q[Celery / Redis broker]
 W[Independent worker]
 G[Shared LangGraph]
 L[Cache / Routing]
 D[Domain specialists / bounded ReAct]
 C[Parallel or fixed supervisor if needed]
 T[Tool registry / authorized ERP read adapter]
 H[HITL / operation ledger for writes]
 E[Quality gate / deterministic finalization]
 S[SSE or durable result]
 P[(PostgreSQL checkpoint / CAS)]
 I[(Redis session ownership / thread lock)]
 B[Beat / reconciler]
 X[External ERP / optional MCP and A2A]
 U --> N
 N --> A
 A --> F
 A --> R
 R --> Q
 Q --> W
 W --> G
 F --> G
 G --> L
 L --> D
 D --> C
 C --> T
 T --> H
 H --> E
 E --> S
 W --> P
 A --> I
 W --> I
 B --> R
 T --> X
```

Seven domain roles, ReAct and Response are nine registered runtime roles, not nine autonomous services. Direct routing is the default; parallel and fixed supervisor-workers share fan-out; consultation/hierarchical are compatibility profiles. No dynamic swarm. ResponseAgent is deterministic post-processing. Standalone loop is an offline/reference lane, not an alternative online executor. Local blackboard/message bus are run-local diagnostics, not distributed authorization. Durable human handoff is distinct from write approval.

## 本轮候选改变

| 旧风险 | 新边界 |
|---|---|
| 非 dev 匿名 Run 查询/取消/事件访问放行 | 无 owner 不允许客户访问，需人工核查；新 Run 要求身份 |
| 幂等键命中不核对原始请求；并发 insert 竞争可重复 publish | 验证 query/session/user，冲突 409；只有 DB 创建赢家在 API 首次投递，reconciler 仍可能重投 |
| header/body 幂等键不一致 | 400 |
| 同步 checkpoint 探测失败当作无 pending | 非 dev 严格探测，失败或旧未完成 checkpoint 拒绝，不接管 |
| JWT refresh token 被用于普通请求、数字/字符串身份不一致 | HTTP/用户查询/WS 校验 token 类型；HTTP principal 字符串统一 |
| 会话 metadata 可被当成归属、进程不一致 | industrial 服务端生成新 UUID + Redis NX owner；API、worker、历史、反馈独立复核；不存在或过期拒绝 |
| Redis 会话 I/O 失败降级 | industrial 不回退到内存/空历史，消息与 metadata 通过 Redis transaction 提交 |
| prod app real ERP 而 worker mock；加密要求不一致 | prod overlay 统一两者 ERP 模式与凭据/映射、加密配置 |
| 反馈写失败仍报成功 | industrial 返回 503，不将未提交反馈计作成功 |
| SSE 首个事件后可能等待整个图完成，图先结束时遗漏缓冲事件 | 按队列逐项转发；仅取消临时队列读取任务，图结束后排空已缓冲事件；不改变在途业务取消合同 |
| Redis ZADD 返回新增成员数而非窗口总量；登录/注册本地限流，Redis 故障回退 | 修复旧窗口 ZCARD；industrial 使用 Redis TIME + 原子 Lua 滑窗，登录/注册/普通 HTTP 独立作用域，429/503，无内存回退；健康与静态资源保持豁免 |
| 旧 WS 旁路、会话删除未覆盖活跃 Run/审批/账本 | industrial 暂停旧 WS 和旧删除入口；不是功能完成 |

## 工业候选配置

```dotenv
CSAI_RUNTIME_PROFILE=industrial
ERP_MODE=real
SESSION_ENCRYPTION_REQUIRED=true
SESSION_ENCRYPTION_PLAINTEXT_COMPAT=reject
AGENT_EXECUTION_MODE=queued
AGENT_RUN_THREAD_LOCK_BACKEND=redis
HITL_ENABLED=true
```

必须另外配置有效且授权的 ERP 凭据、可信用户到客户映射、PostgreSQL、Redis、密钥、模型及 TLS。运行时检查禁止 DEV_MODE、mock ERP、非 Redis session、非 PostgreSQL checkpoint、inline dispatcher、非 Redis thread lock 和关闭 HITL。原 demo/base 和未显式选择 industrial 的 prod 兼容配置不等于工业发布；不自动迁移旧会话。客户以真实 access JWT 创建新聊天会话，保留返回的 session ID/token；工业 POST /api/runs 要求现有会话及 Idempotency-Key。相同请求重试复用该会话。已过期 owner 不允许重新认领；30 天 TTL 是暂定策略，非完整保留/删除政策。历史列表仍基于当前实现，未保证跨副本完整分页。

本候选不承诺 Redis 丢失后可恢复会话归属；需要持久归属、迁移、备份、删除竞态治理，且校验所有客户端及替代入口。真实 ERP adapter 当前是授权读能力，退款/改单无已验证外部交易接口，不得称为已实现真实支付；未知结果必须对账，禁止自动重复外部副作用。HITL 通过不等于交易成功。MCP SKU 文档、A2A 售后服务默认关闭，真实目录/远程互操作仍未验收。模型或 RAG 降级的语义、未确认工具回执、敏感数据、媒体、反馈去重、HTTP 分布式限流与配额应按真实任务继续核验。

## 验收和阻塞

`pytest -q tests/unit/test_industrial_admission.py tests/unit/test_runs_api.py tests/test_episode_checkpoint_ownership.py --no-cov -o addopts=''`

新增离线测试覆盖配置拒绝、Redis fixture 归属拒绝与故障、严格 checkpoint 读取、请求幂等冲突；旧会话测试改用已验证 bearer，拒绝无归属会话、refresh token 和不可信状态；PG 重放用例验证同会话复用、异会话冲突；SSE 回归验证图未完成时连续转发及结束后排空缓冲。不声称已运行或通过。需 exact-head CI、真实 PostgreSQL/Redis 并发与故障注入（含 API 与 worker 接管、崩溃、重投、Redis 状态丢失）、真实客户端、真实 ERP 只读与授权范围、独立人工审批/业务回执、真实模型与 review gold、provider 成本、负载与监控、备份恢复和隐私删除验收。#75 #46 #33 #49 #31 #25 保留开放，不凭合成用例或本地证据关闭。工业 release NO-GO 直到完成真实授权和全部发布门禁。

### HTTP admission regression（2026-11-25）

工业 HTTP 使用共享 Redis 时间、原子检查及记录，IP 键由一致的 JWT secret HMAC 派生，密钥轮换会重置窗口。每次请求创建有连接及读取超时的 Redis client；限流规则使用进程配置，所有副本须一致。普通 HTTP 默认 60/60s，登录 5/300s，注册 3/3600s；拒绝不插入成员或延长 TTL。仅以可信 ASGI client 地址为依据，不自行信任转发头；真实代理配置、连接池容量、租户与任务并发配额仍待验收。新增 HTTP 200/429/503/健康豁免、旧 ZCARD 及真实 Redis 并发回归，后者纳入既有 runtime-e2e 目录。不得将这项检查等同于生产容量测试或防 DDoS 认证。

### Durable run admission（2026-11-25）

工业新 AgentRun 插入在 PostgreSQL READ COMMITTED 事务内获取统一 advisory xact lock（2 秒锁等待上限），查询非终态数量后才插入。默认全局 1000 / 用户 5 / thread 1，分别以 CSAI_MAX_OUTSTANDING_RUNS、CSAI_MAX_OUTSTANDING_RUNS_PER_USER、CSAI_MAX_OUTSTANDING_RUNS_PER_THREAD 配置；所有 API 副本须一致。PENDING、QUEUED、RUNNING、RETRYING、WAITING_APPROVAL 均占容量；终态自然释放。既有幂等键先返回旧 run 再由 service 校验原请求，饱和不阻止同请求复用。用户/thread 超限 429，全局或准入不可用 503。新增真实 PG 竞争测试，不代表真实负载容量。此边界限制新耐久任务，不限制 SSE 快路径、broker 重投、实际模型调用或 worker 并行度；历史超额状态不会被自动取消。真实配置、慢查询、连接池与锁争用、运营审批积压仍须验收。

### DLQ 重放统一准入（2026-11-25）

工业 DEAD_LETTER -> QUEUED 在 repository.transition 的同一 PostgreSQL 事务取得新建任务使用的 advisory lock 并按原用户/thread 检查容量；拒绝时保留原状态、attempt 与不可变 DLQ 历史，不投递。无 owner 或 idempotency key 的历史死信需人工对账，不直接重放；CLI 准入拒绝/不可用退出码 4。重放复用原 run_id 和 side-effect ledger。真实 PG 回归覆盖饱和拒绝、释放容量后重放、重放与新建竞争。此约束只适用于工业 profile 的受控 repository 路径，不保证直接 SQL、旧进程、外部 ERP 已执行副作用或实际 broker/worker 容量；运维 CLI 必须使用同一工业环境配置。

### SSE producer lifecycle（2026-11-25，独立候选）

文字与图片 SSE 共用 ManagedSSE，队列最多 64 个事件，生产者 await put 提供本地事件级背压；空闲 15 秒发送 SSE comment，不冒充进度。响应发送失败、生成器关闭或断线取消时取消并等待图 task，清理使用 shield；尚未进入生成器也由响应对象收尾。工业图执行 AttributeError/TypeError 不再无 config 重跑，工业 checkpoint 检查显式 strict。仅提供协作式取消，不保证同步线程/已发 provider 请求中断，不保证 checkpoint rollback 或租约 fencing；空闲 heartbeat 不是业务超时。限制的是单流事件个数，不是每事件字节、全局 SSE 并发、broker 背压或模型成本。工业断线遗留 pending checkpoint 仍需对账，禁止自动接管；真实 ASGI 代理断线与慢客户端负载待验收。
