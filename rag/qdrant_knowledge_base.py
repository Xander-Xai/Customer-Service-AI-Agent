"""
Qdrant 知识库管理器（internal feature milestone: hybrid retrieval）
基于向量检索的 RAG 检索增强生成（Qdrant 实现）。

混合检索升级（internal feature milestone）：
- 新增 BM25 词法检索通道（内存倒排索引）
- 新增 RRF 融合（向量 + BM25 结果排名融合）
- 新增 HYBRID_SEARCH_ENABLED 配置开关
- query_multiple() 并行执行向量检索 + BM25 检索

设计要点：
- API 兼容 CosmeticsKnowledgeBase（KnowledgeBaseProtocol）
- embedding 在应用侧计算（bge-large-zh-v1.5），Qdrant 仅做向量存储和检索
- 支持 text + image（CLIP）双 embedding
- 内建重试和连接池
"""

import asyncio
import threading
import time
from typing import Any, cast

from qdrant_client import QdrantClient
from qdrant_client.http import models

from core.config import (
    HYBRID_BM25_TOP_K,
    HYBRID_RRF_K,
    HYBRID_SEARCH_ENABLED,
    HYBRID_VECTOR_TOP_K,
)
from core.logger import get_logger
from core.monitoring import (
    bm25_fallback_used_total,
    bm25_rebuild_failures_total,
    bm25_rebuilds_total,
    embedding_dimension_errors_total,
    embedding_provider_failures_total,
    retrieval_no_channel_total,
    vector_channel_disabled_total,
)
from rag.bm25_lifecycle import (
    REASON_BM25_NOT_READY,
    REASON_QDRANT_UNAVAILABLE,
    REASON_REBUILD_SCROLL_FAILED,
    REASON_REBUILD_TIMEOUT,
    BM25IndexMeta,
    BM25Readiness,
    _now,
)
from rag.embedding_status import (
    EmbeddingDimensionError,
    EmbeddingStatus,
    EmbeddingUnavailableError,
    RetrievalResultList,
    validate_embedding_vector,
)
from rag.point_id import assert_no_point_id_collision, document_id_to_point_id
from rag.retrieval_contract import (
    STAGE_BM25,
    STAGE_FILTER,
    STAGE_FINAL,
    STAGE_FUSION_RRF,
    STAGE_RERANK,
    STAGE_REWRITE,
    STAGE_VECTOR,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    StageStatus,
    TraceStage,
)

logger = get_logger("rag.qdrant_knowledge_base")

# bge-large-zh-v1.5 输出维度
_EMBEDDING_DIM = 1024


# ===== RRF 融合函数（hybrid retrieval milestone）=====


