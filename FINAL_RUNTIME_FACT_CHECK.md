# FINAL_RUNTIME_FACT_CHECK

> 审计对象：`feat/distributed-agent-runtime` @ `aa8d082`
> 原则：以代码为唯一事实源，不引用历史总结。本文件是临时审计，收口后可保留作证据。

## A. 12 个事实问题（逐条以代码回答）

| # | 问题 | 结论 | 代码证据 |
|---|---|---|---|
| 1 | Celery + Redis Broker 是否已真实实现？ | **是** | `runtime/celery_app.py`（`Celery("csai_agent", broker=CELERY_BROKER_URL)`；`CELERY_BROKER_URL` 默认复用 `REDIS_URL`）；`requirements.txt` celery |
| 2 | `/api/runs` 是否已存在？ | **是** | `api/routes/runs.py`：`POST /api/runs`、`GET /api/runs/{run_id}`、`GET /api/runs/dead`；`api/app.py` 挂载 |
| 3 | Worker 是否能消费 `run_id`？ | **是** | `runtime/tasks.py::execute_agent_run(self, run_id)`；`runtime/dispatch.py::dispatch_run` → `execute_agent_run.apply_async(args=[run_id])` |
| 4 | AgentRun 是否是业务状态真相源？ | **是** | `db/models.py::AgentRun`；`runtime/repository.py`；`runtime/celery_app.py` `task_ignore_result=True`（result backend 非真相源） |
| 5 | 是否配置 `acks_late`？ | **是** | `runtime/celery_app.py` `task_acks_late=True` |
| 6 | 是否配置 `reject_on_worker_lost`？ | **是** | `runtime/celery_app.py` `task_reject_on_worker_lost=True` |
| 7 | 是否配置 `visibility_timeout`？ | **是** | `runtime/celery_app.py` `broker_transport_options={"visibility_timeout": AGENT_RUN_VISIBILITY_TIMEOUT}`（默认 3600s） |
| 8 | retry 是否已有真实实现？ | **是（基础）** | `runtime/errors.py`（transient/permanent/timeout taxonomy + `is_retryable`）；`runtime/retry.py::compute_backoff`；`runtime/executor.py::_handle_failure`（`mark_retrying` + dispatch countdown）；`RunStatus.RETRYING`；`attempt` 计数 |
| 9 | DLQ 是否完整实现？ | **部分（state/foundation）** | 有 `DEAD_LETTER` 终态 + `agent_dead_letters` 表 + `GET /api/runs/dead` + `agent_run_dead_letter_total`；**没有** replay/requeue 运维闭环、独立 DLQ queue、告警。→ 只能说 "dead-letter state / terminal failure foundation" |
| 10 | cross-process SSE bridge 是否实现？ | **否** | `api/routes/runs.py` 无 stream/Redis Streams 端点；异步路径仅 polling |
| 11 | Kubernetes 是否实现？ | **否** | 本分支无 `deploy/k8s/`（其它未合并分支有，但不属于本分支事实） |
| 12 | 真实生产多副本是否验证？ | **否** | 仅本地双实例 + 本地真实 PG/Redis integration；无生产集群 evidence |

## B. 其它被澄清的事实

- **独立 Celery Worker service**：`deploy/compose/docker-compose.yml` 有独立 `worker:` service（`celery -A runtime.celery_app:celery_app worker`）。→ "独立 Agent Worker runtime 已实现"；但**生产 Worker Pool autoscaling / 多队列编排未实现**。
- **API 与 Worker 共用同一锁 key**：`core/concurrency/distributed_lock.py::KEY_PREFIX = "agent:thread-lock:"` 与 `runtime/thread_lock.py::KEY_PREFIX = "agent:thread-lock:"` 一致。→ 快路径与异步路径不能同时修改同一 conversation thread。
- **锁 TTL 边界**：`AGENT_RUN_THREAD_LOCK_TTL_SECONDS=300` > `AGENT_RUN_TASK_TIME_LIMIT=180`（默认值成立），但**当前无启动校验**强制该关系；本轮补 gate。
- **锁 stale-worker 边界**：owner token + Lua compare-and-delete 只防"旧 owner 错删新锁"，**不**严格消除"pause 超过 TTL 后旧 worker 继续执行"。无 lease renewal / fencing token。
- **`AGENT_RUN_DISPATCH=inline` 生产行为**：当前仅 warning（`core/config.py`），未 fail-fast；本轮收紧为生产要求 `celery`。

## C. 本轮收口范围

**补齐/收紧**：事实口径、能力层级、evidence schema v2、retry/DLQ 措辞、Worker Pool 措辞、架构一致性测试、锁 TTL 配置 gate、面试 evidence 页面、README 事实边界。

**不新增**：Redis Streams SSE bridge、完整 DLQ 运维闭环、Worker Pool autoscaling、backpressure/admission control、Kubernetes/HPA、multi-region、Kafka/Temporal/Saga/Outbox、新 Agent/RAG/模型/前端功能。
