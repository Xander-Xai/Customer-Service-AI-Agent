"""
ChromaDB 知识库管理器（v4.3）
基于向量检索的 RAG 检索增强生成。
v4.3: 更换为中文 embedding 模型（BAAI/bge-small-zh-v1.5），提升中文语义检索精度。
"""
import asyncio
from typing import Any, Dict, List, Optional
from logger import get_logger

logger = get_logger("rag.knowledge_base")


class CosmeticsKnowledgeBase:
    """化妆品领域知识库（基于 ChromaDB 向量检索）"""

    def __init__(self, persist_directory: Optional[str] = None):
        """
        Args:
            persist_directory: 持久化目录。None 则使用内存模式（适合演示和测试）。
        """
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            # v4.3: 使用中文 embedding 模型提升语义检索精度
            self._embed_fn = self._create_embedding_function()

            if persist_directory:
                self._client = chromadb.PersistentClient(path=persist_directory)
            else:
                self._client = chromadb.Client()
            self._collections: Dict[str, Any] = {}
            self._available = True
            logger.info(f"ChromaDB 初始化成功 (persist={persist_directory}, embedding={self._embed_fn_name})")
        except ImportError:
            self._available = False
            logger.warning("chromadb 未安装，RAG 功能不可用")
        except Exception as e:
            self._available = False
            logger.error(f"ChromaDB 初始化失败: {e}")

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
                ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=model_name
                )
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

    @property
    def available(self) -> bool:
        return self._available

    def get_or_create_collection(self, name: str):
        """获取或创建一个 collection"""
        if not self._available:
            return None
        if name not in self._collections:
            kwargs = {"name": name}
            if hasattr(self, '_embed_fn') and self._embed_fn is not None:
                kwargs["embedding_function"] = self._embed_fn
            self._collections[name] = self._client.get_or_create_collection(**kwargs)
            count = self._collections[name].count()
            logger.debug(f"Collection '{name}' 已加载 ({count} docs)")
        return self._collections[name]

    def add_documents(self, collection_name: str,
                      documents: List[str],
                      metadatas: Optional[List[Dict[str, Any]]] = None,
                      ids: Optional[List[str]] = None):
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

    def seed_if_empty(self, collection_name: str, seed_fn):
        """仅在 collection 为空时执行种子函数（防止重复初始化）"""
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return
        if collection.count() == 0:
            seed_fn(self, collection_name=collection_name)
            logger.info(f"Collection '{collection_name}' 已种子初始化 ({collection.count()} docs)")

    async def query(self, collection_name: str, query_text: str,
                    n_results: int = 3) -> List[Dict[str, Any]]:
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
            loop = asyncio.get_running_loop()  # v3.8 fix: use get_running_loop (get_event_loop deprecated in 3.10+)
            result = await loop.run_in_executor(
                None,
                lambda: collection.query(query_texts=[query_text], n_results=n_results)
            )
            return self._parse_query_result(result)
        except Exception as e:
            logger.error(f"RAG 查询失败 [{collection_name}]: {e}")
            return []

    async def query_multiple(self, collection_names: List[str], query_text: str,
                             n_results: int = 3) -> List[Dict[str, Any]]:
        """
        异步查询多个 collection，合并结果并按距离排序。
        每个 collection 取 top-n_results，合并后保留总 top-n_results。
        """
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
        return deduped[:n_results]

    @staticmethod
    def _parse_query_result(result: dict) -> List[Dict[str, Any]]:
        """解析 ChromaDB 查询结果"""
        docs = []
        if not result:
            return docs
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for i, doc in enumerate(documents):
            docs.append({
                "content": doc,
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "distance": distances[i] if i < len(distances) else 0.0,
            })
        return docs

    def get_collection_count(self, collection_name: str) -> int:
        """获取 collection 中的文档数量"""
        collection = self.get_or_create_collection(collection_name)
        if collection is None:
            return 0
        return collection.count()
