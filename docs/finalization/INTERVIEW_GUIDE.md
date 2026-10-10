# Interview Guide — architecture, failures, trade-offs, pitches

> Grounded in the code and evidence in this repo. Every number here is
> reproducible from a `make` target. Nothing here claims production verification.

---

## 1. 30-second pitch

"一款化妆品企业的多智能体客服系统。四层状态机（缓存 → 路由 → 协作模式 →
响应后处理）、9 个 Agent、5 种协作模式。检索是 Qdrant 向量 + BM25 + rerank。
长任务跑在分布式 Runtime 上：PostgreSQL checkpoint、Redis 会话与 per-thread
锁、Celery worker、at-least-once 投递 + 三层幂等、崩溃可恢复。高风险写操作有
Human-in-the-Loop 审批闸门，只对 HIGH 风险拦截，fail-closed。评测分三条独立
车道：脚本化 LLM 测编排/治理、真实模型 lane（默认关闭）、人工标注。检索质量
指标因为缺人工相关度标注，按要求只发布 `NOT_MEASURABLE`，不编数字。"

## 2. 90-second pitch (adds the hard part)

"这套系统最难的不是把 Agent 串起来，是**正确性与活性边界**。比如说审批恢复：
`consume_resume` 用 `WHERE resumed_at IS NULL` 原子认领一条已批准的决策。如果
worker 在认领之后、真正执行之前崩溃，重投递**不会**再消费这条决策 —— run 就停在
`WAITING_APPROVAL`。这是**安全**的（不会重复扣款），但需要人工介入。

我们**没有**实现自动 re-issue，因为自动恢复只有在「副作用对 operation_key 幂等，
或 ledger 提交一定落库」时才是安全的；而先调外部系统再写 ledger 之间有一个真实
崩溃窗口，无法证明它不重复写。所以我保留人工 Runbook + 告警，并写进
`remaining_risks`，而不是假装审批恢复万无一失。

另一条是评测诚实性。脚本化 LLM 的 `task_completion_rate=0.962` **不是**真实模型
成功率，这句话被逐字写进每个 artifact。路由正确率必须依赖人工确认的标签 ——
当前 0 条，所以 `route_accuracy` 就是 `NOT_MEASURED`，整体 `INCONCLUSIVE`，
绝不渲染成 `PASS`。"

## 3. Architecture you should be able to draw

```
API (FastAPI)  ── /api/chat, /api/chat/stream  ── real-time fast path (inline)
               └─ /api/runs  ── enqueue ──► Celery worker ── execute_run
                                              │
                     ┌────────────────────────┼───────────────────────┐
                  LangGraph graph        PostgreSQL checkpoint     Redis (session,
                  (router → modes →      (AsyncPostgresSaver)      thread lock,
                   approval gate →                                 run events)
                   response)
```

- Four-layer state machine; Response Cache (L1 Redis MD5 / L2 Qdrant vector / L3
  Jaccard) is separate from Tool Result cache/store/compression and Session
  Memory.
- Distributed runtime: `agent_runs` is the single source of truth; Celery result
  backend is not (`task_ignore_result=True`).
- HITL gate lives between collaboration modes and `final_response`; it defers
  HIGH side effects *before execution*.

## 4. Failure cases worth telling (real, not hypothetical)

1. **P0 — ReAct mode silently dropped `pending_actions`.** HIGH-risk refunds were
   neither executed nor approved, yet the run reported success. Found by the
   agent-eval harness driving the *real* graph. Fixed by propagating
   `pending_actions` through all five modes; regression pinned by
   `tests/unit/test_hitl_real_graph_gate.py`.
2. **HITL fail-open through misconfiguration.** `HITL_ENABLED=true` with empty
   HIGH rules made every tool LOW → gate did nothing, no alert. Fixed with
   startup-time fail-closed validation + a runtime "unclaimed side-effect tool =
   HIGH" fallback.
3. **DLQ alert was un-firable.** The metric was never serialised
   (`generate_latest` had zero hits) and worker-incremented counters were
   invisible to the app-scrape. Fixed with `/metrics` exposition +
   `PROMETHEUS_MULTIPROC_DIR`; now proven FIRING on real Prometheus.
4. **Approval resume could double-execute.** `execute_raw` without a
   `tool_call_id` silently skipped the ledger. Fixed by deriving
   `tool_call_id = approval:{approval_id}`; proven exactly-once under duplicate
   delivery and crash-after-effect.
