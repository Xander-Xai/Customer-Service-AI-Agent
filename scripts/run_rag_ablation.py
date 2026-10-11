#!/usr/bin/env python3
"""RAG 四组配置消融 —— 只对**可测**的配置产出数字，其余显式 BLOCKED。

设计原则（与 ``scripts/evaluate_rag.py`` 同源，不另立第二套事实）
----------------------------------------------------------------
* 复用 ``evaluate_rag.compute_query_metrics`` / ``summarize_metric_rows`` /
  ``classify_failure`` —— 指标口径只有一份。
* 复用**生产检索代码** ``rag/qdrant_knowledge_base.py`` 的 BM25 通道：
  把语料灌进本地 Qdrant（载荷 = doc_id + content，向量为占位），
  再调 ``rebuild_bm25_from_qdrant`` 建索引、``_bm25_search`` 检索。
  不是在评测脚本里另写一个 BM25 —— 那样测的是评测脚本，不是被测系统。
* 每组配置**要么给出真实数字，要么给出结构化 BLOCKED 原因**。绝不估算。

四组配置与服务要求
------------------
===================  ==========================  ================================
配置                 需要什么                     本环境状态
===================  ==========================  ================================
``bm25_only``        Qdrant（载荷，无需向量）     可测
``vector_only``      embedding provider（HTTP）   **BLOCKED**（provider 无可用凭据）
``hybrid_no_rerank`` 上面两者                    **BLOCKED**
``hybrid_rerank``    上面两者 + reranker          **BLOCKED**
===================  ==========================  ================================

为什么向量通道不能"用占位向量凑合"
----------------------------------
占位向量能让 Qdrant 返回**任意**结果，于是 vector_only 的 Hit@K 会变成
"随机召回率"——一个看起来是数字、实际毫无意义的指标。这正是本任务要求
避免的"估算结果"。因此向量通道一律 BLOCKED。

用法::

    python3 scripts/run_rag_ablation.py --limit 300
    python3 scripts/run_rag_ablation.py --configs bm25_only vector_only
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gold_label_contract import (  # noqa: E402
    LABEL_STATUS_CONSTRUCTED,
    corpus_sha256,
    load_jsonl,
)

DEFAULT_CORPUS = REPO_ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
DEFAULT_GOLD = REPO_ROOT / "tests" / "eval" / "gold_labels" / "known_item_gold.jsonl"
EVAL_COLLECTION = "known_item_eval"
DEFAULT_KS = (1, 3, 5, 8)

ALL_CONFIGS = ("bm25_only", "vector_only", "hybrid_no_rerank", "hybrid_rerank")


def _git_sha() -> str | None:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).commit_sha


def _code_provenance() -> dict:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).to_dict()


def _load_corpus_texts(path: Path) -> tuple[list[dict], str]:
    docs = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                docs.append(json.loads(line))
    return docs, corpus_sha256(path)


def _ingest_corpus(host: str, port: int, docs: list[dict], dim: int) -> int:
    """把语料灌进本地 Qdrant：载荷带 doc_id/content，向量用零向量占位。

    向量用零**只影响向量通道**；BM25 通道 ``_scroll_collection_for_bm25``
    只读 ``payload.doc_id`` / ``payload.content``（``with_vectors=False``），
    因此这条路是生产代码的真实路径，不是模拟。
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = QdrantClient(host=host, port=port, timeout=60.0, check_compatibility=False)
    if client.collection_exists(EVAL_COLLECTION):
        client.delete_collection(EVAL_COLLECTION)
    client.create_collection(
        collection_name=EVAL_COLLECTION,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    zero = [0.0] * dim
    batch: list[PointStruct] = []
    written = 0
    for index, doc in enumerate(docs):
        batch.append(
            PointStruct(
                id=index,
                vector=zero,
                payload={
                    "doc_id": doc["id"],
                    "content": doc.get("content") or "",
                    "title": doc.get("title") or "",
                    "category": doc.get("category") or "",
                },
            )
        )
        if len(batch) >= 500:
            client.upsert(EVAL_COLLECTION, points=batch)
            written += len(batch)
            batch = []
    if batch:
        client.upsert(EVAL_COLLECTION, points=batch)
        written += len(batch)
    return written


def _bm25_retrieve(host: str, port: int, queries: list[dict], top_k: int) -> dict[str, list[str]]:
    """用**生产 BM25 通道**检索，返回 query_id -> retrieved doc_ids。"""
    import core.config as config

    # BM25 服务集合在配置里是硬编码的四个集合名；评测只灌了一个集合，
    # 因此这里把检索目标收敛到它。这是**评测入口**的配置，不是改被测逻辑。
    config.BM25_COLLECTIONS = [EVAL_COLLECTION]

    from rag.qdrant_knowledge_base import QdrantKnowledgeBase

    kb = QdrantKnowledgeBase(host=host, port=port, embedding_model=_NullEmbedding())
    kb._bm25_collections = [EVAL_COLLECTION] if hasattr(kb, "_bm25_collections") else None

    # 语料直接来自 Qdrant 载荷 -> 用生产方法重建索引。
    rebuilt = kb.rebuild_bm25_from_qdrant([EVAL_COLLECTION])
    status = getattr(rebuilt, "status", rebuilt)
    readiness = kb.bm25_readiness()
    if str(getattr(readiness, "value", readiness)) not in ("ready", "READY"):
        raise RuntimeError(f"BM25 index not ready after rebuild: {readiness!r} ({status!r})")

    results: dict[str, list[str]] = {}
    for query in queries:
        hits = kb._bm25_search(query["query"], [EVAL_COLLECTION], top_k=top_k)
        ids = []
        for hit in hits:
            meta = hit.get("metadata") or {}
            doc_id = meta.get("doc_id") or hit.get("id")
            if doc_id:
                ids.append(str(doc_id))
        results[query["query_id"]] = ids
    return results


class _NullEmbedding:
    """占位 embedding：**不允许**被真正调用。

    向量通道在本环境是 BLOCKED 的（provider 无凭据）。这个对象的存在只是为了让
    ``QdrantKnowledgeBase.__init__`` 不去构造 HTTP embedding 客户端从而出网；
    一旦有任何代码路径真的走到 ``encode``，立刻抛错暴露出来 —— 静默返回
    占位向量等于伪造向量检索结果。
    """

    model = "NOT_AVAILABLE"

    def encode(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        raise RuntimeError(
            "NullEmbedding.encode called: the vector channel must stay BLOCKED, "
            "not silently produce placeholder vectors"
        )

    async def aencode(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError("NullEmbedding.aencode called")


def _blocked(reason_code: str, detail: str, blocks: list[str]) -> dict:
    return {
        "status": "BLOCKED",
        "blocker_code": reason_code,
        "detail": detail,
        "blocks_configs": blocks,
        "metrics": None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--configs", nargs="*", default=list(ALL_CONFIGS))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=max(DEFAULT_KS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="把 gold 打乱后重跑一次：真实指标必须显著下降，否则说明指标本身没有区分力",
    )
    args = parser.parse_args(argv)

    from evaluate_rag import compute_query_metrics, summarize_metric_rows

    corpus_path, gold_path = Path(args.corpus), Path(args.gold)
    if not gold_path.exists():
        print(f"❌ gold 不存在：{gold_path}（先跑 make rag-gold-known-item）", file=sys.stderr)
        return 2

    docs, corpus_hash = _load_corpus_texts(corpus_path)
    gold = [r for r in load_jsonl(gold_path) if r.get("label_status") == LABEL_STATUS_CONSTRUCTED]
    if args.limit is not None:
        gold = gold[: args.limit]
    corpus_ids = {d["id"] for d in docs}

    missing_gold = [r["doc_id"] for r in gold if r["doc_id"] not in corpus_ids]
    if missing_gold:
        print(f"❌ gold 中 {len(missing_gold)} 个 doc_id 不在语料中，拒绝运行", file=sys.stderr)
        return 2

    print(f"corpus   : {len(docs)} docs  sha256={corpus_hash[:16]}…")
    print(f"gold     : {len(gold)} CONSTRUCTED records（构造保证相关，非人工判定）")
    print(f"configs  : {', '.join(args.configs)}\n")

    report: dict = {
        "schema_version": "rag-ablation-evidence/v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "code_provenance": _code_provenance(),
        "dataset": {
            "corpus_path": str(corpus_path.relative_to(REPO_ROOT)),
            "corpus_sha256": corpus_hash,
            "corpus_docs": len(docs),
            "gold_path": str(gold_path.relative_to(REPO_ROOT)),
            "gold_records": len(gold),
            "label_status": LABEL_STATUS_CONSTRUCTED,
            "annotation_method": gold[0]["annotation_method"] if gold else None,
        },
        "population_note": (
            "CONSTRUCTED gold：query := 文档标题（逐字），相关度由构造保证。"
            "度量的是**索引的词法可检索性**，不是真实查询的搜索质量。"
            "必须与本仓库的 JUDGED（人工判定）口径分开陈述，永不合并。"
        ),
        "configs": {},
        "environment": {"qdrant": f"{args.host}:{args.port}", "ks": list(DEFAULT_KS)},
    }

    # ── bm25_only（唯一可测的配置）────────────────────────────────────
    if "bm25_only" in args.configs:
        try:
            written = _ingest_corpus(args.host, args.port, docs, dim=1024)
            print(f"ingested {written} docs into local Qdrant collection {EVAL_COLLECTION!r}")
            started = time.perf_counter()
            retrieved = _bm25_retrieve(args.host, args.port, gold, top_k=args.top_k)
            elapsed = time.perf_counter() - started
            rows = []
            for record in gold:
                ids = retrieved.get(record["query_id"], [])
                metrics = compute_query_metrics(ids, [record["doc_id"]], ks=DEFAULT_KS)
                rows.append(
                    {
                        "query_id": record["query_id"],
                        "category": "known_item",
                        "failure": None if metrics["hit@1"] else "RANK_MISS",
                        **metrics,
                    }
                )
            summary = summarize_metric_rows(rows, DEFAULT_KS)

            # 负控：把「正确答案」系统性打乱后重跑同一批 query。
            # 如果打乱后指标仍然是 1.0，说明这个指标根本没在度量检索 ——
            # 那种情况下上面那份"漂亮"的数字毫无意义。负控不通过就**不能**
            # 声称该指标有效。
            negative_control = None
            if args.negative_control:
                shuffled = [
                    {**r, "doc_id": gold[(i + 1) % len(gold)]["doc_id"]} for i, r in enumerate(gold)
                ]
                rows_nc = []
                for record in shuffled:
                    ids = retrieved.get(record["query_id"], [])
                    m = compute_query_metrics(ids, [record["doc_id"]], ks=DEFAULT_KS)
                    rows_nc.append({"query_id": record["query_id"], "category": "known_item", **m})
                nc_summary = summarize_metric_rows(rows_nc, DEFAULT_KS)
                negative_control = {
                    "method": "gold 旋转一位后重算（同一批 query、同一份索引）",
                    "metrics": nc_summary,
                    "discriminates": nc_summary.get("hit@1", 0.0) < summary.get("hit@1", 0.0),
                }
                print(
                    f"[bm25_only] NEGATIVE CONTROL hit@1={nc_summary.get('hit@1', 0):.4f} "
                    f"(discriminates={negative_control['discriminates']})"
                )

            report["configs"]["bm25_only"] = {
                "status": "MEASURED",
                "channels": ["bm25"],
                "metrics": summary,
                "query_count": len(rows),
                "elapsed_seconds": round(elapsed, 3),
                "failure_breakdown": {
                    "rank_miss@1": sum(1 for r in rows if r["hit@1"] == 0.0),
                    "rank_miss@8": sum(1 for r in rows if r["hit@8"] == 0.0),
                },
                "negative_control": negative_control,
                "interpretation": (
                    "query 是文档标题的逐字副本，因此这是一个**词法可检索性天花板 / "
                    "索引完整性**检查：它证明语料确实进了索引、BM25 通道端到端可用。"
                    "它**不是**搜索质量指标 —— 真实查询的相关度仍需人工 JUDGED 标注。"
                ),
                "method": (
                    "corpus ingested into local Qdrant (payload only; vectors are zero placeholders "
                    "and are NEVER consulted); BM25 index rebuilt via the production "
                    "QdrantKnowledgeBase.rebuild_bm25_from_qdrant and queried via the production "
                    "_bm25_search path"
                ),
            }
            print(f"[bm25_only] MEASURED  {len(rows)} queries in {elapsed:.1f}s")
            for key in sorted(summary):
                print(f"    {key:14s} {summary[key]:.4f}")
        except Exception as exc:  # noqa: BLE001 - 结构化 BLOCKED，不静默
            report["configs"]["bm25_only"] = _blocked(
                "BM25_CHANNEL_FAILED", f"{type(exc).__name__}: {exc}", ["bm25_only"]
            )
            print(f"[bm25_only] BLOCKED  {type(exc).__name__}: {exc}", file=sys.stderr)

    # ── 向量相关配置：结构化 BLOCKED ──────────────────────────────────
    for name in ("vector_only", "hybrid_no_rerank", "hybrid_rerank"):
        if name in args.configs:
            report["configs"][name] = _blocked(
                "EMBEDDING_PROVIDER_UNAVAILABLE",
                (
                    "向量通道需要可用的 embedding provider（HTTP）。本环境凭据为占位符，"
                    "provider 调用不可用。**不使用占位向量替代** —— 占位向量会让 Qdrant 返回"
                    "任意结果，把 vector_only 的 Hit@K 变成随机召回率：一个看起来是数字、"
                    "实际毫无意义的指标。"
                ),
                ["vector_only", "hybrid_no_rerank", "hybrid_rerank"],
            )
            print(f"[{name}] BLOCKED  EMBEDDING_PROVIDER_UNAVAILABLE")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out)
        if args.out
        else (REPO_ROOT / "artifacts" / "evaluation" / "rag-ablation" / stamp / "report.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nevidence -> {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
