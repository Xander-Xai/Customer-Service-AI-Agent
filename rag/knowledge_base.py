"""
ChromaDB 知识库管理器（v5.1）
基于向量检索的 RAG 检索增强生成。
v4.3: 更换为中文 embedding 模型（BAAI/bge-small-zh-v1.5），提升中文语义检索精度。
v5.1: 集成 CLIP 多模态 embedding，支持图片语义检索。
"""

import asyncio
import threading
import uuid
from typing import Any

from core.logger import get_logger

logger = get_logger("rag.knowledge_base")

# v5.4: 全局锁防止并发测试时 ChromaDB 租户冲突
_chromadb_lock = threading.Lock()


class CosmeticsKnowledgeBase:
    """化妆品领域知识库（基于 ChromaDB 向量检索）"""

    def __init__(self, persist_directory: str | None = None, clip_enabled: bool = False):
        """
        Args:
            persist_directory: 持久化目录。None 则使用内存模式（适合演示和测试）。
            clip_enabled: 是否启用 CLIP 多模态检索
        """
        self._clip_enabled = clip_enabled
        self._clip_embed_fn = None
        # v5.1: Reranker（延迟加载）
        self._reranker = None

        try:
            import chromadb
            from chromadb.utils import embedding_functions  # noqa: F401

            # v4.3: 使用中文 embedding 模型提升语义检索精度
            self._embed_fn = self._create_embedding_function()

            # v5.1: CLIP embedding（延迟加载）
            if clip_enabled:
                self._clip_embed_fn = self._create_clip_embedding_function()

            if persist_directory:
                self._client = chromadb.PersistentClient(path=persist_directory)
            else:
                # v5.3: 使用 EphemeralClient 避免持久化污染；显式禁用 telemetry 并允许 reset
                # v5.4: 加锁防止并发测试时 ChromaDB 租户冲突
                from chromadb.config import Settings

                with _chromadb_lock:
                    self._client = chromadb.EphemeralClient(
                        settings=Settings(
                            anonymized_telemetry=False,
                            allow_reset=True,
                            migrations="apply",
                        )
                    )
            self._collections: dict[str, Any] = {}
            self._available = True
            clip_info = (
                f", clip={'enabled' if self._clip_embed_fn else 'failed'}" if clip_enabled else ""
            )
            logger.info(
                f"ChromaDB 初始化成功 (persist={persist_directory}, embedding={self._embed_fn_name}{clip_info})"
            )
        except ImportError:
            self._available = False
            logger.warning("chromadb 未安装，RAG 功能不可用")
        except Exception as e:
            self._available = False
            logger.error(f"ChromaDB 初始化失败: {e}", exc_info=True)

    @staticmethod
    def _create_embedding_function():
        """v4.3: 创建中文 embedding 函数，按优先级尝试多种模型"""
        from chromadb.utils import embedding_functions

        # 优先级 1: BAAI/bge-small-zh-v1.5（专为中文优化的轻量模型）
        # 优先级 2: shibing624/text2vec-base-chinese（通用中文向量模型）
        # 优先级 3: ChromaDB 默认模型（fallback）
        models_to_try = [
            ("BAAI/bge-small-zh-v1.5", "bge-small-zh"),
            ("shibing624/text2vec-base-chinese", "text2vec-chinese"),
        ]
        for model_name, label in models_to_try:
            try:
                ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model_name)
                CosmeticsKnowledgeBase._embed_fn_name = label
                logger.info(f"中文 embedding 模型加载成功: {model_name}")
                return ef
            except Exception as e:
                logger.debug(f"模型 {model_name} 加载失败: {e}，尝试下一个")

        # Fallback: ChromaDB 默认（all-MiniLM-L6-v2）
        CosmeticsKnowledgeBase._embed_fn_name = "default(all-MiniLM-L6-v2)"
        logger.warning("中文 embedding 模型不可用，回退到 ChromaDB 默认模型")
        return embedding_functions.DefaultEmbeddingFunction()

    _embed_fn_name: str = "unknown"  # 类变量，记录实际使用的模型名

    @staticmethod
    def _create_clip_embedding_function():
        """v5.1: 创建 CLIP 多模态 embedding 函数"""
        try:
            from chromadb.utils.embedding_functions import OpenCLIPEmbeddingFunction

            ef = OpenCLIPEmbeddingFunction()
            logger.info("CLIP embedding 模型加载成功")
            return ef
        except Exception as e:
            logger.warning(f"CLIP embedding 加载失败: {e}，多模态检索不可用")
            return None

    @property
    def available(self) -> bool:
        return self._available

    def get_or_create_collection(self, name: str):
        """获取或创建一个 collection"""
        if not self._available:
            return None
        if name not in self._collections:
            kwargs = {"name": name}
            if hasattr(self, "_embed_fn") and self._embed_fn is not None:
                kwargs["embedding_function"] = self._embed_fn
            self._collections[name] = self._client.get_or_create_collection(**kwargs)
            count = self._collections[name].count()
            logger.debug(f"Collection '{name}' 已加载 ({count} docs)")
        return self._collections[name]

    def add_documents(
        self,
        collection_name: str,
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ):
        """
        向 collection 添加文档。
        如果未提供 ids，自动生成。
        注意：ChromaDB 不接受空 dict 作为 metadata，自动转为 None。
        """
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return
        if not documents:
            return
        if ids is None:
            existing = collection.count()
            ids = [f"{collection_name}_{existing + i}" for i in range(len(documents))]
        if metadatas is None:
            # ChromaDB 不接受空 dict metadata，传 None 让其自动处理
            metadatas = [{}] * len(documents)

        # ChromaDB 要求 metadata 非空 dict，将空 dict 替换为含默认值的 dict
        cleaned_metadatas = []
        for m in metadatas:
            if m:
                cleaned_metadatas.append(m)
            else:
                cleaned_metadatas.append({"_default": "true"})

        collection.add(documents=documents, metadatas=cleaned_metadatas, ids=ids)
        logger.debug(f"Collection '{collection_name}' 添加 {len(documents)} 条文档")

    def delete_documents(
        self,
        collection_name: str,
        ids: list[str],
    ):
        """从 collection 中删除指定 ID 的文档"""
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return
        if not ids:
            return
        collection.delete(ids=ids)
        logger.debug(f"Collection '{collection_name}' 删除 {len(ids)} 条文档")

    def seed_if_empty(self, collection_name: str, seed_fn):
        """仅在 collection 为空时执行种子函数（防止重复初始化）"""
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return
        if collection.count() == 0:
            seed_fn(self, collection_name=collection_name)
            logger.info(f"Collection '{collection_name}' 已种子初始化 ({collection.count()} docs)")

    async def query(
        self, collection_name: str, query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        异步查询单个 collection，返回匹配文档列表。
        每条结果: {"content": str, "metadata": dict, "distance": float}
        """
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return []
        if collection.count() == 0:
            return []
        try:
            loop = (
                asyncio.get_running_loop()
            )  # v3.8 fix: use get_running_loop (get_event_loop deprecated in 3.10+)
            result = await loop.run_in_executor(
                None, lambda: collection.query(query_texts=[query_text], n_results=n_results)
            )
            return self._parse_query_result(result)
        except Exception as e:
            logger.error(f"RAG 查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query_multiple(
        self, collection_names: list[str], query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        异步查询多个 collection，合并结果并按距离排序。
        v5.1: 集成 reranker 二次排序，提升相关性。
        每个 collection 取 top-n_results，合并后保留总 top-n_results。
        """
        # v5.1: 查询改写（同义词扩展）
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

        # 按距离排序（越小越相关），去重
        all_results.sort(key=lambda r: r.get("distance", 999))
        seen = set()
        deduped = []
        for r in all_results:
            content = r.get("content", "")
            if content not in seen:
                seen.add(content)
                deduped.append(r)

        # v5.1: Reranker 二次排序
        deduped = self._apply_reranker(query_text, deduped, top_k=n_results)
        return deduped[:n_results]

    def _apply_reranker(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        """v5.1: 应用 reranker 对结果二次排序"""
        if not results or len(results) <= 1:
            return results

        if self._reranker is None:
            try:
                from rag.reranker import create_reranker

                self._reranker = create_reranker()
                logger.info(f"Reranker 初始化完成: {type(self._reranker).__name__}")
            except Exception as e:
                logger.debug(f"Reranker 初始化失败: {e}")
                self._reranker = False  # 标记为不可用，避免重复尝试
                return results

        if self._reranker is False:
            return results

        try:
            return self._reranker.rerank(query, results, top_k=top_k)
        except Exception as e:
            logger.warning(f"Rerank 失败，使用原始排序: {e}")
            return results

    @staticmethod
    def _parse_query_result(result: dict) -> list[dict[str, Any]]:
        """解析 ChromaDB 查询结果"""
        docs = []
        if not result:
            return docs
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for i, doc in enumerate(documents):
            docs.append(
                {
                    "content": doc,
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 0.0,
                }
            )
        return docs

    def get_collection_count(self, collection_name: str) -> int:
        """获取 collection 中的文档数量"""
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return 0
        return collection.count()

    # ===== v5.1: CLIP 多模态检索 =====

    def get_or_create_image_collection(self, name: str = "image_knowledge"):
        """获取或创建 CLIP 图片 collection（使用 CLIP embedding）"""
        if not self._available or not self._clip_embed_fn:
            return None
        if name not in self._collections:
            self._collections[name] = self._client.get_or_create_collection(
                name=name,
                embedding_function=self._clip_embed_fn,
            )
        return self._collections[name]

    def add_image_documents(
        self,
        collection_name: str,
        image_paths: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ):
        """
        向图片 collection 添加图片文档

        Args:
            collection_name: collection 名称
            image_paths: 图片路径列表（本地路径或 URL）
            metadatas: 元数据列表
        """
        collection = self.get_or_create_image_collection(collection_name)
        if collection is None:
            return
        if not image_paths:
            return

        ids = [f"{collection_name}_{i}" for i in range(len(image_paths))]
        if metadatas is None:
            metadatas = [{"_default": "true"}] * len(image_paths)
        else:
            metadatas = [m if m else {"_default": "true"} for m in metadatas]

        # CLIP embedding 接受 URIs（图片路径）
        collection.add(uris=image_paths, metadatas=metadatas, ids=ids)
        logger.debug(f"图片 collection '{collection_name}' 添加 {len(image_paths)} 张图片")

    async def query_image(
        self, collection_name: str, query_text: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        用文本查询图片 collection（CLIP 跨模态检索）

        Args:
            collection_name: collection 名称
            query_text: 查询文本
            n_results: 返回结果数

        Returns:
            匹配的图片文档列表
        """
        collection = self.get_or_create_image_collection(collection_name)
        if collection is None or collection.count() == 0:
            return []
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: collection.query(query_texts=[query_text], n_results=n_results)
            )
            return self._parse_query_result(result)
        except Exception as e:
            logger.error(f"CLIP 图片查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query_image_by_uri(
        self, collection_name: str, query_image_uri: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        用图片查询图片 collection（CLIP 图片-图片检索）

        Args:
            collection_name: collection 名称
            query_image_uri: 查询图片路径
            n_results: 返回结果数

        Returns:
            匹配的图片文档列表
        """
        collection = self.get_or_create_image_collection(collection_name)
        if collection is None or collection.count() == 0:
            return []
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: collection.query(query_uris=[query_image_uri], n_results=n_results)
            )
            return self._parse_query_result(result)
        except Exception as e:
            logger.error(f"CLIP 图片-图片查询失败 [{collection_name}]: {e}", exc_info=True)
            return []

    async def query_multimodal(
        self,
        text_query: str,
        image_uri: str | None = None,
        collections: list[str] | None = None,
        n_results: int = 3,
    ) -> list[dict[str, Any]]:
        """
        v5.1: 多模态融合检索（文本 + 图片）
        同时查询文本 collection 和图片 collection，用 RRF 融合排序。

        Args:
            text_query: 文本查询
            image_uri: 可选图片 URI（用于图片-图片检索）
            collections: 文本 collection 列表（默认 product_knowledge, faq, tech_support）
            n_results: 返回结果数
        """
        if collections is None:
            collections = ["product_knowledge", "faq", "tech_support"]

        all_results = []

        # 文本检索（现有路径）
        text_results = await self.query_multiple(collections, text_query, n_results)
        for i, r in enumerate(text_results):
            r["_rank"] = i
            r["_source"] = "text"
        all_results.extend(text_results)

        # CLIP 图片检索
        if self._clip_enabled and self._clip_embed_fn:
            image_results = await self.query_image("image_knowledge", text_query, n_results)
            for i, r in enumerate(image_results):
                r["_rank"] = i
                r["_source"] = "clip_text"
            all_results.extend(image_results)

            # 图片-图片检索（如果提供了查询图片）
            if image_uri:
                img_results = await self.query_image_by_uri("image_knowledge", image_uri, n_results)
                for i, r in enumerate(img_results):
                    r["_rank"] = i
                    r["_source"] = "clip_image"
                all_results.extend(img_results)

        # RRF 融合排序
        return self._rrf_merge(all_results, n_results)

    @staticmethod
    def _rrf_merge(
        results: list[dict[str, Any]], n_results: int, k: int = 60
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion 融合排序"""
        # 按 source 分组
        groups = {}
        for r in results:
            source = r.get("_source", "text")
            groups.setdefault(source, []).append(r)

        # 计算 RRF 分数
        scored = {}
        for _source, items in groups.items():
            for rank, item in enumerate(items):
                content = item.get("content", "")
                key = content[:100]  # 用前 100 字符作为去重 key
                rrf_score = 1.0 / (k + rank + 1)
                if key in scored:
                    scored[key]["_rrf_score"] += rrf_score
                else:
                    scored[key] = {**item, "_rrf_score": rrf_score}

        # 按 RRF 分数排序
        merged = sorted(scored.values(), key=lambda x: x.get("_rrf_score", 0), reverse=True)
        # 清理内部字段
        for r in merged:
            r.pop("_rank", None)
            r.pop("_source", None)
            r.pop("_rrf_score", None)
        return merged[:n_results]

    @property
    def clip_available(self) -> bool:
        """CLIP 是否可用"""
        return self._clip_enabled and self._clip_embed_fn is not None

    # ===== v5.2: Query Rewriting + Reranker =====

    async def rewrite_query(self, query: str, llm_client=None) -> str:
        """
        v5.2: 用 LLM 将用户口语化查询改写为更适合向量检索的形式。
        保留核心关键词，去除口语化表达，补充隐含的领域术语。
        无 LLM 时自动降级为原查询。

        Args:
            query: 用户原始查询
            llm_client: LLM 客户端（需有 async_invoke 方法）

        Returns:
            改写后的查询，或原查询（降级时）
        """
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
        """
        v5.3: 基于关键词覆盖率的简单重排序（无需外部模型）。
        综合向量距离和关键词匹配度进行排序。

        Args:
            query: 用户查询
            results: 初步检索结果列表
            top_k: 返回结果数

        Returns:
            重排序后的结果列表
        """
        if not results:
            return results

        # v5.3: jieba 可用性只检查一次，失败后用 regex 分词兜底
        try:
            import jieba

            def _tokenize(text: str) -> set[str]:
                """jieba 中文分词"""
                return set(jieba.cut(text))
        except ImportError:
            import re as _re

            def _tokenize(text: str) -> set[str]:
                """regex 兜底分词：英文单词 + 单个中文字符"""
                tokens = set(_re.findall(r"[a-z0-9]+", text.lower()))
                tokens.update(_re.findall(r"[一-鿿]", text))
                return tokens

        query_tokens = _tokenize(query)

        scored = []
        for r in results:
            content = r.get("content", "")
            content_tokens = _tokenize(content)

            overlap = len(query_tokens & content_tokens)
            # 综合向量距离和关键词匹配
            distance_score = 1.0 / (1.0 + r.get("distance", 0))
            keyword_score = overlap / max(len(query_tokens), 1)
            combined = 0.6 * distance_score + 0.4 * keyword_score
            scored.append({**r, "_rerank_score": combined})

        scored.sort(key=lambda x: x.get("_rerank_score", 0), reverse=True)
        for s in scored:
            s.pop("_rerank_score", None)
        return scored[:top_k]
