#!/usr/bin/env python3
"""
评测语料导入器：把 data/knowledge_base/knowledge_base_5000.jsonl 幂等导入 Qdrant。

用途（RAG 649 Evidence Refresh 工作单）：
    本地评测环境需要向量索引 + BM25 就绪后才能运行 649 条正式评测。
    本脚本负责：
      1. 语料 JSONL -> 4 个业务 collection 的确定性导入（复用 P1-03 稳定点 ID，幂等）
      2. scene -> collection 映射（取文档 scene 列表第一个值，确定性）
      3. 导入后触发 BM25 全量 rebuild（发布 READY）
      4. 计算并写出 benchmark gold docs 覆盖率（INDEX_COVERAGE 审计）
      5. 生成 import manifest（含 corpus sha256 / git SHA / embedding 模型 / 集合计数）

导入语义与 scripts/import_real_docs.py 一致：仅 embed content 文本，
title/category/scene/tags/source 进入 payload metadata。

用法：
    python3 scripts/import_eval_corpus.py                  # 正式导入 + rebuild + manifest
    python3 scripts/import_eval_corpus.py --dry-run        # 只统计与映射，不写 Qdrant
    python3 scripts/import_eval_corpus.py --skip-bm25      # 只导入向量，不 rebuild BM25
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_CORPUS = PROJECT_ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
DEFAULT_BENCHMARK = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "artifacts" / "evaluation" / "rag-649"

# scene -> 业务 collection（与评测/生产一致的四集合语义）
SCENE_TO_COLLECTION = {
    "售前咨询": "product_knowledge",
    "售后支持": "faq",
    "技术答疑": "tech_support",
    "投诉处理": "complaint_knowledge",
}

EVAL_COLLECTIONS = [
    "product_knowledge",
    "faq",
    "tech_support",
    "complaint_knowledge",
]

BATCH_SIZE = 64


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            check=True,
        ).stdout.strip()
    except Exception:
        return "unavailable"


def map_collection(doc: dict[str, Any]) -> str | None:
    """scene 列表第一个可映射值 -> collection（确定性；无映射返回 None）。"""
    scene = doc.get("scene")
    scenes = scene if isinstance(scene, list) else ([scene] if scene else [])
    for s in scenes:
        coll = SCENE_TO_COLLECTION.get(str(s))
        if coll:
            return coll
    return None


def load_corpus(path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def compute_coverage(corpus_ids: set[str], benchmark_path: Path) -> dict[str, Any]:
    """benchmark gold doc 覆盖率审计（INDEX_COVERAGE）。"""
    with open(benchmark_path, encoding="utf-8") as f:
        bench = json.load(f)
    queries = bench["queries"]
    gold_ids: set[str] = set()
    affected_queries: list[str] = []
    fully_missing_queries: list[str] = []
    for q in queries:
        expected = q.get("expected_doc_ids", [])
        gold_ids.update(expected)
        present = [gid for gid in expected if gid in corpus_ids]
        if len(present) < len(expected):
            affected_queries.append(q["query_id"])
        if not present:
            fully_missing_queries.append(q["query_id"])
    return {
        "benchmark_path": str(benchmark_path.relative_to(PROJECT_ROOT)),
        "benchmark_sha256": _sha256_file(benchmark_path),
        "unique_gold_doc_ids": len(gold_ids),
        "gold_docs_present": len(gold_ids & corpus_ids),
        "gold_docs_missing": len(gold_ids - corpus_ids),
        "coverage_ratio": round(len(gold_ids & corpus_ids) / max(len(gold_ids), 1), 4),
        "missing_gold_doc_ids": sorted(gold_ids - corpus_ids),
        "queries_with_missing_gold": len(affected_queries),
        "queries_with_all_gold_missing": len(fully_missing_queries),
        "query_ids_with_missing_gold": affected_queries,
    }


def _collection_payload_index(kb, collection: str) -> dict[str, str | None]:
    """Scroll one collection and return ``{logical doc_id: eval_corpus_hash}``.

    Uses the logical ``doc_id`` stored in the payload (never the storage Point
    ID) so identity can be verified against the requested corpus.
    """
    index: dict[str, str | None] = {}
    offset = None
    while True:
        result = kb._client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points = (
            list(result[0])
            if isinstance(result, tuple)
            else list(getattr(result, "points", []) or [])
        )
        offset = (
            result[1] if isinstance(result, tuple) else getattr(result, "next_page_offset", None)
        )
        for point in points:
            payload = getattr(point, "payload", None) or {}
            doc_id = payload.get("doc_id")
            if isinstance(doc_id, str) and doc_id:
                marker = payload.get("eval_corpus_hash")
                index[doc_id] = marker if isinstance(marker, str) else None
        if offset is None:
            break
    return index


async def import_corpus(
    kb, docs: list[dict[str, Any]], batch_size: int, corpus_sha256: str
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """分批幂等导入，返回 (各 collection 计数, 逐 collection identity 报告)。

    Crucially, a collection is skipped ONLY when its logical document identity
    verifies against the requested corpus (every expected doc_id present with a
    matching ``eval_corpus_hash``). A count-only check would accept a stale or
    foreign collection that happens to have enough points.
    """
    per_collection: dict[str, list[dict[str, Any]]] = {c: [] for c in EVAL_COLLECTIONS}
    unmapped: list[str] = []
    for d in docs:
        coll = map_collection(d)
        if coll is None:
            unmapped.append(d.get("id", "?"))
            continue
        per_collection[coll].append(d)

    counts: dict[str, int] = {}
    identity: dict[str, dict[str, Any]] = {}
    t0 = time.monotonic()
    total_batches = sum((len(ds) + batch_size - 1) // batch_size for ds in per_collection.values())
    done_batches = 0
    for coll in EVAL_COLLECTIONS:
        ds = per_collection[coll]
        expected_ids = {d["id"] for d in ds}
        existing = _collection_payload_index(kb, coll)
        present = expected_ids & set(existing)
        missing = expected_ids - set(existing)
        hash_mismatch = {doc_id for doc_id in present if existing.get(doc_id) != corpus_sha256}
        foreign = set(existing) - expected_ids
        identity_clean = not missing and not hash_mismatch and not foreign

        foreign_removed = 0
        if identity_clean:
            counts[coll] = kb.get_collection_count(coll)
            done_batches += (len(ds) + batch_size - 1) // batch_size
            action = "skipped"
        else:
            for i in range(0, len(ds), batch_size):
                chunk = ds[i : i + batch_size]
                await asyncio.to_thread(
                    kb.add_documents,
                    coll,
                    [d["content"] for d in chunk],
                    [
                        {
                            "doc_id": d["id"],
                            "title": d.get("title", ""),
                            "category": d.get("category", ""),
                            "scene": d.get("scene", []),
                            "tags": ",".join(d.get("tags", []) or []),
                            "source": d.get("source", "eval_corpus"),
                            # Corpus-provenance marker: identity verification
                            # rejects collections imported from a different
                            # corpus version even when the count matches.
                            "eval_corpus_hash": corpus_sha256,
                        }
                        for d in chunk
                    ],
                    [d["id"] for d in chunk],
                )
                done_batches += 1
                pct = done_batches / max(total_batches, 1) * 100
                elapsed = time.monotonic() - t0
                sys.stdout.write(
                    f"\r  导入进度: [{('#' * int(pct // 5)).ljust(20)}] "
                    f"{done_batches}/{total_batches} 批 ({pct:.0f}%) {elapsed:.0f}s"
                )
                sys.stdout.flush()
                await asyncio.sleep(0.15)  # 限速：避免触发 provider 限流
            # Repair must remove foreign points too, otherwise the collection
            # stays identity-dirty, can return stale documents during
            # evaluation, and is re-embedded on every subsequent import.
            if foreign:
                await asyncio.to_thread(kb.delete_documents, coll, sorted(foreign))
                foreign_removed = len(foreign)
                print()
                print(
                    f"  [PRUNE] {coll}: removed {foreign_removed} foreign/legacy "
                    f"point(s) not in the requested corpus"
                )
            counts[coll] = kb.get_collection_count(coll)
            action = "imported"

        # Post-import identity verification: never report completion on a
        # collection that still does not match the requested corpus.
        after = existing if action == "skipped" else _collection_payload_index(kb, coll)
        missing_after = expected_ids - set(after)
        hash_mismatch_after = {
            doc_id for doc_id in (expected_ids & set(after)) if after.get(doc_id) != corpus_sha256
        }
        foreign_after = set(after) - expected_ids
        identity_clean_after = not missing_after and not hash_mismatch_after and not foreign_after

        identity[coll] = {
            "expected_logical_ids": len(expected_ids),
            "present_before": len(present),
            "missing_before": len(missing),
            "hash_mismatch_before": len(hash_mismatch),
            "foreign_before": len(foreign),
            "identity_clean_before": identity_clean,
            "foreign_removed": foreign_removed,
            "missing_after": len(missing_after),
            "hash_mismatch_after": len(hash_mismatch_after),
            "foreign_after": len(foreign_after),
            "identity_clean_after": identity_clean_after,
            "action": action,
        }
    print()
    if unmapped:
        print(f"  [WARN] {len(unmapped)} 条文档无可映射 collection（前 5: {unmapped[:5]}）")
    return counts, identity


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--input", default=str(DEFAULT_CORPUS), help="语料 JSONL 路径")
    parser.add_argument(
        "--benchmark", default=str(DEFAULT_BENCHMARK), help="基准文件（覆盖率审计）"
    )
    parser.add_argument(
        "--manifest-dir", default=str(DEFAULT_MANIFEST_DIR), help="manifest 输出目录"
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="只统计与映射，不写 Qdrant")
    parser.add_argument("--skip-bm25", action="store_true", help="导入后不执行 BM25 rebuild")
    args = parser.parse_args()

    corpus_path = Path(args.input)
    benchmark_path = Path(args.benchmark)
    if not corpus_path.exists():
        print(f"[ERROR] 语料不存在: {corpus_path}")
        return 1

    docs = load_corpus(corpus_path)
    corpus_sha256 = _sha256_file(corpus_path)
    print(f"语料: {corpus_path.name} -> {len(docs)} docs (sha256={corpus_sha256[:16]}…)")

    from core.config import (
        EMBEDDING_DIM,
        EMBEDDING_MODEL,
        QDRANT_API_KEY,
        QDRANT_HOST,
        QDRANT_PORT,
    )
    from rag.qdrant_knowledge_base import BM25Readiness, QdrantKnowledgeBase

    # Canonical endpoint: same QDRANT_HOST/PORT (+ optional API key) as
    # scripts/evaluate_rag.py. A hardcoded localhost:6333 would silently import
    # into a different database whenever the deployment overrides the host
    # (e.g. Docker Compose service name `qdrant`).
    kb = QdrantKnowledgeBase(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY or "")
    if not kb.available:
        print(f"[FATAL] Qdrant 不可达 ({QDRANT_HOST}:{QDRANT_PORT})")
        return 1
    if not kb.embedding_available:
        print("[FATAL] EMBEDDING_API_KEY 未配置，向量导入无法进行（fail-closed）")
        return 1

    coverage = compute_coverage({d["id"] for d in docs}, benchmark_path)
    print(
        f"Gold 覆盖率: {coverage['gold_docs_present']}/{coverage['unique_gold_doc_ids']}"
        f" ({coverage['coverage_ratio']:.1%})，缺失 {coverage['gold_docs_missing']}"
        f"（影响 {coverage['queries_with_missing_gold']} 条查询）"
    )

    manifest: dict[str, Any] = {
        "schema_version": "eval_corpus_import_manifest/v1",
        "run_id": f"import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "corpus": {
            "path": str(corpus_path.relative_to(PROJECT_ROOT)),
            "sha256": corpus_sha256,
            "declared_docs": len(docs),
            "expected_logical_ids": len({d["id"] for d in docs}),
        },
        "embedding": {"model": EMBEDDING_MODEL, "dim": EMBEDDING_DIM},
        "qdrant": {
            "host": QDRANT_HOST,
            "port": QDRANT_PORT,
            "collections": EVAL_COLLECTIONS,
        },
        "scene_mapping": SCENE_TO_COLLECTION,
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        mapped: dict[str, int] = {}
        for d in docs:
            c = map_collection(d)
            if c:
                mapped[c] = mapped.get(c, 0) + 1
        manifest["planned_counts"] = mapped
        manifest["coverage"] = coverage
        print("dry-run 映射结果:", json.dumps(mapped, ensure_ascii=False))
    else:
        print("开始导入（幂等，确定性 point id；按 logical identity 校验）...")
        counts, identity = await import_corpus(kb, docs, args.batch_size, corpus_sha256)
        manifest["imported_counts"] = counts
        manifest["total_indexed"] = sum(counts.values())
        manifest["identity"] = identity
        manifest["coverage"] = coverage
        print(f"导入完成: {json.dumps(counts, ensure_ascii=False)}")

        dirty = [coll for coll, info in identity.items() if not info["identity_clean_after"]]
        if dirty:
            manifest["post_import_identity_clean"] = False
            print(
                f"[ERROR] post-import identity 未通过: {dirty}；"
                "collection 仍包含 foreign/missing/hash-mismatch 文档，"
                "拒绝报告导入完成"
            )
            # Persist the manifest before failing so the audit trail is intact.
            manifest_dir = Path(args.manifest_dir)
            manifest_dir.mkdir(parents=True, exist_ok=True)
            out = manifest_dir / f"import_manifest_{manifest['run_id']}.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            print(f"manifest 已保存: {out}")
            return 1
        manifest["post_import_identity_clean"] = True

        if not args.skip_bm25:
            print("执行 BM25 全量 rebuild ...")
            meta = await asyncio.to_thread(kb.rebuild_bm25_from_qdrant, EVAL_COLLECTIONS)
            manifest["bm25"] = {
                "readiness": str(kb.bm25_readiness()),
                "document_count": meta.document_count if meta else 0,
            }
            if kb.bm25_readiness() is not BM25Readiness.READY:
                print("[ERROR] BM25 rebuild 未达到 READY，评测 hybrid 通道将不可用")
                return 1
            print(f"BM25 READY: {manifest['bm25']['document_count']} docs")

    manifest_dir = Path(args.manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    out = manifest_dir / f"import_manifest_{manifest['run_id']}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"manifest 已保存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
