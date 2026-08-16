"""
Qdrant 知识库管理器（v7.0）
基于向量检索的 RAG 检索增强生成（Qdrant 实现）。

v7.0 混合检索升级：
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
from typing import Any

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
    embedding_dimension_errors_total,
    embedding_provider_failures_total,
    retrieval_no_channel_total,
    vector_channel_disabled_total,
)
from rag.embedding_status import (
    EmbeddingDimensionError,
    EmbeddingStatus,
    EmbeddingUnavailableError,
    RetrievalResultList,
    degraded_reason_for,
    validate_embedding_vector,
)

logger = get_logger("rag.qdrant_knowledge_base")

# bge-large-zh-v1.5 输出维度
_EMBEDDING_DIM = 1024


# ===== v7.0: RRF 融合函数 =====


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
    for content, (score, doc) in sorted_items:
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
        self._reranker = None
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
        # v7.0: BM25 词法检索 + 混合检索开关
        self._bm25 = None
        self._hybrid_enabled = HYBRID_SEARCH_ENABLED

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
                self._ensure_bm25().add_documents(
                    documents,
                    collection=collection_name,
                    ids=ids,
                    metadatas=cleaned_metadatas,
                )
            return

        vectors = self._embed_texts(documents)

        points = [
            models.PointStruct(
                id=hash(id_) & 0x7FFFFFFFFFFFFFFF,
                vector=vector,
                payload={"doc_id": id_, "content": doc, **meta},
            )
            for id_, doc, vector, meta in zip(ids, documents, vectors, cleaned_metadatas)
        ]

        self._client.upsert(collection_name=collection_name, points=points)
        logger.debug(f"Collection '{collection_name}' 添加 {len(documents)} 条文档")

        # v7.0: 同步更新 BM25 索引
        if self._hybrid_enabled:
            self._ensure_bm25().add_documents(
                documents,
                collection=collection_name,
                ids=ids,
                metadatas=cleaned_metadatas,
            )

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
            return self._client.count(collection_name).count
        except Exception:
            return 0

    async def query_with_vector(
        self, collection_name: str, query_vector: list[float], n_results: int = 3
    ) -> list[dict[str, Any]]:
        """v6.3: 使用预计算的向量查询（避免 query_multiple 中重复 embedding）"""
        # P0-05 BF-03 (Phase 1 Gate re-review): validate the caller-supplied
        # precomputed vector BEFORE Qdrant dispatch — never send a
        # wrong-dimension / non-finite / non-numeric vector to Qdrant. The
        # internal caller (query_multiple) pre-validates via _embed_texts, so
        # this is a no-op on that path; it closes the external precomputed-vector
        # bypass. Invalid input raises EmbeddingDimensionError (consistent with
        # _embed_texts); the caller decides degrade/error handling.
        validate_embedding_vector(
            query_vector, _EMBEDDING_DIM, model=self._embedding_model_name
        )
        if not self._ensure_collection(collection_name):
            return []
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._client.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=n_results,
                    with_payload=True,
                    score_threshold=0.0,
                ),
            )
            return self._parse_query_result(result)
        except Exception as e:
            logger.error(f"Qdrant 查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query(
        self, collection_name: str, query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        if not self._ensure_collection(collection_name):
            return []
        # P0-05: disable the vector channel when embedding is unavailable — never
        # query Qdrant with a random/pseudo vector. query() is vector-only, so
        # there is no BM25 fallback here; return an explicit degraded empty result.
        if not self.embedding_available:
            vector_channel_disabled_total.inc()
            logger.warning(
                f"向量检索通道禁用（embedding 不可用），query 返回空降级结果: "
                f"collection={collection_name} model={self._embedding_model_name}"
            )
            return self._degraded_result(
                "embedding_unavailable", vector_used=False, lexical_used=False
            )
        try:
            loop = asyncio.get_running_loop()
            query_vector = self._embed_texts([query_text])[0]
            result = await loop.run_in_executor(
                None,
                lambda: self._client.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=n_results,
                    with_payload=True,
                    score_threshold=0.0,
                ),
            )
            return self._parse_query_result(result)
        except (EmbeddingUnavailableError, EmbeddingDimensionError) as e:
            self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
            vector_channel_disabled_total.inc()
            logger.warning(f"向量检索通道禁用（{degraded_reason_for(e)}）: {e}")
            return self._degraded_result(
                degraded_reason_for(e), vector_used=False, lexical_used=False
            )
        except Exception as e:
            logger.error(f"Qdrant 查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def search(
        self,
        query: str,
        top_k: int = 5,
        scene: str = None,
        filter_dict: dict = None,
        collection_name: str = "product_knowledge",
    ) -> list[dict]:
        """检索知识库，支持场景过滤（v6.1: 新增 scene/filter_dict 参数）。

        Args:
            query: 查询文本
            top_k: 返回结果数
            scene: 场景过滤（售前咨询/售后支持/技术答疑/投诉处理）
            filter_dict: 通用过滤条件
            collection_name: Qdrant 集合名
        Returns:
            标准化结果列表
        """
        if not self._ensure_collection(collection_name):
            return []
        # P0-05: disable the vector channel when embedding is unavailable.
        if not self.embedding_available:
            vector_channel_disabled_total.inc()
            logger.warning(
                f"向量检索通道禁用（embedding 不可用），search 返回空降级结果: "
                f"collection={collection_name} model={self._embedding_model_name}"
            )
            return self._degraded_result(
                "embedding_unavailable", vector_used=False, lexical_used=False
            )
        try:
            from qdrant_client.http.models import FieldCondition, Filter, MatchValue

            loop = asyncio.get_running_loop()
            query_vector = self._embed_texts([query])[0]

            # 构建过滤条件
            conditions = []
            if scene:
                conditions.append(FieldCondition(
                    key="scene", match=MatchValue(value=scene)
                ))
            if filter_dict:
                for key, value in filter_dict.items():
                    conditions.append(FieldCondition(
                        key=key, match=MatchValue(value=value)
                    ))

            query_filter = Filter(must=conditions) if conditions else None

            result = await loop.run_in_executor(
                None,
                lambda: self._client.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=top_k,
                    with_payload=True,
                    score_threshold=0.0,
                    query_filter=query_filter,
                ),
            )
            return self._parse_query_result(result)
        except (EmbeddingUnavailableError, EmbeddingDimensionError) as e:
            self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
            vector_channel_disabled_total.inc()
            logger.warning(f"向量检索通道禁用（{degraded_reason_for(e)}）: {e}")
            return self._degraded_result(
                degraded_reason_for(e), vector_used=False, lexical_used=False
            )
        except Exception as e:
            logger.error(f"Qdrant search 失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query_multiple(
        self, collection_names: list[str], query_text: str, n_results: int = 3
    ) -> RetrievalResultList:
        """v7.0: 混合检索 — 向量 + BM25 双通道并行 → RRF 融合 → 重排序

        P0-05: embedding 不可用时**禁用向量通道**（绝不生成随机向量查询
        Qdrant）。若 BM25 词法通道就绪，退化为 BM25-only 显式降级检索；
        若 BM25 也不可用，返回 no_retrieval_channel 降级空结果，由上层决定
        安全回答/拒绝/升级。降级元数据随 RetrievalResultList.meta 到达调用者。

        1. 同义词扩展（query rewrite）
        2. 向量检索 + BM25 检索并行执行（embedding 可用时）
        3. RRF 融合两个通道的结果
        4. bge-reranker-v2-m3 / BM25 二次重排
        """
        try:
            from rag.query_rewriter import QueryRewriter

            rewriter = QueryRewriter()
            query_text = rewriter.expand_query(query_text)
        except Exception:
            pass

        loop = asyncio.get_running_loop()

        # ---- BM25 词法通道（先调度，与向量通道并行；embedding 无关）----
        bm25_task = None
        if self._hybrid_enabled:
            bm25_top_k = max(n_results * 2, HYBRID_BM25_TOP_K)
            bm25_task = loop.run_in_executor(
                None,
                lambda: self._bm25_search(query_text, collection_names, top_k=bm25_top_k),
            )

        # ---- 向量检索通道（embedding 不可用时整体跳过，不查询 Qdrant）----
        vector_results_list: list[list[dict[str, Any]]] = []
        embedding_down = not self.embedding_available
        # Distinguish unavailable (no embed_fn / encode raised) from invalid
        # (wrong dim / non-finite / non-numeric) in the degraded metadata.
        embedding_down_reason = "embedding_unavailable"
        if not embedding_down:
            try:
                query_vector = await loop.run_in_executor(
                    None, lambda: self._embed_texts([query_text])[0]
                )
                vector_top_k = max(n_results * 2, HYBRID_VECTOR_TOP_K)
                vector_tasks = [
                    self.query_with_vector(name, query_vector, vector_top_k)
                    for name in collection_names
                ]
                vector_results_list = await asyncio.gather(*vector_tasks, return_exceptions=True)
            except (EmbeddingUnavailableError, EmbeddingDimensionError) as e:
                self._record_embedding_failure(getattr(e, "reason", "encode_failed"))
                embedding_down = True
                embedding_down_reason = degraded_reason_for(e)
                vector_results_list = []

        if embedding_down:
            vector_channel_disabled_total.inc()
            logger.warning(
                f"向量检索通道禁用（{embedding_down_reason}），降级为词法检索: "
                f"model={self._embedding_model_name}"
            )

        # ---- 收口 BM25 任务 ----
        bm25_results: list[dict[str, Any]] = []
        if bm25_task is not None:
            try:
                bm25_results = await bm25_task
            except Exception as e:
                logger.debug(f"BM25 检索异常: {e}")
                bm25_results = []

        # ---- 降级路径：向量通道禁用 → BM25-only 或无通道 ----
        if embedding_down:
            if bm25_results:
                bm25_fallback_used_total.inc()
                reranked = self._apply_reranker(query_text, bm25_results, top_k=n_results)
                return RetrievalResultList(
                    reranked[:n_results],
                    meta=self._degraded_meta(
                        embedding_down_reason, vector_used=False, lexical_used=True
                    ),
                )
            retrieval_no_channel_total.inc()
            logger.warning(
                "无可用检索通道（向量禁用 + BM25 空），返回降级空结果"
            )
            return RetrievalResultList(
                [],
                meta=self._degraded_meta(
                    "no_retrieval_channel", vector_used=False, lexical_used=False
                ),
            )

        # ---- 正常混合检索路径（embedding 可用）----
        all_vector_results: list[dict[str, Any]] = []
        for results in vector_results_list:
            if isinstance(results, list):
                all_vector_results.extend(results)
        all_vector_results.sort(key=lambda r: r.get("distance", 999))
        seen = set()
        deduped_vector = []
        for r in all_vector_results:
            content = r.get("content", "")
            if content not in seen:
                seen.add(content)
                deduped_vector.append(r)

        # ---- RRF 融合 ----
        channels = [deduped_vector]
        if bm25_results:
            channels.append(bm25_results)

        if len(channels) > 1:
            fused = rrf_fusion(channels, k=HYBRID_RRF_K)
            logger.debug(
                f"RRF 融合: vector={len(deduped_vector)} BM25={len(bm25_results)} "
                f"fused={len(fused)}"
            )
        else:
            fused = deduped_vector

        # ---- 重排序 ----
        reranked = self._apply_reranker(query_text, fused, top_k=n_results)
        return RetrievalResultList(
            reranked[:n_results],
            meta={
                "retrieval_degraded": False,
                "degraded_reason": "",
                "vector_channel_used": True,
                "lexical_channel_used": bool(bm25_results),
                "embedding_provider": self._embedding_provider,
                "embedding_model": self._embedding_model_name,
            },
        )

    def _ensure_bm25(self):
        """懒初始化 BM25 检索器"""
        if self._bm25 is None:
            try:
                from rag.bm25_retriever import BM25Retriever

                self._bm25 = BM25Retriever()
                logger.info("BM25 检索器初始化完成")
            except Exception as e:
                logger.warning(f"BM25 检索器初始化失败: {e}")
                self._bm25 = False
                self._hybrid_enabled = False
        return self._bm25

    def _bm25_search(
        self,
        query: str,
        collection_names: list[str],
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """BM25 检索（同步，供 run_in_executor 调用）"""
        bm25 = self._ensure_bm25()
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
            return self._reranker.rerank(query, results, top_k=top_k)
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
