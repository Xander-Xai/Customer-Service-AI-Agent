#!/usr/bin/env python3
"""
RAG 检索质量评估脚本（v6.3 evidence 版，RAG 649 Evidence Refresh 工作单）

基于 tests/eval/rag_benchmark.json（649 条，数量以文件 metadata 为准）评估
检索/重排质量，生成可复现、可审计的机器可读 evidence artifact。

核心特性：
- 4 个检索配置 ablation：vector_only / bm25_only / hybrid_no_rerank / hybrid_rerank
  （ablation 切换仅作用于本进程的 KB 实例/请求参数，生产默认配置不变）
- 多 K 指标：Hit@K / Recall@K / Precision@K / NDCG@K（binary relevance）/ MRR@K
- 实测 latency：total + 分阶段（VECTOR/BM25/FUSION_RRF/RERANK，来自 retrieval trace），
  mean/P50/P90/P95/P99；不使用推算值
- warm-up：每实验正式 run 前 N 条 warmup（结果弃置，不计入正式统计）
- provider/index preflight gate：Qdrant 集合计数、embedding 探针、reranker 探针、BM25 rebuild
- blocker 语义（v2）：primary cause != downstream symptom —— 结构化
  primary_blocker + blockers[]（code/stage/blocking/blocks_experiments/caused_by，
  机器可读；历史 v1 artifact 的单层 status 字符串不再产生）
- 评测分母三视图（evaluation_populations，全部运行时动态计算，禁止硬编码）：
  all_queries（View A，端到端，主口径）/ retrieval_eligible（View B）/
  full_gold_covered（View C，适合 Recall@K / NDCG）
- 失败记账：exception/timeout/degraded 全部记录，failure taxonomy 规则化分类
- provenance：git SHA + benchmark sha256 + runtime config + 模型名 全部自动采集

指标分母定义（写进 artifact notes）：
- 主口径（primary metric view）固定为 all_queries（端到端）：exception / timeout /
  GOLD_NOT_INDEXED 的查询以零指标进入该 population 的分母，绝不只对 success 行取均值
- ``metrics`` / category 指标分母为 n_success（诊断用）；三视图 population_metrics
  的 all_queries.query_count 必须等于 n_total（成功 + 失败）
- latency 只在真正完成的请求上聚合；失败请求不伪造 stage/total latency 样本
- degraded（如向量通道超时降级为词法）查询计入指标，但按 degraded_reason 单独计数
- retrieval_eligible / full_gold_covered 只作为诊断视图并列输出，不得单独替代主口径

用法：
    python3 scripts/evaluate_rag.py                       # 正式 649 全量 4 实验
    python3 scripts/evaluate_rag.py --limit 16            # 冒烟（subset_run=true）
    python3 scripts/evaluate_rag.py --experiments hybrid_rerank
    python3 scripts/evaluate_rag.py --preflight-only      # 只跑 gate 不跑评测

输出：
    artifacts/evaluation/rag-649/<run-id>/report.json      # 机器可读证据（提交）
    artifacts/evaluation/rag-649/<run-id>/failures.json    # 全量失败明细（提交）
    artifacts/evaluation/rag-649/<run-id>/raw_results.json # 本地原始结果（sha256 记录于 report）
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from eval_contract import (  # noqa: E402
    CHANNELS,
    DEFAULT_KS,
    EXPERIMENT_SPECS,
    POPULATION_DEFINITIONS,
    POPULATION_VIEWS,
    REPORT_SCHEMA_VERSION,
    VERDICT_VALID,
    required_channels,
)
from rag_evidence_validity import assess_evidence_validity  # noqa: E402

DEFAULT_BENCHMARK = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "evaluation" / "rag-649"

EVAL_COLLECTIONS = [
    "product_knowledge",
    "faq",
    "tech_support",
    "complaint_knowledge",
]

# provider 认证失败判定（仅记录 HTTP 状态码本身，绝不记录凭据材料）
PROVIDER_AUTH_HTTP_STATUSES = frozenset({401, 403})


# ---------------------------------------------------------------------------
# 指标计算（纯函数，可单测）
# ---------------------------------------------------------------------------


def compute_query_metrics(
    retrieved_ids: list[str],
    expected_ids: list[str],
    ks: tuple[int, ...] = DEFAULT_KS,
) -> dict[str, float]:
    """单查询多 K 指标。binary relevance；NDCG 的 IDCG 按可命中上限截断。"""
    expected_set = set(expected_ids)
    max_k = max(ks)
    top = retrieved_ids[:max_k]

    out: dict[str, float] = {}
    first_rank = 0
    for rank, doc_id in enumerate(top, 1):
        if doc_id in expected_set and first_rank == 0:
            first_rank = rank
            break
    out["mrr"] = 1.0 / first_rank if first_rank > 0 else 0.0
    out["first_relevant_rank"] = float(first_rank)

    for k in ks:
        top_k = top[:k]
        hits = sum(1 for d in top_k if d in expected_set)
        out[f"hit@{k}"] = 1.0 if hits > 0 else 0.0
        out[f"recall@{k}"] = hits / max(len(expected_set), 1)
        out[f"precision@{k}"] = hits / k
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, d in enumerate(top_k, 1)
            if d in expected_set
        )
        n_relevant = min(len(expected_set), k)
        idcg = sum(1.0 / math.log2(i + 1) for i in range(1, n_relevant + 1))
        out[f"ndcg@{k}"] = dcg / idcg if idcg > 0 else 0.0
    return out


def reciprocal_rank(first_relevant_rank: int | float) -> float:
    """Comparable score for a rank where 0 is the MISS sentinel.

    ``first_relevant_rank == 0`` means "no relevant document in top-k" — it is
    strictly worse than any hit, not rank 0. Comparing raw ranks directly
    inverts every hit/miss transition, so uplift classification goes through
    this monotonic mapping instead.
    """
    rank = float(first_relevant_rank)
    return 1.0 / rank if rank > 0 else 0.0


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位（p∈[0,100]）。空列表返回 0.0。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def aggregate_latency(values_ms: list[float]) -> dict[str, float]:
    return {
        "n": len(values_ms),
        "mean_ms": round(sum(values_ms) / len(values_ms), 2) if values_ms else 0.0,
        "p50_ms": round(percentile(values_ms, 50), 2),
        "p90_ms": round(percentile(values_ms, 90), 2),
        "p95_ms": round(percentile(values_ms, 95), 2),
        "p99_ms": round(percentile(values_ms, 99), 2),
    }


def summarize_metric_rows(rows: list[dict[str, Any]], ks: tuple[int, ...]) -> dict[str, float]:
    """对成功行的指标取均值（分母 = n_success）。"""
    if not rows:
        return {}
    n = len(rows)
    out: dict[str, float] = {}
    for key in rows[0]:
        if key in ("mrr",) or key.startswith(("hit@", "recall@", "precision@", "ndcg@")):
            out[key] = round(sum(r[key] for r in rows) / n, 4)
    return out


def group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(str(r[key]), []).append(r)
    return grouped


def category_metrics(
    rows: list[dict[str, Any]], ks: tuple[int, ...]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cat, grp in sorted(group_by(rows, "category").items()):
        lat = [r["total_ms"] for r in grp]
        entry = {
            "query_count": len(grp),
            "metrics": summarize_metric_rows(grp, ks),
            "latency": aggregate_latency(lat),
        }
        out[cat] = entry
    return out


# ---------------------------------------------------------------------------
# 评测分母三视图（population protocol；全部运行时动态计算，禁止硬编码）
# ---------------------------------------------------------------------------

def population_flags(expected_ids: list[str], corpus_ids: set[str]) -> dict[str, Any]:
    """单查询的 population 归属标记（基于 gold docs 是否在索引 corpus 中）。"""
    present = sum(1 for gid in expected_ids if gid in corpus_ids)
    return {
        "gold_expected_count": len(expected_ids),
        "gold_in_corpus_count": present,
        "retrieval_eligible": present > 0,
        "full_gold_covered": bool(expected_ids) and present == len(expected_ids),
    }


def population_counts(
    queries: list[dict[str, Any]], corpus_ids: set[str]
) -> dict[str, int]:
    """三视图分母计数（动态计算；不依赖任何硬编码数字）。"""
    eligible = sum(
        1 for q in queries if any(gid in corpus_ids for gid in q["expected_doc_ids"])
    )
    covered = sum(
        1
        for q in queries
        if q["expected_doc_ids"]
        and all(gid in corpus_ids for gid in q["expected_doc_ids"])
    )
    return {
        "all_queries": len(queries),
        "retrieval_eligible": eligible,
        "full_gold_covered": covered,
    }


def zero_metric_row(
    query_id: str, expected_ids: list[str], corpus_ids: set[str], ks: tuple[int, ...]
) -> dict[str, Any]:
    """Denominator row for a failed request.

    Exceptions/timeouts must still enter the end-to-end ``all_queries``
    population with zero-valued metrics — otherwise the primary view silently
    averages over survivors and inflates results. ``total_ms`` stays ``None``
    so the latency stage never fabricates a stage/total sample for a request
    that did not complete.
    """
    return {
        "query_id": query_id,
        "total_ms": None,
        **population_flags(expected_ids, corpus_ids),
        **compute_query_metrics([], expected_ids, ks),
    }


def population_metrics(rows: list[dict[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    """按三视图聚合指标/latency。

    ``rows`` 是「分母行」：成功行 + 失败查询的零指标行（end-to-end
    all_queries 主口径）。因此 ``all_queries.query_count`` 必须等于该实验的
    n_total，而 latency 只在真正完成的请求上聚合（``total_ms is not None``）。
    """
    views = {
        "all_queries": rows,
        "retrieval_eligible": [r for r in rows if r.get("retrieval_eligible")],
        "full_gold_covered": [r for r in rows if r.get("full_gold_covered")],
    }
    out: dict[str, Any] = {}
    for name in POPULATION_VIEWS:
        sub = views[name]
        out[name] = {
            "query_count": len(sub),
            "metrics": summarize_metric_rows(sub, ks),
            "latency": aggregate_latency(
                [r["total_ms"] for r in sub if r.get("total_ms") is not None]
            ),
        }
    return out


# ---------------------------------------------------------------------------
# 失败分类（规则化）
# ---------------------------------------------------------------------------


def classify_failure(
    *,
    gold_in_corpus_flags: list[bool],
    first_rank: int,
    exception_type: str | None,
    degraded_reason: str,
    wall_ms: float,
    timeout_s: float | None,
) -> tuple[str, list[str]]:
    """规则化 failure taxonomy。

    返回 (primary_label, diagnostics)。优先级：
    TIMEOUT > PROVIDER_ERROR > GOLD_NOT_INDEXED > MISS_ALL > LOW_RANK。
    通道级诊断（VECTOR_MISS/BM25_MISS/FUSION_DEGRADED/RERANK_DEGRADED）进 diagnostics。
    """
    diagnostics: list[str] = []
    if exception_type == "timeout" or degraded_reason == "retrieval_timeout":
        diagnostics.append("VECTOR_TIMEOUT_DEGRADED")
    if degraded_reason and degraded_reason != "retrieval_timeout":
        diagnostics.append(f"CHANNEL_DEGRADED:{degraded_reason}")

    if exception_type == "timeout" or (
        timeout_s is not None and wall_ms > timeout_s * 1000
    ):
        return "TIMEOUT", diagnostics
    if exception_type is not None:
        return "PROVIDER_ERROR", diagnostics
    if not any(gold_in_corpus_flags):
        return "GOLD_NOT_INDEXED", diagnostics
    if first_rank == 0:
        return "MISS_ALL", diagnostics
    if first_rank > 3:
        return "LOW_RANK", diagnostics
    return "HIT", diagnostics  # rank 1..3: not a failure (internal label)


# ---------------------------------------------------------------------------
# Provenance / environment（自动采集，不手填）
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, check=True,
        ).stdout.strip()
    except Exception:
        return "unavailable"
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, check=True,
        ).stdout.strip()
    except Exception:
        dirty = ""
    return f"{sha}+dirty" if dirty else sha


def collect_config() -> dict[str, Any]:
    from core import config as cfg

    return {
        "vector_db_mode": cfg.VECTOR_DB_MODE,
        "hybrid_search_enabled": cfg.HYBRID_SEARCH_ENABLED,
        "hybrid_vector_top_k": cfg.HYBRID_VECTOR_TOP_K,
        "hybrid_bm25_top_k": cfg.HYBRID_BM25_TOP_K,
        "hybrid_rrf_k": cfg.HYBRID_RRF_K,
        "rag_n_results": cfg.RAG_N_RESULTS,
        "rag_query_rewriting": cfg.RAG_QUERY_REWRITING,
        "embedding_model": cfg.EMBEDDING_MODEL,
        "embedding_dim": cfg.EMBEDDING_DIM,
        "reranker_model": cfg.RERANKER_MODEL,
    }


def load_benchmark(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"  [FATAL] 基准文件不存在: {path}")
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("metadata", {})
    queries = data.get("queries", [])
    declared = meta.get("total_queries")
    if declared != len(queries):
        print(
            f"  [FATAL] benchmark metadata 不一致: "
            f"metadata.total_queries={declared} != len(queries)={len(queries)}"
        )
        sys.exit(2)
    return data


# ---------------------------------------------------------------------------
# Preflight gates
# ---------------------------------------------------------------------------


def http_status_from_exception(exc: BaseException | None, max_depth: int = 5) -> int | None:
    """沿异常因果链（__cause__/__context__）提取 HTTP 状态码。

    只返回状态码整数本身；response body / headers / 凭据材料一律不采集。
    找不到时返回 None（例如本地构造的 EmbeddingUnavailableError 无底层响应）。
    """
    cur = exc
    seen = 0
    while cur is not None and seen < max_depth:
        status = getattr(getattr(cur, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return None


def probe_reranker(*, rerank_probe: bool = True) -> dict[str, Any]:
    """Probe the reranker and report **this call's** structured facts.

    Consumes the same per-call typed outcome the runtime uses
    (``ApiReranker.rerank_with_outcome`` → ``RerankOutcome``), so
    ``applied`` / ``degraded`` / ``reason`` / ``http_status`` /
    ``provider_called`` are direct facts rather than an inference.

    Two things this deliberately does **not** do:

    - infer success from the returned list's shape (``"rerank_score" in r``).
      That re-creates a second truth model next to ``RerankOutcome.applied``.
    - read the instance's shared ``last_error_status`` to attribute this call.
      It is mutable per-instance state, so the evaluation contract and the
      production runtime must not disagree about where truth comes from.

    Probe vocabulary is ``ok`` / ``degraded`` / ``failed`` / ``None``.
    ``silent_fallback`` was the pre-v2 label for runtime behaviour that is now
    explicit and structured; the specific cause lives in ``reason``.
    """
    gate: dict[str, Any] = {}
    try:
        from rag.reranker import create_reranker

        rr = create_reranker()
        gate["configured"] = rr.available
        gate["model"] = rr._model
        if rr.available and rerank_probe:
            outcome = rr.rerank_with_outcome(
                "透明质酸功效",
                [
                    {"id": "a", "content": "透明质酸保湿"},
                    {"id": "b", "content": "防晒指数"},
                ],
                top_k=2,
            )
            applied = bool(outcome.applied)
            degraded = bool(outcome.degraded)
            gate["applied"] = applied
            gate["degraded"] = degraded
            gate["reason"] = outcome.reason_value
            gate["provider_called"] = bool(outcome.provider_called)
            gate["http_status"] = outcome.http_status
            gate["probe"] = "ok" if (applied and not degraded) else "degraded"
            if not (applied and not degraded):
                # detail carries only the bounded reason enum. Never an
                # exception message, provider response body, query or documents.
                gate["detail"] = (
                    f"reranker 探针未确认真实 rerank：reason={outcome.reason_value}"
                    f"（仅阻塞 hybrid_rerank）"
                )
    except Exception as e:  # noqa: BLE001 - gate 必须记录失败而不是中断
        gate.update(
            {
                "configured": False,
                "probe": "failed",
                "http_status": http_status_from_exception(e),
                "error": type(e).__name__,
            }
        )
    return gate


def _reranker_verdict_is_ok(
    *,
    reranker_applied: bool | None,
    reranker_degraded: bool | None,
    reranker_probe: str | None,
) -> bool:
    """Did a real rerank happen? Typed facts win; the probe string is a fallback.

    Keeping this in one place stops a third truth model from creeping in: the
    gate verdict and the blocker verdict must agree on what "ok" means.
    """
    if reranker_applied is not None:
        return bool(reranker_applied) and not bool(reranker_degraded)
    return reranker_probe == "ok"


def derive_blockers(
    *,
    embedding_configured: bool,
    embedding_probe: str | None,
    embedding_http_status: int | None,
    reranker_configured: bool = True,
    reranker_probe: str | None = None,
    reranker_http_status: int | None = None,
    reranker_applied: bool | None = None,
    reranker_degraded: bool | None = None,
    reranker_reason: str | None = None,
    reranker_provider_called: bool | None = None,
    qdrant_total_points: int,
    requested_experiments: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """preflight 复合判定 v2：primary cause != downstream symptom。

    因果链：provider authentication failure → embedding unavailable
    → corpus 无法 embed/import → Qdrant 为空 → vector/hybrid 评测不可运行。
    因此：
    - embedding/reranker 认证失败 → PROVIDER_AUTH 类 blocker（根因候选）
    - 索引为空 + embedding 不可用 → VECTOR_INDEX_EMPTY 为 downstream blocker，
      标记 caused_by=EMBEDDING_PROVIDER_AUTH（不与根因互斥/矛盾）
    - 索引为空 + embedding 健康 → VECTOR_INDEX_EMPTY 本身是根因（待导入）
    - reranker 失败只阻塞 hybrid_rerank；不得据此阻塞
      vector_only / bm25_only / hybrid_no_rerank

    reranker verdict **优先取自 typed per-call facts**
    （``reranker_applied`` / ``reranker_degraded`` / ``reranker_reason``），
    与 runtime 使用同一个 ``RerankOutcome`` 契约。``reranker_probe`` 仅作为
    旧调用方与纯函数测试的兼容入口；typed facts 缺失时才回退到它。

    返回 {"status", "primary_blocker", "blockers"}；字段全部机器可读。
    """
    wanted = set(requested_experiments) & set(EXPERIMENT_SPECS)
    vector_channel_needed = bool(wanted & {"vector_only", "hybrid_no_rerank", "hybrid_rerank"})
    bm25_needed = bool(wanted & {"bm25_only", "hybrid_no_rerank", "hybrid_rerank"})
    rerank_needed = "hybrid_rerank" in wanted

    blockers: list[dict[str, Any]] = []

    embedding_auth = embedding_probe == "failed" and (
        embedding_http_status in PROVIDER_AUTH_HTTP_STATUSES
    )
    if embedding_probe == "failed":
        blockers.append({
            "code": (
                "EMBEDDING_PROVIDER_AUTH"
                if embedding_auth else "EMBEDDING_PROVIDER_UNAVAILABLE"
            ),
            "stage": "embedding",
            "blocking": vector_channel_needed,
            "blocks_experiments": [
                e for e in ("vector_only", "hybrid_no_rerank", "hybrid_rerank") if e in wanted
            ],
            "blocks_corpus_import": not embedding_configured or embedding_auth,
            "http_status": embedding_http_status,
            "detail": "embedding provider 探针失败：语料导入与向量依赖实验无法进行",
        })
    elif not embedding_configured:
        blockers.append({
            "code": "EMBEDDING_PROVIDER_UNAVAILABLE",
            "stage": "embedding",
            "blocking": vector_channel_needed,
            "blocks_experiments": [
                e for e in ("vector_only", "hybrid_no_rerank", "hybrid_rerank") if e in wanted
            ],
            "blocks_corpus_import": True,
            "http_status": None,
            "detail": "embedding 通道未配置（无 embed_fn），向量依赖实验与导入均不可用",
        })

    if qdrant_total_points == 0:
        downstream: dict[str, Any] = {
            "code": "VECTOR_INDEX_EMPTY",
            "stage": "qdrant",
            "blocking": vector_channel_needed or bm25_needed,
            "blocks_experiments": [e for e in EXPERIMENT_SPECS if e in wanted],
            "detail": (
                "评测 collection 全部为 0 points；BM25 索引由 Qdrant 重建，"
                "因此 bm25_only 同样被阻塞；修复根因后运行 "
                "python3 scripts/import_eval_corpus.py 导入语料"
            ),
        }
        if any(b["code"].startswith("EMBEDDING_PROVIDER") for b in blockers):
            downstream["caused_by"] = blockers[0]["code"]
        blockers.append(downstream)

    if rerank_needed and (not reranker_configured or reranker_probe is None):
        # Unconfigured / unprobed must never be read as healthy: hybrid_rerank
        # would then run without a proven rerank and be counted as a real rerank
        # ablation. Blocks hybrid_rerank only.
        blockers.append({
            "code": "RERANKER_PROVIDER_UNAVAILABLE",
            "stage": "reranker",
            "blocking": False,
            "blocks_experiments": ["hybrid_rerank"],
            "http_status": reranker_http_status,
            "detail": (
                "reranker 未配置或未探测（RERANKER_API_KEY 缺失 / 探针未运行）；"
                "不得把 probe=None 当作健康；仅阻塞 hybrid_rerank"
            ),
        })
    elif rerank_needed and not _reranker_verdict_is_ok(
        reranker_applied=reranker_applied,
        reranker_degraded=reranker_degraded,
        reranker_probe=reranker_probe,
    ):
        # Typed facts are canonical when present: `applied` comes from the
        # per-call outcome, never inferred from the returned list's shape and
        # never from shared mutable state.
        reranker_auth = reranker_http_status in PROVIDER_AUTH_HTTP_STATUSES
        reason = reranker_reason or "provider_error"
        blockers.append({
            "code": "RERANKER_PROVIDER_AUTH" if reranker_auth else "RERANKER_PROVIDER_DEGRADED",
            "stage": "reranker",
            # 只阻塞 hybrid_rerank；不影响 vector_only/bm25_only/hybrid_no_rerank
            "blocking": False,
            "blocks_experiments": ["hybrid_rerank"],
            "http_status": reranker_http_status,
            # detail 只带 bounded reason / 状态码；不含 exception message、
            # provider 响应体、query 或 documents。
            "detail": (
                f"reranker 探针未确认真实 rerank：reason={reason}"
                f"（仅阻塞 hybrid_rerank，其余实验照常运行）"
            ),
        })

    blocking = [b for b in blockers if b["blocking"]]
    primary: str | None = None
    for code in ("EMBEDDING_PROVIDER_AUTH", "EMBEDDING_PROVIDER_UNAVAILABLE", "VECTOR_INDEX_EMPTY"):
        if any(b["code"] == code for b in blocking):
            primary = code
            break
    status = "BLOCKED" if blocking else ("PARTIAL" if blockers else "OK")
    return {"status": status, "primary_blocker": primary, "blockers": blockers}


def preflight(
    kb,
    *,
    rerank_probe: bool,
    requested_experiments: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """provider/index gate。所有 gate 都执行并记录（阻塞原因要可审计）。

    ``requested_experiments`` 决定 blocker 只针对本次 CLI 选择的实验派生：
    ``--experiments bm25_only`` 不应因 embedding provider 故障而整体退出。
    未提供时退回全部 canonical 实验（旧行为，用于 preflight-only 默认）。
    """

    gates: dict[str, Any] = {}

    # 1. Qdrant 集合（含 client/server 版本差异记录）
    collections_info: dict[str, int] = {}
    for c in EVAL_COLLECTIONS:
        collections_info[c] = kb.get_collection_count(c)
    versions: dict[str, Any] = {}
    try:
        import importlib.metadata

        versions["client"] = importlib.metadata.version("qdrant-client")
    except Exception:
        versions["client"] = "unknown"
    try:
        import httpx

        from core.config import QDRANT_HOST, QDRANT_PORT

        root = httpx.get(f"http://{QDRANT_HOST}:{QDRANT_PORT}/", timeout=5.0).json()
        versions["server"] = root.get("version", "unknown")
    except Exception:
        versions["server"] = "unknown"
    gates["qdrant"] = {
        "reachable": kb.available,
        "versions": versions,
        "collections": collections_info,
        "total_points": sum(collections_info.values()),
    }

    # 2. Embedding 探针（无论索引是否为空都探测，记录真实凭据状态；
    #    configured=embed_fn 已配置；probe=实际调用结果；绝不记录凭据材料）
    embedding_gate: dict[str, Any] = {"configured": kb.embedding_available}
    if kb.embedding_available:
        try:
            vec = kb._embed_texts(["评测探针：透明质酸功效"])[0]
            embedding_gate.update(
                {"probe": "ok", "dim": len(vec), "model": kb._embedding_model_name}
            )
        except Exception as e:  # noqa: BLE001 - 评测必须记录任何失败并继续
            embedding_gate.update(
                {
                    "probe": "failed",
                    "error": type(e).__name__,
                    "detail": str(e)[:200],
                    "http_status": http_status_from_exception(e),
                }
            )
    gates["embedding"] = embedding_gate

    # 3. BM25 rebuild（未 READY 时执行；READY 时保留现有索引）
    from rag.qdrant_knowledge_base import BM25Readiness

    if kb.bm25_readiness() is not BM25Readiness.READY:
        meta = kb.rebuild_bm25_from_qdrant(EVAL_COLLECTIONS)
        gates["bm25_rebuild"] = {
            "executed": True,
            "readiness": str(kb.bm25_readiness()),
            "document_count": meta.document_count if meta else 0,
        }
    else:
        meta = kb.bm25_index_meta()
        gates["bm25_rebuild"] = {
            "executed": False,
            "readiness": str(kb.bm25_readiness()),
            "document_count": meta.document_count if meta else 0,
        }

        # 4. Reranker 探针（consumes the same typed per-call outcome as runtime）
    reranker_gate = probe_reranker(rerank_probe=rerank_probe)
    gates["reranker"] = reranker_gate

    # 5. 复合判定（primary cause != downstream symptom；机器可读 blockers）
    embedding_gate = gates["embedding"]
    assessment = derive_blockers(
        embedding_configured=embedding_gate.get("configured", False),
        embedding_probe=embedding_gate.get("probe"),
        embedding_http_status=embedding_gate.get("http_status"),
        reranker_configured=gates["reranker"].get("configured", False),
        reranker_probe=gates["reranker"].get("probe"),
        reranker_http_status=gates["reranker"].get("http_status"),
        # typed per-call facts：blocker verdict 优先取自这里，而不是从
        # probe 字符串间接推断。
        reranker_applied=gates["reranker"].get("applied"),
        reranker_degraded=gates["reranker"].get("degraded"),
        reranker_reason=gates["reranker"].get("reason"),
        reranker_provider_called=gates["reranker"].get("provider_called"),
        qdrant_total_points=gates["qdrant"]["total_points"],
        requested_experiments=(
            tuple(requested_experiments)
            if requested_experiments is not None
            else tuple(EXPERIMENT_SPECS)
        ),
    )
    gates.update(assessment)
    if gates["status"] == "BLOCKED":
        if gates["primary_blocker"] == "VECTOR_INDEX_EMPTY":
            gates["hint"] = "运行 python3 scripts/import_eval_corpus.py 先导入评测语料"
        else:
            gates["hint"] = (
                "修复 provider 认证（http_status 见 blockers），"
                "然后重新运行 make rag-eval-import 导入语料"
            )
    elif gates["status"] == "PARTIAL":
        gates["hint"] = "reranker 不可用：可运行 vector_only/bm25_only/hybrid_no_rerank"
    return gates


# ---------------------------------------------------------------------------
# Experiment overrides（仅本进程实例级，默认全关）
# ---------------------------------------------------------------------------


@dataclass
class Override:
    disable_hybrid: bool = False
    disable_embedding: bool = False


def apply_override(kb, override: Override):
    """进入实验状态：临时修改 KB 实例属性。返回恢复函数（finally 调用）。"""
    orig_hybrid = kb._hybrid_enabled
    orig_embed_fn = kb._embed_fn
    if override.disable_hybrid:
        kb._hybrid_enabled = False
    if override.disable_embedding:
        kb._embed_fn = None

    def restore():
        kb._hybrid_enabled = orig_hybrid
        kb._embed_fn = orig_embed_fn

    return restore


async def run_experiment(
    kb,
    name: str,
    override: Override,
    queries: list[dict[str, Any]],
    *,
    corpus_ids: set[str],
    top_k: int,
    ks: tuple[int, ...],
    timeout_s: float | None,
    warmup_n: int,
) -> dict[str, Any]:
    """执行单个实验配置的完整评测（含 warmup）。"""
    rerank_flag = EXPERIMENT_SPECS[name]["rerank"]
    restore = apply_override(kb, override)
    try:
        # ---- warmup（结果弃置）----
        warmup_ids: list[str] = []
        for q in queries[:warmup_n]:
            try:
                await kb.retrieve(
                    _build_request(q["query"], top_k, rerank_flag, timeout_s)
                )
                warmup_ids.append(q["query_id"])
            except Exception:
                pass  # warmup 失败不影响正式 run（记 ids 供审计）

        # ---- 正式 run ----
        rows: list[dict[str, Any]] = []
        # 分母行 = 成功行 + 失败查询的零指标行；all_queries 主口径据此计算。
        population_rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        degraded_count = 0
        channel_error_count = 0
        progress_interval = max(1, len(queries) // 20)
        t_all = time.monotonic()

        for idx, case in enumerate(queries, 1):
            qid = case["query_id"]
            expected = list(case["expected_doc_ids"])
            gold_flags = [gid in corpus_ids for gid in expected]
            t0 = time.perf_counter()
            exception_type: str | None = None
            error_detail = ""
            retrieved_ids: list[str] = []
            first_rank = 0
            trace_stages: dict[str, float] = {}
            stage_detail: dict[str, dict[str, Any]] = {}
            degraded_reason = ""
            try:
                result = await kb.retrieve(
                    _build_request(case["query"], top_k, rerank_flag, timeout_s)
                )
                total_ms = (time.perf_counter() - t0) * 1000.0
                retrieved_ids = [d.get("id", "") for d in result]
                meta = getattr(result, "meta", {}) or {}
                degraded_reason = meta.get("degraded_reason", "") or ""
                trace = getattr(result, "trace", None)
                if trace is not None:
                    for st in trace.to_dict()["stages"]:
                        trace_stages[st["name"]] = st["duration_ms"]
                        stage_detail[st["name"]] = {
                            "status": st["status"],
                            "candidate_out": st["candidate_out"],
                        }
                metrics = compute_query_metrics(retrieved_ids, expected, ks)
                first_rank = int(metrics["first_relevant_rank"])
                if meta.get("retrieval_degraded"):
                    degraded_count += 1
                row = {
                    "query_id": qid,
                    "category": case["category"],
                    "difficulty": case["difficulty"],
                    "query_type": case.get("query_type", ""),
                    "expected_ids": expected,
                    "retrieved_ids": retrieved_ids,
                    "total_ms": round(total_ms, 2),
                    "stages_ms": {k: round(v, 3) for k, v in trace_stages.items()},
                    "retrieval_degraded": bool(meta.get("retrieval_degraded")),
                    "degraded_reason": degraded_reason,
                    "vector_channel_used": bool(meta.get("vector_channel_used")),
                    "lexical_channel_used": bool(meta.get("lexical_channel_used")),
                    # execution facts per stage (status + candidate_out). Kept in
                    # the row so channel availability can be recomputed from the
                    # artifact without re-running the retrieval pipeline.
                    "channel_stages": stage_detail,
                    **population_flags(expected, corpus_ids),
                    **metrics,
                }
                rows.append(row)
                population_rows.append(row)
            except (asyncio.TimeoutError, TimeoutError) as e:
                total_ms = (time.perf_counter() - t0) * 1000.0
                exception_type = "timeout"
                error_detail = type(e).__name__
            except Exception as e:  # noqa: BLE001 - 评测必须记录任何失败并继续
                total_ms = (time.perf_counter() - t0) * 1000.0
                exception_type = type(e).__name__
                error_detail = str(e)[:200]

            if exception_type is not None:
                channel_error_count += 1
                label, diags = classify_failure(
                    gold_in_corpus_flags=gold_flags,
                    first_rank=0,
                    exception_type=exception_type,
                    degraded_reason="",
                    wall_ms=total_ms,
                    timeout_s=timeout_s,
                )
                failures.append(
                    {
                        "query_id": qid,
                        "category": case["category"],
                        "expected_ids": expected,
                        "failure_type": label,
                        "exception_type": exception_type,
                        "error_detail": error_detail,
                        "wall_ms": round(total_ms, 2),
                        "diagnostics": diags,
                    }
                )
                # 失败请求以零指标进入 all_queries 分母（end-to-end 主口径）。
                population_rows.append(
                    zero_metric_row(qid, expected, corpus_ids, ks)
                )
                continue

            # 成功行：产出失败分析（MISS_ALL / LOW_RANK / GOLD_NOT_INDEXED）
            label, diags = classify_failure(
                gold_in_corpus_flags=gold_flags,
                first_rank=first_rank,
                exception_type=None,
                degraded_reason=degraded_reason,
                wall_ms=total_ms,
                timeout_s=timeout_s,
            )
            if label in ("MISS_ALL", "LOW_RANK", "GOLD_NOT_INDEXED"):
                vec = stage_detail.get("VECTOR", {})
                bm25 = stage_detail.get("BM25", {})
                if vec.get("status") != "executed" or vec.get("candidate_out", 0) == 0:
                    diags.append("VECTOR_MISS")
                if bm25.get("status") != "executed" or bm25.get("candidate_out", 0) == 0:
                    diags.append("BM25_MISS")
                rer = stage_detail.get("RERANK", {})
                if rer.get("status") == "degraded":
                    diags.append("RERANK_DEGRADED")
                fusion = stage_detail.get("FUSION_RRF", {})
                if fusion.get("status") == "degraded":
                    diags.append("FUSION_DEGRADED")
                failures.append(
                    {
                        "query_id": qid,
                        "category": case["category"],
                        "expected_ids": expected,
                        "retrieved_ids": retrieved_ids[:top_k],
                        "rank_of_gold": first_rank,
                        "failure_type": label,
                        "exception_type": None,
                        "wall_ms": round(total_ms, 2),
                        "diagnostics": diags,
                    }
                )

            if idx % progress_interval == 0 or idx == len(queries):
                pct = idx / len(queries) * 100
                sys.stdout.write(
                    f"\r  [{name}] [{('#' * int(pct // 5)).ljust(20)}] "
                    f"{idx}/{len(queries)} ({pct:.0f}%)"
                )
                sys.stdout.flush()

        print()
        elapsed = time.monotonic() - t_all
        return {
            "experiment": name,
            "n_total": len(queries),
            "n_success": len(rows),
            "n_failed": len(failures),
            "n_error": channel_error_count,
            "n_degraded": degraded_count,
            "wall_seconds": round(elapsed, 1),
            "warmup": {"count": warmup_n, "query_ids": warmup_ids, "results_discarded": True},
            "metrics": summarize_metric_rows(rows, ks),
            "population_metrics": population_metrics(population_rows, ks),
            "latency": aggregate_latency([r["total_ms"] for r in rows]),
            "stage_latency": {
                stage: aggregate_latency([r["stages_ms"][stage] for r in rows if stage in r["stages_ms"]])
                for stage in ("VECTOR", "BM25", "FUSION_RRF", "RERANK")
            },
            "category_metrics": category_metrics(rows, ks),
            "failure_counts": _count_by(failures, "failure_type"),
            "rows": rows,
            "failures": failures,
        }
    finally:
        restore()


def _build_request(query: str, top_k: int, rerank: bool, timeout_s: float | None):
    from rag.retrieval_contract import RetrievalRequest

    return RetrievalRequest(
        query=query,
        collections=list(EVAL_COLLECTIONS),
        top_k=top_k,
        rewrite=True,
        rerank=rerank,
        retrieval_timeout=timeout_s,
    )


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r[key])] = out.get(str(r[key]), 0) + 1
    return out


# ---------------------------------------------------------------------------
# Evidence validity observations（issue #45）
# ---------------------------------------------------------------------------
#
# 只记录「跑了什么」，绝不记录「跑得怎么样」：指标大小不是 validity 的输入
# （合法的差模型也必须能产出 VERIFIED）。因此这里采集的是通道执行事实、
# 请求错误计数与语料覆盖计数。

#: retrieval result.meta 的通道使用标记 -> 通道
_CHANNEL_META_USED_KEY = {"vector": "vector_channel_used", "bm25": "lexical_channel_used"}
#: 通道 -> retrieval trace 的 stage 名
_CHANNEL_STAGE_NAME = {"vector": "VECTOR", "bm25": "BM25", "rerank": "RERANK"}
_STAGE_EXECUTED = "executed"


def collect_channel_observations(
    rows: list[dict[str, Any]], required: tuple[str, ...]
) -> dict[str, Any]:
    """单个 ablation leg 的通道执行事实（每个通道都记录，含未 required 的）。

    ``required`` 来自 ``eval_contract.required_channels``（由该 leg 的 override
    推导），leg 无法自行声明更弱的要求。rerank 没有 meta 标记，以 RERANK stage
    是否 ``executed`` 作为「真的生效」的事实来源（而不是从返回条数推断）。
    """
    out: dict[str, Any] = {}
    for channel in CHANNELS:
        used_key = _CHANNEL_META_USED_KEY.get(channel)
        stage_name = _CHANNEL_STAGE_NAME[channel]
        used_count = 0
        executed_count = 0
        candidate_total = 0
        for row in rows:
            if used_key is not None and bool(row.get(used_key)):
                used_count += 1
            stage_fact = (row.get("channel_stages") or {}).get(stage_name)
            if not isinstance(stage_fact, dict):
                continue
            if str(stage_fact.get("status") or "") == _STAGE_EXECUTED:
                executed_count += 1
                if used_key is None:
                    used_count += 1
            candidates = stage_fact.get("candidate_out")
            if isinstance(candidates, int) and not isinstance(candidates, bool):
                candidate_total += max(candidates, 0)
        out[channel] = {
            "required": channel in required,
            "used_count": used_count,
            "executed_count": executed_count,
            "candidate_total": candidate_total,
        }
    return out


def collect_evidence_validity_observations(
    results: dict[str, dict[str, Any]],
    *,
    subset_run: bool,
    declared_queries: int,
    executed_queries: int,
    population_counts: dict[str, int],
    preflight_status: str,
) -> dict[str, Any]:
    """汇总 validity predicate 的输入（可被 artifact 逐字复核）。"""
    return {
        "subset_run": subset_run,
        "declared_queries": declared_queries,
        "executed_queries": executed_queries,
        "preflight_status": preflight_status,
        "population_counts": dict(population_counts),
        "experiments": {
            name: {
                "n_total": res["n_total"],
                "n_success": res["n_success"],
                "n_error": res["n_error"],
                "n_degraded": res["n_degraded"],
                "failure_counts": dict(res["failure_counts"]),
                "channels": collect_channel_observations(res["rows"], required_channels(name)),
            }
            for name, res in results.items()
        },
    }


# ---------------------------------------------------------------------------
# Ablation 对比
# ---------------------------------------------------------------------------


def metric_delta(base: dict[str, float], target: dict[str, float]) -> dict[str, float]:
    keys = sorted(set(base) & set(target))
    return {k: round(target[k] - base[k], 4) for k in keys}


def ablation_analysis(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "hybrid_no_rerank" in results and "vector_only" in results:
        out["hybrid_vs_vector"] = metric_delta(
            results["vector_only"]["metrics"], results["hybrid_no_rerank"]["metrics"]
        )
    if "hybrid_no_rerank" in results and "bm25_only" in results:
        out["hybrid_vs_bm25"] = metric_delta(
            results["bm25_only"]["metrics"], results["hybrid_no_rerank"]["metrics"]
        )
    if "hybrid_rerank" in results and "hybrid_no_rerank" in results:
        base_rows = {r["query_id"]: r for r in results["hybrid_no_rerank"]["rows"]}
        target_rows = {r["query_id"]: r for r in results["hybrid_rerank"]["rows"]}
        improved, degraded_, unchanged = [], [], []
        for qid, base_row in base_rows.items():
            tgt = target_rows.get(qid)
            if tgt is None:
                continue
            d = reciprocal_rank(tgt["first_relevant_rank"]) - reciprocal_rank(
                base_row["first_relevant_rank"]
            )
            if d > 0:
                improved.append(qid)
            elif d < 0:
                degraded_.append(qid)
            else:
                unchanged.append(qid)
        out["reranker_uplift"] = {
            "metrics_delta": metric_delta(
                results["hybrid_no_rerank"]["metrics"], results["hybrid_rerank"]["metrics"]
            ),
            "latency_p95_delta_ms": round(
                results["hybrid_rerank"]["latency"]["p95_ms"]
                - results["hybrid_no_rerank"]["latency"]["p95_ms"], 2
            ),
            "improved_queries": len(improved),
            "degraded_queries": len(degraded_),
            "unchanged_queries": len(unchanged),
            # comparative tags（不是 error 分类；用于回答 reranker 值不值）
            "tags": {
                "RERANK_IMPROVED": improved,
                "RERANK_DEGRADED": degraded_,
            },
            "improved_query_ids_sample": improved[:20],
            "degraded_query_ids_sample": degraded_[:20],
        }
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def derive_run_status(
    *, full_run: bool, subset_run: bool, reranker_ok: bool, validity: dict[str, Any]
) -> str:
    """Run status label (issue #45)。

    VERIFIED_FULL 只在「全部 declared query 跑完」**且**「evidence validity 判定为
    VALID」**且** reranker gate 健康时产生。任一条件缺失都不自证：诊断 artifact
    仍然产出，状态降级为 NOT_VERIFIED / SUBSET_SMOKE / PARTIAL。
    """
    if full_run and validity["verdict"] == VERDICT_VALID:
        return "VERIFIED_FULL" if reranker_ok else "VERIFIED_FULL_NO_RERANK"
    if full_run:
        return "NOT_VERIFIED"
    if subset_run:
        return "SUBSET_SMOKE"
    return "PARTIAL"


async def evaluate(args: argparse.Namespace) -> int:
    from core.config import QDRANT_HOST, QDRANT_PORT
    from rag.qdrant_knowledge_base import QdrantKnowledgeBase

    run_id = args.run_id or (
        "rag649-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir = Path(args.output) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 76)
    print("  RAG 检索质量评估（evidence pipeline）")
    print(f"  run_id={run_id}")
    print("=" * 76)

    # 1. benchmark 完整性
    benchmark = load_benchmark(Path(args.benchmark))
    meta = benchmark["metadata"]
    queries = benchmark["queries"]
    benchmark_sha = _sha256_file(Path(args.benchmark))
    print(f"  benchmark: {meta['total_queries']} queries (sha256={benchmark_sha[:16]}…)")

    subset_run = args.limit is not None and args.limit < len(queries)
    if subset_run:
        queries = queries[: args.limit]
        print(f"  [SMOKE] --limit={args.limit}: subset_run=true，结果不可作为正式证据")

    # 2. 初始化 KB + preflight
    kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT)
    if not kb.available:
        print("  [FATAL] Qdrant 不可达")
        return 1
    want_rerank = "hybrid_rerank" in args.experiments
    gates = preflight(
        kb, rerank_probe=want_rerank, requested_experiments=tuple(args.experiments)
    )
    print(f"  preflight: {gates['status']} | points={gates.get('qdrant', {}).get('total_points')}")
    if gates["status"].startswith("BLOCKED"):
        print(f"  [FATAL] {gates.get('hint', '')}")
        _write_gate_report(out_dir, run_id, benchmark_sha, len(queries), gates, meta, args)
        return 1

    reranker_ok = gates["reranker"].get("probe") == "ok"
    # 实验可运行性由 blockers 的 blocks_experiments 直接推导（机器可读，
    # 不再手工维护 if/elif 链；reranker 失败只会阻塞 hybrid_rerank）
    blocked_by: dict[str, list[str]] = {}
    for blocker in gates.get("blockers", []):
        for exp in blocker.get("blocks_experiments", []):
            blocked_by.setdefault(exp, []).append(blocker["code"])
    experiments = [e for e in args.experiments if e not in blocked_by]
    status_notes: list[str] = [
        f"{e} 未运行：被 {'/'.join(codes)} 阻塞"
        for e, codes in blocked_by.items()
        if e in args.experiments
    ]
    if not experiments:
        print("  [FATAL] 无可运行实验（所有请求实验均被 preflight blocker 阻塞）")
        _write_gate_report(out_dir, run_id, benchmark_sha, len(queries), gates, meta, args)
        return 1
    for note in status_notes:
        print(f"  [NOTE] {note}")

    # 3. gold corpus 覆盖（GOLD_NOT_INDEXED 记账依据）
    corpus_ids = await _collect_corpus_ids(kb)

    # 4. 逐实验执行
    results: dict[str, dict[str, Any]] = {}
    for name in experiments:
        spec = EXPERIMENT_SPECS[name]
        print(f"[experiment] {name}（vector={'on' if not spec['disable_embedding'] else 'off'} "
              f"bm25={'on' if not spec['disable_hybrid'] else 'off'} rerank={spec['rerank']}）")
        results[name] = await run_experiment(
            kb,
            name,
            Override(spec["disable_hybrid"], spec["disable_embedding"]),
            queries,
            corpus_ids=corpus_ids,
            top_k=args.top_k,
            ks=tuple(args.ks),
            timeout_s=args.timeout,
            warmup_n=args.warmup,
        )
        m = results[name]["metrics"]
        print(f"  -> n_success={results[name]['n_success']} "
              f"MRR={m.get('mrr')} hit@3={m.get('hit@3')} "
              f"P95={results[name]['latency']['p95_ms']}ms")

    # 5. 组装 artifact
    rows_payload = {
        name: [
            {k: r[k] for k in r if k not in ("expected_ids", "retrieved_ids")}
            for r in res["rows"]
        ]
        for name, res in results.items()
    }
    raw_path = out_dir / "raw_results.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(rows_payload, f, ensure_ascii=False)

    full_run = (not subset_run) and all(
        results[name]["n_success"] == len(benchmark["queries"]) for name in results
    )
    # Evidence validity（issue #45）：VERIFIED_FULL 只在「evidence 本身有效」时
    # 产生。100% degraded / GOLD_NOT_INDEXED 占绝大多数 / required channel 不可用 /
    # corpus coverage 不足 —— 任一命中都不得自证为正式证据，但诊断 artifact 照常产出。
    pop_counts = population_counts(queries, corpus_ids)
    validity = assess_evidence_validity(
        collect_evidence_validity_observations(
            results,
            subset_run=subset_run,
            declared_queries=meta["total_queries"],
            executed_queries=len(queries),
            population_counts=pop_counts,
            preflight_status=gates["status"],
        )
    )
    evidence_valid = validity["verdict"] == VERDICT_VALID
    status = derive_run_status(
        full_run=full_run,
        subset_run=subset_run,
        reranker_ok=reranker_ok,
        validity=validity,
    )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "git_sha": git_sha(),
        "benchmark": {
            "path": str(Path(args.benchmark).relative_to(PROJECT_ROOT)),
            "sha256": benchmark_sha,
            "declared_queries": meta["total_queries"],
            "actual_queries": len(benchmark["queries"]),
            "executed_queries": len(queries),
            "metadata_version": meta.get("version"),
            "created_at": meta.get("created_at"),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "vector_db_mode": collect_config()["vector_db_mode"],
            "qdrant": {
                "host": QDRANT_HOST,
                "port": QDRANT_PORT,
                "collections": gates["qdrant"]["collections"],
                "total_points": gates["qdrant"]["total_points"],
            },
            "embedding_model": kb._embedding_model_name,
            "embedding_dim": gates["embedding"].get("dim"),
            "reranker_model": gates["reranker"].get("model"),
            "reranker_available": reranker_ok,
            "hybrid_search_enabled": collect_config()["hybrid_search_enabled"],
        },
        "config": collect_config(),
        "preflight": gates,
        "evaluation_protocol": {
            "top_k": args.top_k,
            "ks": list(args.ks),
            "warmup_per_experiment": args.warmup,
            "cache_policy": "no application-level retrieval cache involved; "
                            "embedding/reranker HTTP keepalive warmed by warmup queries",
            "denominator": "primary all_queries view is end-to-end: failed "
                           "(exception/timeout/gold-not-indexed) requests enter the "
                           "population with zero-valued metrics; diagnostic "
                           "metrics/category views average over successful "
                           "queries (n_success); latency only over completed requests",
            "relevance": "binary (any of expected_doc_ids in top-k)",
            "mrr_truncation": f"MRR computed on top-{args.top_k} retrieved list",
        },
        # 三套 population 口径：主口径固定 all_queries（端到端）；
        # 其余两视图只作诊断并列输出，禁止挑选最好看的一组单独宣称
        "evaluation_populations": {
            "definitions": POPULATION_DEFINITIONS,
            "primary_view": "all_queries",
            "counts": pop_counts,
            "per_experiment": {
                name: res["population_metrics"] for name, res in results.items()
            },
        },
        "metrics": {name: res["metrics"] for name, res in results.items()},
        "latency": {name: res["latency"] for name, res in results.items()},
        "stage_latency": {name: res["stage_latency"] for name, res in results.items()},
        "category_metrics": {name: res["category_metrics"] for name, res in results.items()},
        "ablation": ablation_analysis(results),
        "run_summary": {
            name: {
                "n_total": res["n_total"],
                "n_success": res["n_success"],
                "n_failed": res["n_failed"],
                "n_error": res["n_error"],
                "n_degraded": res["n_degraded"],
                "failure_counts": res["failure_counts"],
                "wall_seconds": res["wall_seconds"],
            }
            for name, res in results.items()
        },
        "failure_counts": {
            name: res["failure_counts"] for name, res in results.items()
        },
        "failures_top": {
            name: res["failures"][:50] for name, res in results.items()
        },
        "notes": [
            "warmup queries are discarded from formal statistics",
            "ablation overrides are instance/request-level inside this process; "
            "production defaults (HYBRID_SEARCH_ENABLED etc.) are untouched",
            "primary metric view is evaluation_populations.all_queries (end-to-end); "
            "retrieval_eligible / full_gold_covered are diagnostic views and must "
            "always be reported alongside, never as a standalone replacement",
            "population counts are computed at runtime from the indexed corpus; "
            "no hardcoded denominators",
            "raw_results.json is a local artifact; its sha256 is recorded below",
            "evidence_validity is a fail-closed gate on self-certification: "
            "status=VERIFIED_FULL requires verdict=VALID, and "
            "scripts/rag_evidence_status.py recomputes the verdict from "
            "evidence_validity.observed instead of trusting this label",
            "VERIFIED describes the validity of the evidence, not the exit status "
            "of this process; a valid run may still show poor retrieval metrics",
        ] + status_notes,
        "evidence_validity": validity,
        "raw_results_sha256": _sha256_file(raw_path),
        "subset_run": subset_run,
    }

    report_path = out_dir / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    failures_payload = {name: res["failures"] for name, res in results.items()}
    failures_path = out_dir / "failures.json"
    with open(failures_path, "w", encoding="utf-8") as f:
        json.dump(failures_payload, f, ensure_ascii=False, indent=2)

    # raw_results.json 保持本地（体积大）；run 目录 .gitignore 排除它
    gi = out_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("raw_results.json\n", encoding="utf-8")

    print()
    print("=" * 76)
    print(f"  status: {status}")
    print(f"  evidence_validity: {validity['verdict']} ({validity['contract']})")
    for reason in validity["reasons"]:
        print(f"    - {reason['code']} [{reason['group']}] {reason['scope']}: "
              f"{reason['detail']}")
    print(f"  report:   {report_path}")
    print(f"  failures: {failures_path}")
    print(f"  raw (local, sha256 recorded): {raw_path}")
    print("=" * 76)
    if not subset_run and not evidence_valid:
        # fail closed：正式全量 run 无法自证时不能以 0 退出，否则 CI 会把
        # NOT_VERIFIED 的 artifact 当成通过。诊断 artifact 仍然已写出。
        print("  [FAIL] 正式评测证据无效：状态保持 NOT_VERIFIED，不得作为正式指标引用")
        return 1
    return 0


async def _collect_corpus_ids(kb) -> set[str]:
    """从 4 个 collection scroll 收集全部 doc_id（GOLD_NOT_INDEXED 判定依据）。"""
    ids: set[str] = set()
    for coll in EVAL_COLLECTIONS:
        offset = None
        while True:
            result = kb._client.scroll(
                collection_name=coll, limit=1000, offset=offset,
                with_payload=True, with_vectors=False,
            )
            points = list(result[0]) if isinstance(result, tuple) else list(getattr(result, "points", []) or [])
            offset = result[1] if isinstance(result, tuple) else getattr(result, "next_page_offset", None)
            for p in points:
                payload = getattr(p, "payload", None) or {}
                doc_id = payload.get("doc_id")
                if doc_id:
                    ids.add(doc_id)
            if offset is None:
                break
    return ids


def _write_gate_report(
    out_dir: Path,
    run_id: str,
    benchmark_sha: str,
    n: int,
    gates: dict,
    meta: dict,
    args: argparse.Namespace | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # v2 blocker semantics: primary cause != downstream symptom
        "status": gates.get("status", "BLOCKED"),
        "primary_blocker": gates.get("primary_blocker"),
        "blockers": gates.get("blockers", []),
        "git_sha": git_sha(),
        "benchmark": {
            "sha256": benchmark_sha,
            "declared_queries": meta.get("total_queries"),
            "executed_queries": n,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "config": collect_config() if args is not None else {},
        "preflight": gates,
        "notes": [
            "formal evaluation not run; no metrics generated",
            "credential material is never recorded; only availability/probe outcome "
            "and provider HTTP status codes",
            "blocker semantics v2: primary_blocker is the root cause; downstream "
            "symptoms (e.g. VECTOR_INDEX_EMPTY) carry caused_by links to it",
        ],
    }
    with open(out_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  blocked-evidence artifact: {out_dir / 'report.json'}")


# ---------------------------------------------------------------------------
# 兼容入口（历史签名：无 CLI 时等价于默认全量评测）
# ---------------------------------------------------------------------------


def compute_metrics(
    retrieved: list[dict[str, Any]],
    expected_ids: list[str],
    k: int = 3,
) -> dict[str, float]:
    """历史兼容：单 K=3 指标（v6.1 行为保留）。"""
    ids = [d.get("id", "") for d in retrieved]
    m = compute_query_metrics(ids, expected_ids, ks=(k,))
    return {
        "recall_at_k": m[f"recall@{k}"],
        "precision_at_k": m[f"precision@{k}"],
        "mrr": m["mrr"],
        "ndcg_at_k": m[f"ndcg@{k}"],
        "hit": m[f"hit@{k}"] > 0,
        "first_relevant_rank": int(m["first_relevant_rank"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG 检索质量评估（evidence pipeline）")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--experiments",
        default=",".join(EXPERIMENT_SPECS),
        help=f"逗号分隔，可选: {','.join(EXPERIMENT_SPECS)}",
    )
    parser.add_argument("--top-k", type=int, default=8, help="检索 top-K（metrics 在 --ks 上计算）")
    parser.add_argument("--ks", default="1,3,5,8", help="逗号分隔的 K 值")
    parser.add_argument("--warmup", type=int, default=8, help="每实验 warmup 查询数（结果弃置）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟，subset_run=true）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单查询检索超时（秒）")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--preflight-only", action="store_true", help="只跑 preflight gate")
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    args.ks = tuple(int(k) for k in str(args.ks).split(",") if k.strip())
    args.experiments = [
        e.strip() for e in str(args.experiments).split(",") if e.strip()
    ]
    for e in args.experiments:
        if e not in EXPERIMENT_SPECS:
            print(f"[ERROR] 未知实验: {e}（可选: {','.join(EXPERIMENT_SPECS)}）")
            return 2

    if args.preflight_only:
        from core.config import QDRANT_HOST, QDRANT_PORT
        from rag.qdrant_knowledge_base import QdrantKnowledgeBase

        kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT)
        if not kb.available:
            print("[FATAL] Qdrant 不可达")
            return 1
        gates = preflight(
            kb, rerank_probe=True, requested_experiments=tuple(args.experiments)
        )
        print(json.dumps(gates, ensure_ascii=False, indent=2))
        run_id = args.run_id or (
            "preflight-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        out_dir = Path(args.output) / run_id
        benchmark = load_benchmark(Path(args.benchmark))
        _write_gate_report(
            out_dir, run_id, _sha256_file(Path(args.benchmark)),
            len(benchmark["queries"]), gates, benchmark["metadata"], args,
        )
        return 0 if gates["status"] in ("OK", "PARTIAL") else 1

    return await evaluate(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
