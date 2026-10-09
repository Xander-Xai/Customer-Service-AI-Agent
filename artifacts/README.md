# Artifacts 证据索引

本目录存放**证据 artifact**（评测 / 运行时 / 可观测性 / 性能）。它不是"运行输出
转储"：每条正式证据都必须能从**一个干净的代码提交**复现。索引规则如下。

## 目录约定

| 路径 | 内容 | 是否正式证据 | 生成命令 |
|---|---|---|---|
| `distributed-runtime/<ts>/report.json` | 分布式 Runtime 能力验证（schema `distributed-runtime-evidence/v2`） | 是（tracked） | `make runtime-verify` |
| `runtime/chaos-<ts>.json` | worker SIGKILL → checkpoint 恢复 → 副作用仅一次 | 是（tracked） | `make runtime-chaos` |
| `runtime-diagnostics/` · `runtime-diagnostics-selftest/` | 崩溃诊断自检 | 是（tracked） | 见对应脚本 |
| `evaluation/rag-649/` | RAG 649 preflight / import 证据 | 是（tracked） | `make rag-eval-649-preflight` 等 |
| `evaluation/rag-gold-provenance/` | gold-label provenance 审计 | 是（tracked） | `scripts/rag_gold_label_provenance.py` |
| `flake-investigation/` | 历史 flake 调查（killgate cycle 序列） | 是（tracked，历史） | 见对应脚本 |
| `observability/otel-collector-*` | OTel collector flush 证据 | 是（tracked） | 见 tracing 脚本 |
| `observability/metrics-exposure-*` | Prometheus 抓取 / 告警 FIRING 端到端 | 正式（提交后重新生成） | `make metrics-exposure-verify` |
| `agent-eval/<ts>/report.json` | Agent Eval V1 行为评测 | 正式（提交后重新生成） | `make agent-eval` |
| `evaluation/performance/<ts>/report.json` | 性能/成本门禁（不可用则 BLOCKED） | 正式（提交后重新生成） | `make perf-evidence` |
| `evaluation/rag-ablation/<ts>/report.json` | BM25 消融 | 正式（提交后重新生成） | `make rag-ablation` |
| `distributed-runtime-summary/<ts>.json` | 上述运行时的汇总视图 | 派生（本地） | `make runtime-report` |
| `demo/` · `mcp/` · `evidence/` | 本地演示 / 契约临时产物（gitignore） | 否 | — |

> **时间戳目录 = 单次运行，不是"最新真相"。** 不要在文档里引用某个
> `2026…Z` 目录当作当前事实；当前事实入口是
> [docs/reference/current-state.md](../docs/reference/current-state.md)。

## 可复现纪律（重要）

1. **正式证据必须在代码提交后、干净工作区里重新生成。** 脏工作区里跑出的
   artifact 会带 `code_provenance.publication_status =
   DIRTY_WORKTREE_NOT_REPRODUCIBLE` —— 那只说明"跑过"，不构成可复现证据。
   每个支持的报告生成器都记录 `commit_sha` / `dirty` / `tracked_diff_sha256` /
   `untracked_source_sha256`（见 `core/code_provenance.py`）。
2. **不要批量提交每次运行的时间戳目录。** 重复的临时运行交给本地输出目录
   （`--out`）或 CI Artifacts；只有经过确认、用于公开陈述的正式证据才入库。
3. **不写敏感数据。** 证据里不得出现 token / 密钥 / 真实业务数据。

## 复现命令

```bash
make runtime-verify        # → artifacts/distributed-runtime/<ts>/report.json
make runtime-chaos         # → artifacts/runtime/chaos-<ts>.json
make agent-eval            # → artifacts/agent-eval/<ts>/report.json
make metrics-exposure-verify
make perf-evidence
make rag-ablation
```
