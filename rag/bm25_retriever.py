"""
BM25 检索器（internal feature milestone: hybrid retrieval）
内存倒排索引，支持中文分词，用于混合检索的 BM25 词法通道。

（BM25Reranker 已移除——历史变更，生产环境始终使用 ApiReranker）

使用方式：
    retriever = BM25Retriever()
    retriever.add_documents(docs, collection="product_knowledge")
    results = retriever.search("烟酰胺 精华", top_k=3)
"""

import math
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from core.logger import get_logger

logger = get_logger("rag.bm25_retriever")


class BM25Retriever:
    """基于 BM25 的文档检索器，维护内存倒排索引。"""

    # BM25 参数（标准值）
    K1 = 1.5
    B = 0.75

    def __init__(self):
        # 索引结构：按 collection 分桶
        # _docs[collection] = [(doc_id, content, metadata), ...]
        self._docs: dict[str, list[tuple[str, str, dict]]] = {}

        # _term_doc_freq[collection][term] = 出现该 term 的文档数 (DF)
        self._term_doc_freq: dict[str, Counter] = {}

        # _doc_term_count[collection][doc_idx] = 该文档的 term 总数 (dl)
        self._doc_term_count: dict[str, list[int]] = {}

        # _doc_term_freq[collection][doc_idx][term] = 该文档中 term 出现次数 (TF)
        self._doc_term_freq: dict[str, list[Counter]] = {}

        # 文档总数（用于 IDF 计算）
        self._total_docs: dict[str, int] = {}

        # 平均文档长度（用于长度归一化）
        self._avg_dl: dict[str, float] = {}

    # ---- 公共 API ----

    def add_documents(
        self,
        documents: list[str],
        collection: str = "default",
        ids: list[str] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """批量添加文档到 BM25 索引。

        Args:
            documents: 文档内容列表
            collection: 所属集合名称
            ids: 文档 ID 列表（可选，自动生成）
            metadatas: 元数据列表（可选）
        """
        if not documents:
            return
        if ids is None:
            ids = [f"bm25_{collection}_{i}" for i in range(len(documents))]
        if metadatas is None:
            metadatas = [{}] * len(documents)

        if collection not in self._docs:
            self._docs[collection] = []
            self._term_doc_freq[collection] = Counter()
            self._doc_term_count[collection] = []
            self._doc_term_freq[collection] = []

        # Preserve historical behavior: if a caller passes a shorter ids /
        # metadatas list, the extra documents are silently truncated (explicit
        # strict=False rather than introducing a new runtime error).
        for doc_id, content, meta in zip(ids, documents, metadatas, strict=False):
            tokens = self._tokenize(content)
            if not tokens:
                continue
            self._docs[collection].append((doc_id, content, meta))

            # 当前文档的 term 频率
            tf_counter = Counter(tokens)
            self._doc_term_freq[collection].append(tf_counter)

            # 文档长度
            self._doc_term_count[collection].append(len(tokens))

            # DF：每个 term 首次在当前文档出现时才计数
            unique_terms = set(tokens)
            for t in unique_terms:
                self._term_doc_freq[collection][t] += 1

        # 更新总量统计
        self._total_docs[collection] = len(self._docs[collection])
        total_terms = sum(self._doc_term_count[collection])
        self._avg_dl[collection] = (
            total_terms / max(self._total_docs[collection], 1)
        )

        logger.debug(
            f"BM25 索引更新: collection={collection} "
            f"docs={self._total_docs[collection]} "
            f"avg_dl={self._avg_dl[collection]:.1f}"
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        collection: str | None = None,
        *,
        metadata_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索入口。

        Args:
            query: 查询文本
            top_k: 返回结果数
            collection: 限定 collection（None 则搜所有集合）
            metadata_filter: 可选的 payload 谓词。谓词在 **每个 collection
                截断到 top_k 之前** 应用，与 dense 通道的 filter-before-limit
                语义一致；否则高分但被过滤的文档会挤掉真正匹配的候选。

        Returns:
            结果列表，每项含 id/content/metadata/bm25_score
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        if collection is not None:
            collections = [collection] if collection in self._docs else []
        else:
            collections = list(self._docs.keys())

        if not collections:
            return []

        all_results: list[dict[str, Any]] = []
        for coll in collections:
            results = self._search_collection(
                query_tokens, coll, top_k, metadata_filter
            )
            # 标记来源 collection
            for r in results:
                r["_collection"] = coll
            all_results.extend(results)

        # 全局按 BM25 分数降序
        all_results.sort(key=lambda r: r.get("bm25_score", 0), reverse=True)
        return all_results[:top_k]

    def get_collection_names(self) -> list[str]:
        """返回已索引的 collection 列表"""
        return list(self._docs.keys())

    def collection_size(self, collection: str) -> int:
        """指定 collection 的文档数"""
        return self._total_docs.get(collection, 0)

    def total_documents(self) -> int:
        return sum(self._total_docs.values())

    def remove_documents(self, collection: str, ids: list[str]) -> int:
        if not ids or collection not in self._docs:
            return 0
        id_set = set(ids)
        docs = self._docs[collection]
        keep_idx = [i for i, (did, _, _) in enumerate(docs) if did not in id_set]
        removed = len(docs) - len(keep_idx)
        if removed == 0:
            return 0
        self._docs[collection] = [docs[i] for i in keep_idx]
        self._doc_term_count[collection] = [self._doc_term_count[collection][i] for i in keep_idx]
        self._doc_term_freq[collection] = [self._doc_term_freq[collection][i] for i in keep_idx]
        new_df: Counter = Counter()
        for tfc in self._doc_term_freq[collection]:
            for term in tfc:
                new_df[term] += 1
        self._term_doc_freq[collection] = new_df
        self._total_docs[collection] = len(self._docs[collection])
        total_terms = sum(self._doc_term_count[collection])
        self._avg_dl[collection] = total_terms / max(self._total_docs[collection], 1)
        return removed

    def upsert_documents(self, documents, collection="default", ids=None, metadatas=None) -> None:
        if not documents:
            return
        if ids is None:
            ids = [f"bm25_{collection}_{i}" for i in range(len(documents))]
        self.remove_documents(collection, ids)
        self.add_documents(documents, collection=collection, ids=ids, metadatas=metadatas)

    # ---- BM25 计算 ----

    def _search_collection(
        self,
        query_tokens: list[str],
        collection: str,
        top_k: int,
        metadata_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """在单个 collection 内执行 BM25 检索

        ``metadata_filter`` is applied to the scored candidates *before* the
        ``top_k`` truncation so a filtered-out high scorer cannot evict a valid
        lower-ranked document.
        """
        n_docs = self._total_docs.get(collection, 0)
        if n_docs == 0:
            return []
        avg_dl = self._avg_dl.get(collection, 1.0)
        df = self._term_doc_freq.get(collection, Counter())

        # 预计算 query 中每个 term 的 IDF
        idf_cache = {}
        for term in set(query_tokens):
            doc_freq = df.get(term, 0)
            # BM25 IDF 公式（平滑版）
            idf_cache[term] = math.log(
                (n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1
            )

        scored: list[tuple[float, int]] = []  # (score, doc_idx)

        for doc_idx in range(n_docs):
            dl = self._doc_term_count[collection][doc_idx]
            tf_counter = self._doc_term_freq[collection][doc_idx]

            score = 0.0
            for term in query_tokens:
                tf = tf_counter.get(term, 0)
                if tf == 0:
                    continue
                # BM25 TF 归一化
                tf_norm = (tf * (self.K1 + 1)) / (
                    tf + self.K1 * (1 - self.B + self.B * dl / max(avg_dl, 1))
                )
                score += idf_cache.get(term, 0) * tf_norm

            if score > 0:
                scored.append((score, doc_idx))

        # 按 BM25 得分降序排列
        scored.sort(key=lambda x: x[0], reverse=True)
        # Filter-before-truncation: a filtered-out high scorer must not consume
        # a top_k slot that belongs to a valid lower-ranked candidate.
        if metadata_filter is not None:
            scored = [
                (score, idx)
                for score, idx in scored
                if metadata_filter(self._docs[collection][idx][2] or {})
            ]
        scored = scored[:top_k]

        results = []
        for score, idx in scored:
            doc_id, content, meta = self._docs[collection][idx]
            results.append({
                "id": doc_id,
                "content": content,
                "metadata": meta,
                "bm25_score": round(score, 4),
            })
        return results

    # ---- 分词 ----

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文分词：优先 jieba，回退到按字切分"""
        text = text.lower().strip()
        if not text:
            return []

        try:
            import jieba

            tokens = list(jieba.cut(text))
            return [t for t in tokens if t.strip()]
        except ImportError:
            # 中文单字 + 英文单词/数字
            tokens = re.findall(r"[a-z0-9]+", text)
            tokens.extend(re.findall(r"[一-鿿]", text))
            return tokens
