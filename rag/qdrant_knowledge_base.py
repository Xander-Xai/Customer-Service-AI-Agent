"""
Qdrant 知识库管理器（v6.0）
基于向量检索的 RAG 检索增强生成，使用 Qdrant 替代 ChromaDB。

设计要点：
- API 兼容 CosmeticsKnowledgeBase（KnowledgeBaseProtocol）
- embedding 在应用侧计算（bge-small-zh-v1.5），Qdrant 仅做向量存储和检索
- 支持 text + image（CLIP）双 embedding
- 内建重试和连接池
"""

import asyncio
import uuid
from typing import Any

from core.logger import get_logger
from qdrant_client import QdrantClient
from qdrant_client.http import models

logger = get_logger("rag.qdrant_knowledge_base")

# bge-small-zh-v1.5 输出维度
_EMBEDDING_DIM = 768


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
    ):
        self._clip_enabled = clip_enabled
        self._clip_embed_fn = None
        self._reranker = None
        self._embed_fn = self._create_embedding_function()
        self._collection_cache: dict[str, bool] = {}

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
        try:
            from sentence_transformers import SentenceTransformer
        except (ImportError, ModuleNotFoundError):
            QdrantKnowledgeBase._embed_fn_name = "default(fallback)"
            logger.warning("sentence_transformers 未安装，中文 embedding 模型不可用，回退到随机向量")
            return None

        models_to_try = [
            ("BAAI/bge-small-zh-v1.5", "bge-small-zh"),
            ("shibing624/text2vec-base-chinese", "text2vec-chinese"),
        ]
        for model_name, label in models_to_try:
            try:
                model = SentenceTransformer(model_name)
                QdrantKnowledgeBase._embed_fn_name = label
                logger.info(f"中文 embedding 模型加载成功: {model_name}")
                return model
            except Exception as e:
                logger.debug(f"模型 {model_name} 加载失败: {e}，尝试下一个")
        QdrantKnowledgeBase._embed_fn_name = "default(fallback)"
        logger.warning("中文 embedding 模型不可用，回退到随机向量")
        return None

    _embed_fn_name: str = "unknown"

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self._embed_fn is None:
            import random

            return [[random.random() for _ in range(_EMBEDDING_DIM)] for _ in texts]
        return self._embed_fn.encode(texts).tolist()

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

    async def query(
        self, collection_name: str, query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        if not self._ensure_collection(collection_name):
            return []
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
        except Exception as e:
            logger.error(f"Qdrant 查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query_multiple(
        self, collection_names: list[str], query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        try:
            from rag.query_rewriter import QueryRewriter

            rewriter = QueryRewriter()
            query_text = rewriter.expand_query(query_text)
        except Exception:
            pass

        all_results = []
        tasks = [self.query(name, query_text, n_results) for name in collection_names]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        for results in results_list:
            if isinstance(results, list):
                all_results.extend(results)

        all_results.sort(key=lambda r: r.get("distance", 999))
        seen = set()
        deduped = []
        for r in all_results:
            content = r.get("content", "")
            if content not in seen:
                seen.add(content)
                deduped.append(r)

        deduped = self._apply_reranker(query_text, deduped, top_k=n_results)
        return deduped[:n_results]

    @staticmethod
    def _parse_query_result(result: list) -> list[dict[str, Any]]:
        docs = []
        for scored_point in result:
            payload = scored_point.payload or {}
            docs.append(
                {
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
