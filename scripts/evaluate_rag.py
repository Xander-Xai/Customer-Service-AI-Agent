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
- 失败记账：exception/timeout/degraded 全部记录，failure taxonomy 规则化分类
- provenance：git SHA + benchmark sha256 + runtime config + 模型名 全部自动采集

指标分母定义（写进 artifact notes）：
- Hit/Recall/Precision/NDCG/MRR 均在「成功执行检索」的查询上取均值（n_success）
- exception / timeout 计入 failures（不进指标分母），计数在 failures_summary
- degraded（如向量通道超时降级为词法）查询计入指标，但按 degraded_reason 单独计数

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

DEFAULT_BENCHMARK = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "evaluation" / "rag-649"

EVAL_COLLECTIONS = [
    "product_knowledge",
    "faq",
    "tech_support",
    "complaint_knowledge",
]

DEFAULT_KS = (1, 3, 5, 8)
REPORT_SCHEMA_VERSION = "rag-eval-evidence/v1"

EXPERIMENT_SPECS: dict[str, dict[str, Any]] = {
    # vector_only: 仅向量通道（禁用 BM25），无 rerank
    "vector_only": {"disable_hybrid": True, "disable_embedding": False, "rerank": False},
    # bm25_only: 仅词法通道（embedding 置空 -> 向量通道 fail-closed 禁用），无 rerank
    "bm25_only": {"disable_hybrid": False, "disable_embedding": True, "rerank": False},
    # hybrid_no_rerank: 生产混合检索（vector+BM25+RRF），无 rerank
    "hybrid_no_rerank": {"disable_hybrid": False, "disable_embedding": False, "rerank": False},
    # hybrid_rerank: 生产混合检索 + ApiReranker（production-like）
    "hybrid_rerank": {"disable_hybrid": False, "disable_embedding": False, "rerank": True},
}

FAILURE_TAXONOMY = (
    "TIMEOUT",
    "PROVIDER_ERROR",
    "GOLD_NOT_INDEXED",
    "MISS_ALL",
    "LOW_RANK",
)


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


def preflight(kb, *, rerank_probe: bool) -> dict[str, Any]:
    """provider/index gate。所有 gate 都执行并记录（阻塞原因要可审计）。"""

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

    # 2. Embedding 探针（无论索引是否为空都探测，记录真实凭据状态）
    embedding_gate: dict[str, Any] = {"available": kb.embedding_available}
    if kb.embedding_available:
        try:
            vec = kb._embed_texts(["评测探针：透明质酸功效"])[0]
            embedding_gate.update(
                {"probe": "ok", "dim": len(vec), "model": kb._embedding_model_name}
            )
        except Exception as e:
            embedding_gate.update(
                {"probe": "failed", "error": type(e).__name__, "detail": str(e)[:200]}
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

    # 4. Reranker 探针（必须验证 rerank 真实发生：ApiReranker 在 API 失败时
    # 静默回退到原始顺序，仅凭「有返回值」会得到假阳性）
    reranker_gate: dict[str, Any] = {}
    try:
        from rag.reranker import create_reranker

        rr = create_reranker()
        reranker_gate["available"] = rr.available
        reranker_gate["model"] = rr._model
        if rr.available and rerank_probe:
            probed = rr.rerank(
                "透明质酸功效",
                [
                    {"id": "a", "content": "透明质酸保湿"},
                    {"id": "b", "content": "防晒指数"},
                ],
                top_k=2,
            )
            real_rerank = any("rerank_score" in r for r in probed)
            reranker_gate["probe"] = "ok" if real_rerank else "silent_fallback"
            if not real_rerank:
                reranker_gate["detail"] = (
                    "rerank API 调用失败或未附加 rerank_score（ApiReranker 静默回退）"
                )
    except Exception as e:
        reranker_gate.update({"available": False, "probe": "failed", "error": type(e).__name__})
    gates["reranker"] = reranker_gate

    # 5. 复合判定（记录到证据，不掩盖任何一项）
    if gates["qdrant"]["total_points"] == 0:
        gates["status"] = "BLOCKED_VECTOR_INDEX"
        gates["hint"] = "运行 python3 scripts/import_eval_corpus.py 先导入评测语料"
    elif embedding_gate.get("probe") != "ok":
        gates["status"] = "BLOCKED_PROVIDER_AUTH"
        gates["hint"] = (
            "embedding provider 认证失败（详见 environment.embedding）；"
            "导入与向量/重排实验均无法进行"
        )
    elif reranker_gate.get("probe") not in ("ok",) and rerank_probe:
        gates["status"] = "PARTIAL"
        gates["hint"] = "reranker 不可用：可运行 vector_only/bm25_only/hybrid_no_rerank"
    else:
        gates["status"] = "OK"
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
                rows.append(
                    {
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
                        **metrics,
                    }
                )
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
            "n_degraded": degraded_count,
            "wall_seconds": round(elapsed, 1),
            "warmup": {"count": warmup_n, "query_ids": warmup_ids, "results_discarded": True},
            "metrics": summarize_metric_rows(rows, ks),
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
            d = tgt["first_relevant_rank"] - base_row["first_relevant_rank"]
            if d < 0:
                improved.append(qid)
            elif d > 0:
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
            "improved_query_ids_sample": improved[:20],
            "degraded_query_ids_sample": degraded_[:20],
        }
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


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
    gates = preflight(kb, rerank_probe=want_rerank)
    print(f"  preflight: {gates['status']} | points={gates.get('qdrant', {}).get('total_points')}")
    if gates["status"].startswith("BLOCKED"):
        print(f"  [FATAL] {gates.get('hint', '')}")
        _write_gate_report(out_dir, run_id, benchmark_sha, len(queries), gates, meta, args)
        return 1

    embedding_ok = gates["embedding"].get("probe") == "ok"
    reranker_ok = gates["reranker"].get("available") is True and gates["reranker"].get("probe") == "ok"
    experiments = list(args.experiments)
    status_notes: list[str] = []
    if not embedding_ok:
        experiments = [e for e in experiments if e == "bm25_only"]
        status_notes.append("embedding 探针失败：仅保留 bm25_only")
    if not reranker_ok and "hybrid_rerank" in experiments:
        experiments.remove("hybrid_rerank")
        status_notes.append("reranker 不可用：hybrid_rerank 未运行（PARTIAL）")
    if not experiments:
        print("  [FATAL] 无可运行实验（embedding + reranker 均不可用）")
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
    status = (
        "VERIFIED_FULL" if full_run and reranker_ok
        else "VERIFIED_FULL_NO_RERANK" if full_run
        else "SUBSET_SMOKE" if subset_run
        else "PARTIAL"
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
            "denominator": "metrics averaged over successfully executed queries "
                           "(n_success); exceptions/timeouts recorded in failures",
            "relevance": "binary (any of expected_doc_ids in top-k)",
            "mrr_truncation": f"MRR computed on top-{args.top_k} retrieved list",
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
            "raw_results.json is a local artifact; its sha256 is recorded below",
        ] + status_notes,
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
    print(f"  report:   {report_path}")
    print(f"  failures: {failures_path}")
    print(f"  raw (local, sha256 recorded): {raw_path}")
    print("=" * 76)
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
        "status": gates.get("status", "BLOCKED"),
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
            "credential material is never recorded; only availability/probe outcome",
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
        gates = preflight(kb, rerank_probe=True)
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
