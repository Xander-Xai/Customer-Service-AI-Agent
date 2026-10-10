# Resume Claims — what you may write, what you must not

> Rule: every resume line must be defensible in an interview against a `make`
> target and a repo artifact. Anything not provable is on the "do not write" list.

---

## 1. The time/version boundary (read first)

Two distinct bodies of work live near this repo:

- **Enterprise tenure (2024–2025):** the original multi-agent customer-service
  system and its early retrieval/caching work.
- **Post-departure / 2026 work:** the **distributed Agent Runtime** (PostgreSQL
  checkpoint resume, Redis per-thread locks, Celery workers, at-least-once +
  three-layer idempotency, DLQ), the **Human-in-the-Loop approval governance**,
  the **Agent Eval harness (V1)**, the **real-provider eval lane**, and the
  observability exposure-chain fixes.

⚠️ **Do not attribute any 2026 distributed-runtime / HITL / agent-eval work to the
2024–2025 enterprise role.** On a resume these are separate entries or clearly
dated personal/open-source work, not part of the earlier employment.

---

## 2. Three achievements you CAN write (each is provable)

### A. Distributed Agent Runtime with crash recovery

"Designed and implemented a distributed Agent runtime on LangGraph +
PostgreSQL checkpointer + Redis + Celery: at-least-once delivery with three-layer
application-level idempotency (run / thread-lock / tool side-effect ledger),
crash recovery from checkpoints, and a dead-letter queue with manual replay.
Recovery converges stranded runs (expired-lease `RUNNING` reclaimed by a
reconciler; attempt-exhausted runs dead-lettered) rather than orphaning them.

**Proof:** `make runtime-e2e` (110 tests on real PostgreSQL + Redis),
`make runtime-chaos` (SIGKILL worker → lease expiry → checkpoint resume, side
effect still once), `make runtime-verify` (evidence artifact with
`tested_code_sha`), and a multi-round real-infra regression recorded in
`docs/finalization/EVIDENCE_MANIFEST.md`."

> Boundary to state: this is **Level 2 (real infra, CI-verified)** — **not**
> production-cluster verified.

### A2. Finding a P0 that a green test suite was hiding

"Found and fixed a silent-orphan defect in which runs could remain `RUNNING`
forever: an expired-lease worker's completion commit was correctly rejected, the
task was ACKed, and the recovery scanner never scanned `RUNNING`. It reproduced
deterministically (3/3, always the same failure signature). Fixed without
relaxing CAS strictness or tool idempotency, with 20 new regression tests
(unit + real PostgreSQL) that fail without the fix.

While validating, I also had to correct my own first reading of the evidence:
252 stranded rows in the test database looked like proof of production
accumulation, but they turned out to be test debris from a case that
deliberately strands runs — the defect is real, that particular corroboration
was not, and the write-up says so."

**Proof:** `tests/unit/test_stale_run_reconciliation.py` (13 tests, 9 fail
pre-fix), `tests/integration/runtime/test_stale_run_reconciliation.py` (7 tests,
5 fail pre-fix), `docs/finalization/EVIDENCE_MANIFEST.md` §2.

### B. Human-in-the-Loop governance for high-risk side effects

"Built a fail-closed HITL approval boundary for high-risk tool side effects:
risk classification with a coverage-gap rule (unclaimed side-effect tools →
HIGH), Database-level CAS approval decisions (N concurrent decisions → exactly
one winner), separation of duties (`reviewer != requester`) enforced in the
service layer, TTL expiry treated as rejection, and double idempotency (approval
+ side-effect ledger) giving exactly-once effects under duplicate delivery and
worker crash.

**Proof:** `core/hitl/*`, `make runtime-e2e`, `tests/integration/runtime/test_hitl_*.py`,
`tests/unit/test_hitl_real_graph_gate.py`. Openly documented liveness boundary:
a claim-then-crash parks the run in `WAITING_APPROVAL` (no duplicate side effect);
no auto re-issue, manual runbook."

### C. Agent behavior evaluation harness with explicit evidence boundaries

"Built an Agent Eval harness that drives the **real compiled graph** with a
deterministic scripted LLM (zero network): per-metric numerator/denominator/
excluded accounting, `NOT_MEASURED` instead of fake 0%/100%, three-valued
`overall_status` (`FAIL`/`INCONCLUSIVE`/`PASS`), machine-readable
`orchestration_coverage`, and a separate, four-switch-guarded real-provider lane
that prints the plan and budget before any external request.

**Proof:** `make agent-eval` (115 cases, `INCONCLUSIVE` by design),
`make agent-eval-contract`, `make agent-eval-real` (`NOT_MEASURED`, 0 network
calls)."

---

## 3. Metrics you MUST NOT write as results

| Do not write | Why | What to write instead |
|---|---|---|
| "达到 96.2% 任务成功率" | scripted-LLM `task_completion_rate`, not the real model | "编排/治理层回归：`task_completion_rate=0.962`（脚本化 LLM，非模型能力）" |
| "路由准确率 X%" | 0 human-confirmed labels → `NOT_MEASURED` | "路由准确率受人工标注阻塞（`route_accuracy=NOT_MEASURED`）" |
| "RAG Hit@K / MRR / NDCG = X" | 649 gold are not relevance judgements; 0 JUDGED | "检索质量指标保持 `NOT_MEASURABLE`（无人工相关度判定）" |
| "生产环境高可用 / P95 / QPS" | no production evidence | "本地/CI 真实基础设施 Level 2 验证" |
| "告警闭环完成" | Alertmanager notification NOT_VERIFIED | "指标可达 + 告警在真实 Prometheus 上 FIRING 已验证；通知投递未验证" |
| "转人工 / 工单系统" | only an approval gate exists | "高风险副作用人工审批（非坐席接管/工单）" |
| "真实 ERP 退款已联调" | staging tools only | "副作用治理机制用 staging 工具验证；真实 ERP 写为 `NOT_VERIFIED`" |

---

## 4. One-line positioning (safe)

"Backend / agent-runtime engineer focused on **reliable agent orchestration**: crash-safe
distributed execution, fail-closed human approval for risky side effects, and
evaluation that refuses to confuse 'not measured' with 'passing'."