5. **P0 — runs stranded in `RUNNING` forever (silent orphan).** A worker whose
   ownership lease expired mid-execution had its `SUCCEEDED` commit *correctly*
   rejected by the owner CAS, returned normally, so Celery ACKed and the broker
   never redelivered. The recovery scanner only looked at `RETRYING`/`QUEUED` —
   so nothing ever looked at that row again: no terminal state, no DLQ record,
   no alert. It reproduced **3/3 deterministically**, and the test database had
   silently accumulated **252** such rows, the oldest stranded a week earlier.
   Two fixes: heartbeat renewal no longer dies on a transient DB error, and the
   reconciler now reclaims expired-lease `RUNNING` runs via the existing atomic
   takeover predicate. CAS strictness and tool idempotency were not relaxed —
   and `WAITING_APPROVAL` is still deliberately excluded, because auto-resend
   after a claim-then-crash cannot be proven free of duplicate side effects.

   *The best part of this story is how it stayed hidden:* (a) the failing test
   lived one directory outside what `make runtime-e2e` runs, and (b) its fake
   runtime blocked the event loop with `time.sleep()` inside `async def`, which
   starved the heartbeat and **guaranteed** the failure it was reporting — so
   the test was measuring the wrong thing while appearing to cover this path.

## 5. Trade-offs (say the cost, not just the benefit)

| Decision | Benefit | Cost accepted |
|---|---|---|
| at-least-once + 3-layer idempotency instead of exactly-once delivery | simpler, crash-safe | downstream writes need idempotency keys or manual reconciliation (W3 window) |
| Approval resume parks on claim-then-crash | never duplicates a refund | needs operator action; no auto-recovery |
| WAITING_APPROVAL not in executable statuses | no busy-loop on unattended approvals | dedicated dispatch path required |
| Scripted-LLM eval instead of real-model eval | deterministic, offline, repeatable, tests governance | does not measure model quality — explicitly out of scope |
| `INCONCLUSIVE` ≠ `PASS` | "not measured" cannot masquerade as "passing" | CI stays non-green until humans label data |
| Tool cases pinned to `react` | tool metrics get meaningful denominators | mode coverage is partial (`parallel/consultation/hierarchical` uncovered) |

## 6. Likely interviewer challenges (and honest answers)

1. **"Does this run in production?"** No — it is a **production-oriented
   architecture**, but production validation evidence is NOT_VERIFIED. Local/CI
   real infra is Level 2 only; no production cluster, no real ERP writes.
2. **"What's the actual model success rate?"** Not measured. Only the
   scripted-LLM *governance* metric exists; the real lane needs a credential and
   a human-labelled set.
3. **"What about RAG quality numbers?"** Not published. The 649 gold are not
   relevance judgements; with 0 JUDGED labels, Recall/NDCG are `NOT_MEASURABLE`.
4. **"What breaks first at scale?"** The documented liveness boundary (stuck
   approvals) and the inherent external-write at-least-once window. Both are
   known and bounded, neither is hidden.
5. **"Why not fix the stuck-approval issue?"** Because a proven-safe fix needs a
   downstream idempotency key. Until then, parking + alert is the honest choice.
6. **"Didn't your crash-recovery tests cover the stale-run case?"** They appeared
   to, and that was the problem. The suite ran real PostgreSQL and Redis, but the
   deterministically-failing test sat outside the `make runtime-e2e` target, and
   its own stand-in graph blocked the event loop so the lease heartbeat could
   never run. I only found it by asking why a *green* suite coexisted with 252
   stranded rows in the database. Green tests are evidence about what they
   execute; they are not evidence that the untested path works.
7. **"How do you know the reconciler can't kill a healthy run?"** The scan is
   global and time-based, so the window between scanning and writing matters. The
   write keeps "lease still expired" inside the same `UPDATE` as the status
   change, so a run whose lease was renewed in that window is simply not
   updated — the same atomic-predicate discipline used by `takeover_running`.
   Covered by a dedicated test.

## 7. Things you must NOT say

- "Distributed runtime is production HA" (it is local/CI verified only).
- "Scripted-LLM task completion is the model's success rate".
- "RAG hit rate is X%".
- "Alerting is closed-loop" (notification is NOT_VERIFIED).
- "Human handoff / ticketing exists" (only an approval gate exists).
- "Real ERP refunds are verified" (staging tools only).
- "The recovery scanner covers every stuck run" — it covers `RETRYING`, `QUEUED`
  and now expired-lease `RUNNING`. `WAITING_APPROVAL` is still parked by design.