def rrf_fusion(
    result_lists: list[list[dict[str, Any]]],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：融合多个检索通道的结果。

    Args:
        result_lists: 每个检索通道的结果列表（按各自评分降序排列）
        k: RRF 平滑常数（默认 60，标准值）

    Returns:
        按 RRF 综合分数降序排列的去重结果列表
    """
    if not result_lists:
        return []
    if len(result_lists) == 1:
        return result_lists[0]

    rrf_scores: dict[str, tuple[float, dict[str, Any]]] = {}

    for channel_results in result_lists:
        for rank, doc in enumerate(channel_results, start=1):
            content = doc.get("content", "")
            if not content:
                continue
            score = 1.0 / (k + rank)
            if content in rrf_scores:
                existing_score, _ = rrf_scores[content]
                rrf_scores[content] = (existing_score + score, doc)
            else:
                rrf_scores[content] = (score, doc)

    if not rrf_scores:
        return []

    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1][0], reverse=True)
    fused = []
    for _content, (score, doc) in sorted_items:
        result = dict(doc)
        result["rrf_score"] = round(score, 4)
        result.pop("bm25_score", None)
        result.pop("distance", None)
        fused.append(result)

    return fused


class QdrantKnowledgeBase:
    """化妆品领域知识库（基于 Qdrant 向量检索），兼容 KnowledgeBaseProtocol"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        grpc_port: int = 6334,
        prefer_grpc: bool = False,
        api_key: str = "",
        clip_enabled: bool = False,
        embedding_model=None,
    ):
        self._clip_enabled = clip_enabled
        self._clip_embed_fn = None
        self._reranker: Any = None
        self._embed_fn = embedding_model or self._create_embedding_function()
        # P0-05: provider/model metadata for degraded-result observability.
        # Derive a stable string name; never expose the embed_fn object itself.
        _model_name = getattr(self._embed_fn, "model", None)
        self._embedding_model_name: str = (
            _model_name
            if isinstance(_model_name, str)
            else (QdrantKnowledgeBase._embed_fn_name if self._embed_fn is not None else "unavailable")
        )
        self._embedding_provider: str = (
            type(self._embed_fn).__name__ if self._embed_fn is not None else "unavailable"
        )
        self._collection_cache: dict[str, bool] = {}
        # hybrid retrieval: BM25 词法通道 + 开关
        self._bm25: Any = None
        self._hybrid_enabled = HYBRID_SEARCH_ENABLED
        # P1-02: BM25 lifecycle state. ``self._bm25 is not None`` is NOT a
        # readiness signal — an empty lazy instance is still UNINITIALIZED.
        # Only a successful ``rebuild_bm25_from_qdrant`` publishes READY.
        self._bm25_readiness: BM25Readiness = BM25Readiness.UNINITIALIZED
        self._bm25_meta: BM25IndexMeta | None = None
        self._bm25_lock = threading.Lock()  # serializes rebuild vs rebuild
        self._bm25_version_counter = 0
        # P1-02 BM25-11/17: rebuild generation. An in-flight rebuild captures
        # the value at start; ``supersede_bm25_rebuild`` (container timeout)
        # bumps it. Before publishing, the rebuild refuses to swap its
        # candidate if the generation no longer matches — so a rebuild that
        # completes AFTER the container timed out cannot flip DEGRADED back
        # to READY.
        self._bm25_build_generation = 0
        # Page size for Qdrant scroll during BM25 rebuild.
        self._bm25_scroll_page = 500

        try:
            self._client = QdrantClient(
                host=host,
                port=port,
                grpc_port=grpc_port,
                prefer_grpc=prefer_grpc,
                api_key=api_key or None,
                timeout=10.0,
            )
            self._client.get_collections()
            self._available = True
            clip_info = f", clip={'enabled' if clip_enabled else 'disabled'}" if clip_enabled else ""
            logger.info(f"Qdrant 连接成功 ({host}:{port}{clip_info})")
        except Exception as e:
            self._available = False
            logger.error(f"Qdrant 连接失败 ({host}:{port}): {e}", exc_info=True)

    @staticmethod
    def _create_embedding_function():
        """创建 API 嵌入客户端（替代本地 SentenceTransformer）"""
        try:
            from core.config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL

            if not EMBEDDING_API_KEY:
                logger.warning(
                    "EMBEDDING_API_KEY 未配置，embedding 不可用（向量通道将被禁用，"
                    "不生成随机向量）"
                )
                return None

            from rag.api_embedding import ApiEmbedding

            model = ApiEmbedding(
                api_key=EMBEDDING_API_KEY,
                model=EMBEDDING_MODEL,
                base_url=EMBEDDING_BASE_URL,
            )
            QdrantKnowledgeBase._embed_fn_name = EMBEDDING_MODEL.split("/")[-1]
            logger.info(f"API Embedding 客户端创建成功: {EMBEDDING_MODEL}")
            return model
        except Exception as e:
            QdrantKnowledgeBase._embed_fn_name = "default(unavailable)"
            logger.warning(
                f"API Embedding 创建失败: {e}（embedding 不可用，向量通道将被禁用，"
                "不生成随机向量）"
            )
            return None

    _embed_fn_name: str = "unknown"

    # ---- P0-05: embedding channel state & typed failure contract ----

    @property
    def embedding_available(self) -> bool:
        """Whether the vector embedding channel is usable (embed_fn configured).

        Callers pre-check this to explicitly DISABLE the vector channel on
        dependency failure — never to fabricate a vector.
        """
        return self._embed_fn is not None

    def embedding_status(self) -> EmbeddingStatus:
        """Typed embedding channel state for callers/metrics."""
        return EmbeddingStatus.AVAILABLE if self._embed_fn is not None else EmbeddingStatus.UNAVAILABLE

    def _record_embedding_failure(self, reason: str) -> None:
        """Increment the embedding-provider-failure counter (label = reason only)."""
        embedding_provider_failures_total.labels(reason=reason).inc()

    def _degraded_meta(
        self, reason: str, *, vector_used: bool, lexical_used: bool
    ) -> dict[str, Any]:
        """Per-query degraded metadata attached to a RetrievalResultList."""
        return {
            "retrieval_degraded": True,
            "degraded_reason": reason,
            "vector_channel_used": vector_used,
            "lexical_channel_used": lexical_used,
            "embedding_provider": self._embedding_provider,
            "embedding_model": self._embedding_model_name,
        }

    def _degraded_result(
        self, reason: str, *, vector_used: bool, lexical_used: bool
    ) -> RetrievalResultList:
        return RetrievalResultList(
            [], meta=self._degraded_meta(reason, vector_used=vector_used, lexical_used=lexical_used)
        )

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into vectors.

        P0-05: NEVER fabricates a vector on dependency failure. When the
        embedding provider is unavailable (no embed_fn) or encode() raises,
        raises EmbeddingUnavailableError so the caller disables the vector
        channel. Wrong-dimension / non-finite vectors raise
        EmbeddingDimensionError and are never padded, truncated, or random-filled.
        """
        if self._embed_fn is None:
            self._record_embedding_failure("provider_unavailable")
            raise EmbeddingUnavailableError(
                reason="provider_unavailable",
                provider=self._embedding_provider,
                model=self._embedding_model_name,
            )
        try:
            vectors = self._embed_fn.encode(texts)
            vectors = vectors.tolist() if hasattr(vectors, "tolist") else list(vectors)
        except Exception as e:
            self._record_embedding_failure("encode_failed")
            logger.warning(f"Embedding encode 失败，禁用向量通道: {e}")
            raise EmbeddingUnavailableError(
                reason="encode_failed",
                provider=self._embedding_provider,
                model=self._embedding_model_name,
            ) from e
        # Dimension + finiteness validation — reject, do not silently reshape.
        for v in vectors:
            try:
                validate_embedding_vector(v, _EMBEDDING_DIM, model=self._embedding_model_name)
            except EmbeddingDimensionError:
                embedding_dimension_errors_total.inc()
                raise
        return vectors

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_collection(self, name: str) -> bool:
        if name in self._collection_cache:
            return True
        if not self._available:
            return False
        try:
            collections = self._client.get_collections().collections
            exists = any(c.name == name for c in collections)
            if not exists:
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=models.VectorParams(
                        size=_EMBEDDING_DIM,
                        distance=models.Distance.COSINE,
                    ),
                    hnsw_config=models.HnswConfigDiff(
                        m=16,
                        ef_construct=100,
                    ),
                    optimizers_config=models.OptimizersConfigDiff(
                        default_segment_number=2,
                    ),
                )
                logger.debug(f"Qdrant collection '{name}' 已创建")
            self._collection_cache[name] = True
            return True
        except Exception as e:
            logger.error(f"Qdrant collection '{name}' 创建失败: {e}")
            return False

    def get_or_create_collection(self, name: str) -> str | None:
        return name if self._ensure_collection(name) else None

    def add_documents(
        self,
        collection_name: str,
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ):
        if not self._ensure_collection(collection_name):
            return
        if not documents:
            return
        if ids is None:
            try:
                count = self._client.count(collection_name).count
            except Exception:
                count = 0
            ids = [f"{collection_name}_{count + i}" for i in range(len(documents))]
        if metadatas is None:
            metadatas = [{}] * len(documents)
        cleaned_metadatas = [{k: v for k, v in m.items() if v is not None} if m else {} for m in metadatas]

        # P0-05: fail closed on embedding unavailability — NEVER persist a fake
        # (random/pseudo) vector. Skip the Qdrant vector upsert; the lexical
        # (BM25) channel is embedding-independent and may still be indexed.
        if not self.embedding_available:
            vector_channel_disabled_total.inc()
            logger.warning(
                "Embedding 不可用，跳过向量写入（拒绝持久化伪造向量），仅索引词法通道: "
                f"collection={collection_name} docs={len(documents)} "
                f"model={self._embedding_model_name}"
            )
            if self._hybrid_enabled:
                # P1-02 BM25-17: hold the rebuild lock across the BM25 mutation
                # so a concurrent rebuild cannot discard this write by swapping
                # in a candidate built from a pre-write scroll snapshot. The
                # Qdrant upsert above is NOT under the lock (slow I/O); only
                # the in-memory BM25 mutation is.
                with self._bm25_lock:
                    self._ensure_bm25().upsert_documents(
                        documents,
                        collection=collection_name,
                        ids=ids,
                        metadatas=cleaned_metadatas,
                    )
                    self._on_bm25_mutation()
            return

        vectors = self._embed_texts(documents)

        # P1-03: stable, deterministic Qdrant Point IDs. The previous
        # `hash(id_) & 0x7FFFFFFFFFFFFFFF` was randomized per Python process
        # via PYTHONHASHSEED, so the same logical doc_id mapped to a different
        # storage Point ID on every restart — breaking idempotent upsert
        # (ghost duplicates) and any cross-process Point-ID reference. All
        # Point ID generation MUST go through this single boundary; business
        # code may not call hash() for a persistent id.
        point_ids = [document_id_to_point_id(collection_name, id_) for id_ in ids]
        # ID-5: refuse to silently overwrite a different document stored at
        # the same stable Point ID (SHA-256 truncation collision / legacy
        # contamination). Fails closed — never upsert when we cannot verify.
        assert_no_point_id_collision(self._client, collection_name, point_ids, ids)

        points = [
            models.PointStruct(
                id=pid,
                vector=vector,
                # Explicit identity wins: a caller-supplied metadata ``doc_id``
                # must never overwrite the logical id used to derive/verify the
                # Point ID (otherwise the stored payload owner disagrees with
                # the collision guard and later idempotent writes get rejected).
                payload={**meta, "doc_id": id_, "content": doc},
            )
            for pid, id_, doc, vector, meta in zip(  # noqa: B905 - preserve ingestion semantics
                point_ids, ids, documents, vectors, cleaned_metadatas
            )
        ]

        self._client.upsert(collection_name=collection_name, points=points)
        logger.debug(f"Collection '{collection_name}' 添加 {len(documents)} 条文档")

        # hybrid retrieval: 同步更新 BM25 索引（P1-02: 幂等 upsert，避免重复 doc_id 追加）
        if self._hybrid_enabled:
            # P1-02 BM25-17: serialize the BM25 mutation with rebuilds so a
            # concurrent rebuild cannot discard this write via candidate swap.
            with self._bm25_lock:
                self._ensure_bm25().upsert_documents(
                    documents,
                    collection=collection_name,
                    ids=ids,
                    metadatas=cleaned_metadatas,
                )
                self._on_bm25_mutation()

    def delete_documents(self, collection_name: str, ids: list[str]):
        if not self._ensure_collection(collection_name):
            return
        if not ids:
            return
        for id_ in ids:
            self._client.delete(
                collection_name=collection_name,
                points_selector=models.Filter(
                    must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=id_))]
                ),
            )
        logger.debug(f"Collection '{collection_name}' 删除 {len(ids)} 条文档")

        # P1-02 BM25-8/16: keep BM25 in lockstep with Qdrant deletes so the
        # lexical channel cannot serve stale tokens for deleted documents.
        # ``self._bm25`` is None (never created), False (init failed), or a
        # BM25Retriever instance — the truthy check covers exactly the last.
        # BM25-17: hold the rebuild lock so a concurrent rebuild cannot swap
        # out a candidate that omits this delete.
        if self._hybrid_enabled and self._bm25:
            with self._bm25_lock:
                self._bm25.remove_documents(collection_name, ids)
                self._on_bm25_mutation()

    def seed_if_empty(self, collection_name: str, seed_fn):
        if not self._ensure_collection(collection_name):
            return
        try:
            count = self._client.count(collection_name).count
        except Exception:
            count = 0
        if count == 0:
            seed_fn(self, collection_name=collection_name)
            logger.info(f"Collection '{collection_name}' 已种子初始化")

    def get_collection_count(self, collection_name: str) -> int:
        if not self._available:
            return 0
        try:
            return int(self._client.count(collection_name).count)
        except Exception:
            return 0

    async def query_with_vector(
        self, collection_name: str, query_vector: list[float], n_results: int = 3
    ) -> list[dict[str, Any]]:
        """P1-01: precomputed-vector dense query via ``query_points`` (1.18 API).

        P0-05 BF-03 (Phase 1 Gate re-review): validate the caller-supplied
        precomputed vector BEFORE Qdrant dispatch — never send a
        wrong-dimension / non-finite / non-numeric vector to Qdrant. Invalid
        input raises ``EmbeddingDimensionError`` (consistent with
        ``_embed_texts``); the caller decides degrade/error handling. After
        validation, forwards to the unified ``_dense_query`` (query_points).
        """
        validate_embedding_vector(
            query_vector, _EMBEDDING_DIM, model=self._embedding_model_name
        )
        return await self._dense_query(collection_name, query_vector, n_results, None)

    async def query(
        self, collection_name: str, query_text: str, n_results: int = 3
    ) -> RetrievalResult:
        """P1-01: compatibility shim — forwards to the unified ``retrieve``
        pipeline. Business agents call ``retrieve`` directly; this is kept for
        scripts/tests using the legacy single-collection signature.

        Per spec step 21, the shim does NOT keep an independent vector-only
        semantics: it runs the full unified pipeline (rewrite → filter →
        dense+BM25 → RRF → rerank) and returns a ``RetrievalResult`` (IS-A
        list, so list callers are unaffected; callers reading ``.meta`` get
        the P0-05/P1-02 degraded contract, and ``.trace`` is available).
        """
        return await self.retrieve(
            RetrievalRequest(
                query=query_text, collections=[collection_name], top_k=n_results
            )
        )

    async def search(
        self,
        query: str,
        top_k: int = 5,
        scene: str | None = None,
        filter_dict: dict | None = None,
        collection_name: str = "product_knowledge",
    ) -> RetrievalResult:
        """P1-01: compatibility shim — forwards to the unified ``retrieve``
        pipeline. The legacy ``collection_name`` default (``product_knowledge``)
        is preserved for scripts, but business agents pass an explicit
        collection via ``retrieve`` (the old default was a scene-bypass bug).
        Runs the full unified pipeline (spec step 21: no separate semantics).
        """
        return await self.retrieve(
            RetrievalRequest(
                query=query,
                collections=[collection_name],
                scene=scene,
                metadata_filters=filter_dict,
                top_k=top_k,
            )
        )

    async def query_multiple(
        self, collection_names: list[str], query_text: str, n_results: int = 3
    ) -> RetrievalResult:
        """P1-01: compatibility shim — forwards to the unified ``retrieve``
        pipeline. The legacy hybrid semantics (rewrite → vector+BM25 → RRF →
        reranker, with the P0-05/P1-02 degraded ``.meta`` contract) are now
        owned by ``retrieve``; this shim is a thin adapter so existing callers
        (scripts/tests) keep working without a separate code path (spec step
        21: shims must not keep an independent retrieval semantics).
        """
        return await self.retrieve(
            RetrievalRequest(
                query=query_text,
                collections=list(collection_names),
                top_k=n_results,
            )
        )

    # ===== P1-01: unified retrieval entrypoint =====

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """P1-01: the single canonical retrieval owner.

        Orchestrates the full pipeline (spec step 9):

            Rewrite → Scene/Metadata Filter → Vector + BM25 → RRF → Rerank → Evidence

        Consumes the existing contracts unchanged — P0-05 (embedding
        fail-closed; never a fake vector), P1-02 (BM25 readiness gates the
        lexical channel), P1-03 (stable IDs via the existing scroll/point-id
        boundary). RRF weights and the reranker model are NOT tuned. The
        dense channel uses ``query_points`` (qdrant-client 1.18 API), fixing
        the pre-existing ``self._client.search`` breakage deferred from P1-02.

        Returns a ``RetrievalResult`` (IS-A ``RetrievalResultList``): the same
        ``.meta`` 4-combo degraded contract callers/tests already rely on,
        plus a ``.trace`` transcript of every stage.
        """
        from rag.embedding_status import (
            REASON_EMBEDDING_UNAVAILABLE,
            REASON_NO_RETRIEVAL_CHANNEL,
        )

        trace = RetrievalTrace()
        loop = asyncio.get_running_loop()
        top_k = request.top_k

        # ---- 1. REWRITE — one canonical rewritten query (spec step 10) ----
        # The pipeline owns ALL rewrite; the agent no longer rewrites then
        # calls a KB that rewrites again. Prefetched rewrite is reused.
        t0 = time.perf_counter()
        canonical = request.query
        if request.rewrite:
            if request.prefetched_rewritten_query:
                canonical = request.prefetched_rewritten_query
            else:
                canonical = await self._canonical_rewrite(canonical, request.llm)
        trace.rewritten_query = canonical
        trace.add(TraceStage(
            STAGE_REWRITE,
            StageStatus.EXECUTED if request.rewrite else StageStatus.SKIPPED,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            reason="" if request.rewrite else "rewrite_disabled",
        ))

        # ---- 2. FILTER — scene + metadata, applied by the pipeline (step 11) ----
        t0 = time.perf_counter()
        query_filter = self._build_retrieve_filter(request.scene, request.metadata_filters)
        trace.add(TraceStage(
            STAGE_FILTER, StageStatus.EXECUTED,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        ))

        # ---- 3. VECTOR — dense channel; consumes P0-05 (no fake vector) ----
        # Dense uses query_points (1.18). Prefetched embedding is reused so
        # embedding is never computed twice (spec step 20). A channel that
        # times out / errors is DEGRADED and excluded from RRF fusion (spec
        # step 14: only fully-successful channels fuse).
        t0 = time.perf_counter()
        bm25_ready = self._hybrid_enabled and self.bm25_readiness() is BM25Readiness.READY
        vector_status = "ok"
        vector_results: list[dict[str, Any]] = []
        query_vector: list[float] | None = None

        if not self.embedding_available:
            vector_status = "embedding_unavailable"
            vector_channel_disabled_total.inc()
        else:
            if request.prefetched_embedding is not None:
                try:
                    validate_embedding_vector(
                        request.prefetched_embedding, _EMBEDDING_DIM,
                        model=self._embedding_model_name,
                    )
                    query_vector = request.prefetched_embedding
                except EmbeddingDimensionError as e:
                    self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
                    embedding_dimension_errors_total.inc()
                    vector_status = "embedding_invalid"
            if query_vector is None and vector_status == "ok":
                try:
                    query_vector = await loop.run_in_executor(
                        None, lambda: self._embed_texts([canonical])[0]
                    )
                except EmbeddingUnavailableError as e:
                    self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
                    vector_status = "embedding_unavailable"
                except EmbeddingDimensionError as e:
                    self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
                    vector_status = "embedding_invalid"
            if query_vector is not None and vector_status == "ok":
                vector_top_k = max(top_k * 2, HYBRID_VECTOR_TOP_K)
                dense_results, dense_failure = await self._dense_query_multi(
                    request.collections, query_vector, vector_top_k,
                    query_filter, request.retrieval_timeout,
                )
                if dense_failure is not None:
                    vector_status = dense_failure  # retrieval_timeout | vector_channel_error
                    vector_results = []  # exclude partial-failed channel from fusion
                else:
                    vector_results = dense_results

        trace.add(TraceStage(
            STAGE_VECTOR,
            StageStatus.EXECUTED if vector_status == "ok" else StageStatus.DEGRADED,
            candidate_in=len(request.collections),
            candidate_out=len(vector_results),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            reason=vector_status if vector_status != "ok" else "",
        ))

        # ---- 4. BM25 — consumes P1-02 readiness (spec step 13) ----
        t0 = time.perf_counter()
        bm25_results: list[dict[str, Any]] = []
        if bm25_ready:
            bm25_top_k = max(top_k * 2, HYBRID_BM25_TOP_K)
            try:
                bm25_results = await loop.run_in_executor(
                    None,
                    lambda: self._bm25_search(
                        canonical,
                        request.collections,
                        top_k=bm25_top_k,
                        scene=request.scene,
                        metadata_filters=request.metadata_filters,
                    ),
                )
            except Exception as e:
                logger.debug(f"retrieve() BM25 检索异常: {e}")
                bm25_results = []
            trace.add(TraceStage(
                STAGE_BM25, StageStatus.EXECUTED,
                candidate_out=len(bm25_results),
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            ))
        else:
            trace.add(TraceStage(
                STAGE_BM25, StageStatus.DEGRADED,
                reason=REASON_BM25_NOT_READY,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            ))

        # ---- 5. FUSION_RRF — fuse only successful channels (spec step 14) ----
        t0 = time.perf_counter()
        deduped_vector = self._dedupe(vector_results)
        channels: list[list[dict[str, Any]]] = []
        if deduped_vector:
            channels.append(deduped_vector)
        if bm25_results:
            channels.append(bm25_results)
        if len(channels) > 1:
            fused = rrf_fusion(channels, k=HYBRID_RRF_K)
            fusion_status = StageStatus.EXECUTED
        elif len(channels) == 1:
            fused = channels[0]
            fusion_status = StageStatus.SKIPPED  # single channel — no fusion needed
        else:
            fused = []
            fusion_status = StageStatus.SKIPPED
        trace.add(TraceStage(
            STAGE_FUSION_RRF, fusion_status,
            candidate_in=sum(len(c) for c in channels),
            candidate_out=len(fused),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        ))

        # ---- 6. RERANK — at most one canonical stage (spec step 15) ----
        t0 = time.perf_counter()
        if request.rerank and len(fused) > 1:
            reranked = self._apply_reranker(canonical, fused, top_k=top_k)
            rerank_status = StageStatus.EXECUTED
        else:
            reranked = fused
            rerank_status = StageStatus.SKIPPED
        trace.add(TraceStage(
            STAGE_RERANK, rerank_status,
            candidate_in=len(fused),
            candidate_out=len(reranked),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            reason="" if request.rerank else "rerank_disabled",
        ))

        # ---- 7. FINAL — evidence + degraded meta (4-combo contract; step 23) ----
        t0 = time.perf_counter()
        evidence = reranked[:top_k]
        vector_used = vector_status == "ok"
        lexical_used = bool(bm25_results)
        if not vector_used:
            if vector_status == "embedding_unavailable":
                reason = REASON_EMBEDDING_UNAVAILABLE
            elif vector_status == "embedding_invalid":
                reason = "embedding_invalid"
            elif vector_status == "retrieval_timeout":
                reason = "retrieval_timeout"
            else:
                reason = "vector_channel_error"
            logger.warning(
                f"向量检索通道禁用（{reason}），降级为词法检索: "
                f"model={self._embedding_model_name}"
            )
            if lexical_used:
                # combo B: embedding down + BM25 ready → lexical degraded
                bm25_fallback_used_total.inc()
                meta = self._degraded_meta(reason, vector_used=False, lexical_used=True)
            else:
                # combo D: no channel
                retrieval_no_channel_total.inc()
                logger.warning("无可用检索通道（向量禁用 + BM25 空），返回降级空结果")
                meta = self._degraded_meta(
                    REASON_NO_RETRIEVAL_CHANNEL, vector_used=False, lexical_used=False
                )
        elif not bm25_ready:
            # combo C: vector available + BM25 not ready → vector degraded
            meta = self._degraded_meta(REASON_BM25_NOT_READY, vector_used=True, lexical_used=False)
        else:
            # combo A: normal hybrid (lexical_channel_used reflects whether BM25
            # returned anything; the channel itself was ready)
            meta = {
                "retrieval_degraded": False,
                "degraded_reason": "",
                "vector_channel_used": True,
                "lexical_channel_used": lexical_used,
                "embedding_provider": self._embedding_provider,
                "embedding_model": self._embedding_model_name,
            }
        trace.add(TraceStage(
            STAGE_FINAL, StageStatus.EXECUTED,
            candidate_out=len(evidence),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        ))

        # P0-05: surface degraded state to the caller's state (no query text)
        if request.state is not None and meta.get("retrieval_degraded"):
            request.state["retrieval_degraded"] = True
            request.state["degraded_reason"] = meta.get(
                "degraded_reason", REASON_EMBEDDING_UNAVAILABLE
            )

        return RetrievalResult(evidence, meta=meta, trace=trace)

    async def _canonical_rewrite(self, query: str, llm: Any = None) -> str:
        """P1-01: the ONE canonical query rewrite owned by the pipeline.

        Composes (1) an optional LLM rewrite (gated by RAG_QUERY_REWRITING,
        the same flag the agent used to apply) and (2) the deterministic
        synonym expansion ``query_multiple`` already ran. The result is a
        single canonical rewritten query recorded in the trace — the agent no
        longer rewrites on its own (eliminating the double-rewrite on the
        hybrid path).
        """
        canonical = query
        if llm is not None:
            try:
                from core.config import RAG_QUERY_REWRITING

                if RAG_QUERY_REWRITING:
                    canonical = await self.rewrite_query(query, llm)
            except Exception as e:
                logger.debug(f"retrieve LLM rewrite 失败，使用原查询: {e}")
        try:
            from rag.query_rewriter import QueryRewriter

            canonical = QueryRewriter().expand_query(canonical)
        except Exception as e:
            logger.debug(f"retrieve synonym 扩展失败: {e}")
        return canonical

    def _build_retrieve_filter(self, scene: str | None, metadata_filters: dict | None):
        """P1-01: build the Qdrant payload filter from scene + metadata.

        Returns None when no filter applies. Applied by retrieve() (not the
        agent) so prefetch cannot bypass it (spec step 11).
        """
        if not scene and not metadata_filters:
            return None
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        conditions = []
        if scene:
            conditions.append(FieldCondition(key="scene", match=MatchValue(value=scene)))
        if metadata_filters:
            for key, value in metadata_filters.items():
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=conditions) if conditions else None

    async def _dense_query_multi(
        self,
        collections: list[str],
        query_vector: list[float],
        limit: int,
        query_filter,
        timeout: float | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """P1-01: query every collection via ``query_points`` (1.18 API).

        Returns ``(results, failure_reason_or_None)``. Per-collection failures
        are collected (return_exceptions) so one slow collection does not
        discard the others' results; a timeout/error sets ``failure_reason``
        so retrieve() can mark the VECTOR stage DEGRADED (spec step 14: a
        channel that did not fully succeed is excluded from RRF). This is a
        local retrieval-level timeout (spec step 24) — NOT the P1-06 global
        request deadline.
        """
        coros = [self._dense_query(c, query_vector, limit, query_filter) for c in collections]
        if timeout is not None:
            coros = [asyncio.wait_for(c, timeout=timeout) for c in coros]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        out: list[dict[str, Any]] = []
        failure_reason: str | None = None
        for r in raw:
            if isinstance(r, BaseException):
                if isinstance(r, (asyncio.TimeoutError, TimeoutError)):
                    failure_reason = "retrieval_timeout"
                else:
                    failure_reason = "vector_channel_error"
            else:
                out.extend(r)
        return out, failure_reason

    async def _dense_query(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int,
        query_filter,
    ) -> list[dict[str, Any]]:
        """P1-01: dense query via ``query_points`` (qdrant-client 1.18).

        P0-05 BF-03: the vector is validated before dispatch (caller-side;
        ``retrieve`` validates prefetched vectors, ``_embed_texts`` validates
        freshly-embedded ones). Exceptions propagate to ``_dense_query_multi``
        so a degraded channel is observable — not swallowed into a silent [].
        """
        if not self._ensure_collection(collection_name):
            return []
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._client.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=limit,
                with_payload=True,
                query_filter=query_filter,
            ),
        )
        points = getattr(result, "points", None)
        if points is None and isinstance(result, list):  # tolerate a raw list return
            points = result
        return self._parse_query_result(points or [])

    @staticmethod
    def _dedupe(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """P1-01: content-deduplicate dense results across collections."""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for r in results:
            content = r.get("content", "")
            if content and content not in seen:
                seen.add(content)
                out.append(r)
        return out

    async def prefetch_reusable(self, query: str, llm: Any = None) -> dict:
        """P1-01: compute REUSABLE retrieval inputs for the graph prefetch.

        Returns ``{"rewritten_query", "embedding", "embedding_status"}`` — the
        canonical rewrite and the embedding vector. These are scene/collection
        AGNOSTIC (the embedding is of the canonical query, independent of
        which collection/scene the agent will target), so they are safe to
        compute before the agent is selected.

        This is NOT final evidence (spec step 18): no dense search, no BM25, no
        RRF, no rerank. retrieve() reuses these to avoid duplicate work, but
        still applies the request's scene/filter and runs the full pipeline.
        On embedding failure, ``embedding`` is None and ``embedding_status``
        records the state; retrieve() re-validates and degrades accordingly.
        """
        rewritten = await self._canonical_rewrite(query, llm)
        embedding: list[float] | None = None
        if not self.embedding_available:
            status = "unavailable"
        else:
            status = "available"
            try:
                loop = asyncio.get_running_loop()
                embedding = await loop.run_in_executor(
                    None, lambda: self._embed_texts([rewritten])[0]
                )
            except EmbeddingUnavailableError:
                embedding = None
                status = "unavailable"
            except EmbeddingDimensionError:
                embedding = None
                status = "invalid"
        return {
            "rewritten_query": rewritten,
            "embedding": embedding,
            "embedding_status": status,
        }

    def _ensure_bm25(self):
        """懒初始化 BM25 检索器对象。

        P1-02: 本方法只保证 **对象存在**，不等于 readiness READY。READY 只能
        由成功的 ``rebuild_bm25_from_qdrant`` 发布。初始化失败时进入 DEGRADED。
        """
        if self._bm25 is None:
            try:
                from rag.bm25_retriever import BM25Retriever

                self._bm25 = BM25Retriever()
                logger.info("BM25 检索器对象初始化完成（readiness 仍 UNINITIALIZED，待 rebuild）")
            except Exception as e:
                logger.warning(f"BM25 检索器初始化失败: {e}")
                self._bm25 = False
                self._hybrid_enabled = False
                self._bm25_readiness = BM25Readiness.DEGRADED
                self._bm25_meta = BM25IndexMeta(
                    index_version=0,
                    document_count=0,
                    last_build_at=0.0,
                    source_snapshot={},
                    status=BM25Readiness.DEGRADED,
                    reason="bm25_init_failed",
                )
        return self._bm25

    def bm25_readiness(self) -> BM25Readiness:
        """P1-02 BM25-2: 显式 readiness 状态（不等于 ``self._bm25 is not None``）。"""
        return self._bm25_readiness

    def bm25_index_meta(self) -> BM25IndexMeta | None:
        """P1-02 BM25-6: 当前 BM25 索引的可追踪 metadata。"""
        return self._bm25_meta

    def _on_bm25_mutation(self) -> None:
        """P1-02 BM25-8/15/16: 增量 add/delete 后维护 readiness + metadata。

        - 若当前 READY：保持 READY，bump index_version 与 mutations_since_build，
          更新 document_count（BM25 已与 Qdrant 同步）。
        - 若 DEGRADED：保持 DEGRADED（单次增量写入不能让已知不完整的索引变
          完整），仅记录 mutation，等待显式 rebuild。
        - 若 UNINITIALIZED：保持 UNINITIALIZED——增量写入不建立 READY 基线，
          非持久化模式的 READY 由容器在 seed 后调用 rebuild 建立。
        """
        if not isinstance(self._bm25, object) or self._bm25 is None or self._bm25 is False:
            return
        if self._bm25_readiness is BM25Readiness.READY:
            self._bm25_version_counter += 1
            doc_count = self._bm25.total_documents() if hasattr(self._bm25, "total_documents") else 0
            prev = self._bm25_meta
            self._bm25_meta = BM25IndexMeta(
                index_version=self._bm25_version_counter,
                document_count=doc_count,
                last_build_at=prev.last_build_at if prev else _now(),
                source_snapshot=prev.source_snapshot if prev else {},
                status=BM25Readiness.READY,
                reason="",
                skipped_invalid=prev.skipped_invalid if prev else 0,
                mutations_since_build=(prev.mutations_since_build + 1) if prev else 1,
            )

    def _mark_bm25_degraded(self, reason: str) -> BM25IndexMeta:
        """P1-02 BM25-4: publish a DEGRADED meta (rebuild failure / timeout).

        Preserves ``last_build_at`` and ``source_snapshot`` from the last
        *successful* build so traceability of the last good index survives a
        failure. ``last_build_at`` records the last **successful** build time,
        NOT the failure time — the failure reason/time live in ``reason`` and
        the ``bm25_rebuild_failures_total`` metric. Overwriting it with the
        failure time would erase "when was BM25 last known-good", which is
        exactly what an operator needs during a degradation incident.
        """
        prev = self._bm25_meta
        self._bm25_readiness = BM25Readiness.DEGRADED
        self._bm25_meta = BM25IndexMeta(
            index_version=self._bm25_version_counter,
            document_count=0,
            last_build_at=prev.last_build_at if prev else 0.0,
            source_snapshot=prev.source_snapshot if prev else {},
            status=BM25Readiness.DEGRADED,
            reason=reason,
        )
        return self._bm25_meta

    def supersede_bm25_rebuild(self, reason: str) -> None:
        """P1-02 BM25-11/17: externally supersede an in-flight rebuild.

        Called by the container when ``asyncio.wait_for`` times out — the
        rebuild runs in a ``ThreadPoolExecutor`` and a running task **cannot
        be cancelled**, so it keeps scrolling and would otherwise reach the
        "Success — atomic publication" path and flip DEGRADED back to READY
        after the container already started degraded. Bumping the build
        generation makes the in-flight rebuild's pre-publish check see a
        stale generation and refuse to publish its candidate.

        Does NOT take ``_bm25_lock``: the in-flight rebuild holds it while
        scrolling, and blocking the event loop on the lock would defeat the
        timeout. The generation bump and DEGRADED meta write are individual
        attribute writes (GIL-atomic); the rebuild reads the generation
        under the lock right before publishing, so the happens-before
        ordering is: supersede bumps gen → rebuild reads bumped gen → abort.
        """
        self._bm25_build_generation += 1
        bm25_rebuild_failures_total.labels(reason=reason).inc()
        logger.warning(
            f"BM25 rebuild 被外部 supersede（{reason}），in-flight rebuild 将被阻止发布，"
            "进入 DEGRADED"
        )
        self._mark_bm25_degraded(reason)

    @staticmethod
    def _unpack_scroll(result) -> tuple[list, object]:
        """Normalize a qdrant-client scroll return into (points, next_offset)."""
        if isinstance(result, tuple):
            return list(result[0] or []), result[1]
        points = getattr(result, "points", None) or []
        return list(points), getattr(result, "next_page_offset", None)

    def _scroll_collection_for_bm25(
        self, collection_name: str
    ) -> tuple[list[str], list[str], list[dict], int]:
        """Scroll one Qdrant collection and return validated BM25 inputs.

        P1-02 BM25-1/5/10/22: paginated scroll (not just the first page),
        payload validation (require logical ``doc_id`` + non-empty ``content``),
        and consumption of the P1-03 stable **logical** doc_id (never the
        storage Point ID, never Python hash). Invalid points are skipped and
        counted so the source↔index gap is explainable.
        """
        docs: list[str] = []
        ids: list[str] = []
        metadatas: list[dict] = []
        skipped = 0
        offset = None
        while True:
            result = self._client.scroll(
                collection_name=collection_name,
                limit=self._bm25_scroll_page,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, next_offset = self._unpack_scroll(result)
            for rec in points:
                payload = getattr(rec, "payload", None) or {}
                if not isinstance(payload, dict):
                    skipped += 1
                    continue
                doc_id = payload.get("doc_id")
                content = payload.get("content")
                if not isinstance(doc_id, str) or not doc_id or not isinstance(content, str) or not content.strip():
                    skipped += 1
                    continue
                meta = {k: v for k, v in payload.items() if k not in ("doc_id", "content")}
                docs.append(content)
                ids.append(doc_id)
                metadatas.append(meta)
            if not next_offset:
                break
            offset = next_offset
        return docs, ids, metadatas, skipped

    def rebuild_bm25_from_qdrant(
        self,
        collection_names: list[str],
        *,
        timeout: float | None = None,
    ) -> BM25IndexMeta:
        """P1-02: rebuild the in-memory BM25 index from persisted Qdrant data.

        Source of truth = Qdrant scroll. Does NOT depend on seed/add_documents
        having run in this process (BM25-1/12). Paginates every collection,
        validates payloads, builds a **candidate** BM25Retriever, reconciles
        the document count, and atomically swaps it into ``self._bm25`` only
        on full success (BM25-17/18: a partial index is never published).

        Outcomes:
          - READY + meta(version, document_count, last_build_at, source_snapshot,
            skipped_invalid) on success. An empty collection is READY with
            document_count=0 (BM25-9: legitimate, not a failure).
          - DEGRADED + meta(reason) + ``bm25_rebuild_failures_total`` metric +
            warning log on scroll failure (BM25-4) or deadline timeout (BM25-11).

        ``timeout`` (seconds) bounds the rebuild; None uses no deadline. The
        deadline is checked between collections AND right before publishing:
        if the rebuild ran past its deadline (or was superseded by a
        ``supersede_bm25_rebuild`` call — e.g. the container's
        ``asyncio.wait_for`` fired), the candidate is discarded and never
        swapped in, so a late/superseded result cannot flip DEGRADED to READY.
        """
        from rag.bm25_retriever import BM25Retriever

        # BM25-4: Qdrant unavailable cannot be rebuilt — fail honestly.
        if not self._available:
            bm25_rebuild_failures_total.labels(reason=REASON_QDRANT_UNAVAILABLE).inc()
            logger.warning("BM25 rebuild 跳过：Qdrant 不可用，进入 DEGRADED")
            return self._mark_bm25_degraded(REASON_QDRANT_UNAVAILABLE)

        deadline = (_now() + timeout) if (timeout is not None and timeout > 0) else None
        with self._bm25_lock:
            # P1-02 BM25-11/17: capture this rebuild's generation. A
            # supersede (container wait_for timeout) bumps
            # _bm25_build_generation; if it no longer equals my_gen at
            # publish time, this rebuild is stale and MUST NOT publish.
            my_gen = self._bm25_build_generation
            self._bm25_readiness = BM25Readiness.BUILDING
            candidate = BM25Retriever()
            skipped_invalid = 0
            per_collection: dict[str, dict[str, int]] = {}
            try:
                for coll in collection_names:
                    if deadline is not None and _now() > deadline:
                        bm25_rebuild_failures_total.labels(reason=REASON_REBUILD_TIMEOUT).inc()
                        logger.warning(
                            f"BM25 rebuild 超时（>{timeout}s），已扫描 {len(per_collection)} "
                            f"collections，进入 DEGRADED"
                        )
                        return self._mark_bm25_degraded(REASON_REBUILD_TIMEOUT)
                    if not self._ensure_collection(coll):
                        # A configured collection that cannot be opened must not
                        # be silently skipped: publishing READY would advertise
                        # a complete hybrid channel while an entire configured
                        # corpus is absent. Fail the rebuild as DEGRADED.
                        bm25_rebuild_failures_total.labels(
                            reason=REASON_REBUILD_SCROLL_FAILED
                        ).inc()
                        logger.warning(
                            f"BM25 rebuild 失败：collection `{coll}` 无法打开"
                            "（Qdrant 不可用 / 权限 / 建集合失败），进入 DEGRADED，"
                            "不发布 READY candidate"
                        )
                        return self._mark_bm25_degraded(REASON_REBUILD_SCROLL_FAILED)
                    docs, ids, metadatas, skipped = self._scroll_collection_for_bm25(coll)
                    skipped_invalid += skipped
                    if docs:
                        candidate.add_documents(
                            docs, collection=coll, ids=ids, metadatas=metadatas
                        )
                    per_collection[coll] = {
                        "point_count": self.get_collection_count(coll),
                        "doc_count": len(ids),
                    }
            except Exception as e:
                bm25_rebuild_failures_total.labels(reason=REASON_REBUILD_SCROLL_FAILED).inc()
                logger.warning(f"BM25 rebuild 失败（scroll 异常）：{e}", exc_info=True)
                # BM25-17/18: candidate is local — never published on failure.
                # The previous self._bm25 / readiness is superseded by DEGRADED.
                return self._mark_bm25_degraded(REASON_REBUILD_SCROLL_FAILED)

            # P1-02 BM25-11/17: pre-publish validation. Refuse to publish if
            # this rebuild was superseded (container wait_for timed out and
            # bumped the generation) OR it ran past its own deadline after
            # the last between-collections check. The candidate stays local —
            # never swapped in — so a late/superseded result cannot flip a
            # DEGRADED state back to READY.
            superseded = self._bm25_build_generation != my_gen
            self_deadlined = deadline is not None and _now() > deadline
            if superseded or self_deadlined:
                if not superseded:
                    # Self-deadlined with no external supersede: we are the
                    # authority and must mark DEGRADED ourselves.
                    bm25_rebuild_failures_total.labels(reason=REASON_REBUILD_TIMEOUT).inc()
                    logger.warning(
                        f"BM25 rebuild 超时（>{timeout}s），publish 前检测到超时，"
                        "candidate 丢弃，进入 DEGRADED"
                    )
                    self._mark_bm25_degraded(REASON_REBUILD_TIMEOUT)
                # If superseded, supersede_bm25_rebuild already marked DEGRADED.
                logger.info(
                    f"BM25 rebuild 未发布 candidate（superseded={superseded} "
                    f"self_deadlined={self_deadlined}），readiness={self._bm25_readiness.value}"
                )
                return self._bm25_meta or self._mark_bm25_degraded(REASON_REBUILD_TIMEOUT)

            # Success — atomic publication (BM25-18).
            self._bm25 = candidate
            self._bm25_version_counter += 1
            self._bm25_readiness = BM25Readiness.READY
            bm25_rebuilds_total.inc()
            self._bm25_meta = BM25IndexMeta(
                index_version=self._bm25_version_counter,
                document_count=candidate.total_documents(),
                last_build_at=_now(),
                source_snapshot={"collections": per_collection, "mode": "qdrant_scroll"},
                status=BM25Readiness.READY,
                reason="",
                skipped_invalid=skipped_invalid,
                mutations_since_build=0,
            )
            logger.info(
                f"BM25 rebuild 完成: docs={self._bm25_meta.document_count} "
                f"skipped_invalid={skipped_invalid} version={self._bm25_version_counter} "
                f"readiness=READY"
            )
            return self._bm25_meta

    @staticmethod
    def _metadata_matches_filters(
        metadata: dict[str, Any] | None,
        scene: str | None,
        metadata_filters: dict[str, Any] | None,
    ) -> bool:
        """Python equivalent of ``_build_retrieve_filter`` for BM25 candidates.

        Qdrant's ``MatchValue`` on a list field matches when the list contains
        the value, so list-valued payload fields (e.g. ``scene``) are handled
        the same way here.
        """
        if not scene and not metadata_filters:
            return True
        meta = metadata or {}

        def _matches(stored: Any, expected: Any) -> bool:
            if isinstance(stored, (list, tuple, set)):
                return expected in stored
            return stored == expected

        if scene and not _matches(meta.get("scene"), scene):
            return False
        if metadata_filters:
            for key, value in metadata_filters.items():
                if not _matches(meta.get(key), value):
                    return False
        return True

    def _bm25_search(
        self,
        query: str,
        collection_names: list[str],
        top_k: int = 8,
        scene: str | None = None,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索（同步，供 run_in_executor 调用）。

        P1-02 BM25-2: 仅当 readiness == READY 时返回结果；否则返回空列表，
        让 ``query_multiple`` 的 hybrid truthfulness 逻辑标注降级。

        P1-01 parity: the same ``scene`` / ``metadata_filters`` semantics that
        constrain the dense channel must constrain BM25 — otherwise an
        unfiltered lexical hit can enter RRF (or become the sole evidence when
        the vector channel is degraded) despite failing the request filter.
        """
        if self.bm25_readiness() is not BM25Readiness.READY:
            return []
        bm25 = self._bm25
        if not bm25:
            return []

        all_results: list[dict[str, Any]] = []
        for coll in collection_names:
            if bm25.collection_size(coll) == 0:
                continue
            try:
                results = bm25.search(query, top_k=top_k, collection=coll)
                all_results.extend(results)
            except Exception as e:
                logger.debug(f"BM25 检索失败 [{coll}]: {e}")

        if scene or metadata_filters:
            all_results = [
                r for r in all_results
                if self._metadata_matches_filters(
                    r.get("metadata"), scene, metadata_filters
                )
            ]
        all_results.sort(key=lambda r: r.get("bm25_score", 0), reverse=True)
        return all_results[:top_k]

    @staticmethod
    def _parse_query_result(result: list) -> list[dict[str, Any]]:
        docs = []
        for scored_point in result:
            payload = scored_point.payload or {}
            docs.append(
                {
                    "id": payload.get("doc_id", ""),
                    "content": payload.get("content", ""),
                    "metadata": {k: v for k, v in payload.items() if k not in ("doc_id", "content")},
                    "distance": 1.0 - scored_point.score,
                    "score": scored_point.score,
                }
            )
        return docs

    def _apply_reranker(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        if not results or len(results) <= 1:
            return results
        if self._reranker is None:
            try:
                from rag.reranker import create_reranker

                self._reranker = create_reranker()
                logger.info(f"Reranker 初始化完成: {type(self._reranker).__name__}")
            except Exception as e:
                logger.debug(f"Reranker 初始化失败: {e}")
                self._reranker = False
                return results
        if self._reranker is False:
            return results
        try:
            return cast(list[dict[str, Any]], self._reranker.rerank(query, results, top_k=top_k))
        except Exception as e:
            logger.warning(f"Rerank 失败，使用原始排序: {e}")
            return results

    async def rewrite_query(self, query: str, llm_client=None) -> str:
        if not llm_client:
            return query
        try:
            from langchain_core.messages import HumanMessage

            prompt = (
                "将以下用户问题改写为更适合知识库检索的形式。"
                "保留核心关键词，去除口语化表达和冗余词语，"
                "补充隐含的化妆品领域专业术语。"
                "只返回改写后的查询文本，不要解释。\n\n"
                f"用户问题：{query}\n改写查询："
            )
            result = await llm_client.async_invoke([HumanMessage(content=prompt)])
            rewritten = result.content.strip() if result and result.content else ""
            if rewritten and rewritten != query:
                logger.debug(f"Query Rewriting: '{query[:30]}' -> '{rewritten[:30]}'")
                return rewritten
            return query
        except Exception as e:
            logger.debug(f"Query Rewriting 失败，使用原查询: {e}")
            return query

    @staticmethod
    def simple_rerank(
        query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        if not results:
            return results
        try:
            import jieba

            def _tokenize(text: str) -> set[str]:
                return set(jieba.cut(text))
        except ImportError:
            import re as _re

            def _tokenize(text: str) -> set[str]:
                tokens = set(_re.findall(r"[a-z0-9]+", text.lower()))
                tokens.update(_re.findall(r"[一-鿿]", text))
                return tokens

        query_tokens = _tokenize(query)
        scored = []
        for r in results:
            content = r.get("content", "")
            content_tokens = _tokenize(content)
            overlap = len(query_tokens & content_tokens)
            distance_score = 1.0 / (1.0 + r.get("distance", 0))
            keyword_score = overlap / max(len(query_tokens), 1)
            combined = 0.6 * distance_score + 0.4 * keyword_score
            scored.append({**r, "_rerank_score": combined})

        scored.sort(key=lambda x: x.get("_rerank_score", 0), reverse=True)
        for s in scored:
            s.pop("_rerank_score", None)
        return scored[:top_k]

    @property
    def clip_available(self) -> bool:
        return self._clip_enabled
