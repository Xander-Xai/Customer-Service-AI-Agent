"""
RAG Reranker（v5.1）
对初始检索结果进行二次排序，提升相关性。

实现策略：
1. CrossEncoder（sentence-transformers）— 精度最高，需要额外模型
2. BM25 关键词重叠 — 轻量 fallback，无额外依赖

使用方式：
    reranker = create_reranker()
    reranked = reranker.rerank(query, results, top_k=3)
"""

import math
import re
from collections import Counter
from typing import Any

from core.logger import get_logger

logger = get_logger("rag.reranker")


class CrossEncoderReranker:
    """基于 CrossEncoder 的精排（需要 sentence-transformers）"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base"):
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(model_name)
            self._available = True
            logger.info(f"CrossEncoder reranker 加载成功: {model_name}")
        except (ImportError, Exception) as e:
            self._model = None
            self._available = False
            logger.warning(f"CrossEncoder 不可用 ({e})，回退到 BM25 reranker")

    @property
    def available(self) -> bool:
        return self._available

    def rerank(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        """对检索结果进行 CrossEncoder 重排序"""
        if not self._available or not results:
            return results[:top_k]

        # 构造 query-document pairs
        pairs = [[query, r.get("content", "")] for r in results]
        scores = self._model.predict(pairs)

        # 将分数附加到结果中，按分数降序排列
        for i, score in enumerate(scores):
            results[i]["rerank_score"] = float(score)

        results.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)
        return results[:top_k]


class BM25Reranker:
    """基于 BM25 关键词重叠的轻量 reranker（无额外依赖）"""

    # BM25 参数
    K1 = 1.5
    B = 0.75

    def rerank(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        """使用 BM25 公式对结果重排序"""
        if not results:
            return results

        # 中文分词（简单按字/词切分）
        query_terms = self._tokenize(query)
        if not query_terms:
            return results[:top_k]

        # 计算文档平均长度
        doc_terms_list = [self._tokenize(r.get("content", "")) for r in results]
        avg_dl = sum(len(dt) for dt in doc_terms_list) / max(len(doc_terms_list), 1)

        # 计算 IDF
        n_docs = len(results)
        df: dict[str, int] = Counter()
        for dt in doc_terms_list:
            unique_terms = set(dt)
            for t in unique_terms:
                df[t] += 1

        idf = {}
        for term in set(query_terms):
            doc_freq = df.get(term, 0)
            idf[term] = math.log((n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1)

        # 计算每个文档的 BM25 分数
        for i, (result, doc_terms) in enumerate(zip(results, doc_terms_list)):
            score = 0.0
            dl = len(doc_terms)
            term_counts = Counter(doc_terms)
            for qt in query_terms:
                if qt not in term_counts:
                    continue
                tf = term_counts[qt]
                tf_norm = (tf * (self.K1 + 1)) / (tf + self.K1 * (1 - self.B + self.B * dl / max(avg_dl, 1)))
                score += idf.get(qt, 0) * tf_norm
            results[i]["rerank_score"] = score

        results.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)
        return results[:top_k]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文分词：优先使用 jieba，如果不可用则回退到按字切分"""
        text = text.lower()
        try:
            import jieba
            # 使用 jieba 进行精确模式分词
            tokens = list(jieba.cut(text))
            # 过滤掉空白符
            return [t for t in tokens if t.strip()]
        except ImportError:
            # 提取英文单词和数字
            tokens = re.findall(r"[a-z0-9]+", text)
            # 提取中文字符（每个字作为一个 token）
            chinese_chars = re.findall(r"[一-鿿]", text)
            tokens.extend(chinese_chars)
            return tokens


def create_reranker(prefer_cross_encoder: bool = True) -> CrossEncoderReranker | BM25Reranker:
    """工厂函数：创建最佳可用的 reranker

    Args:
        prefer_cross_encoder: 优先使用 CrossEncoder（需要 sentence-transformers）

    Returns:
        CrossEncoderReranker 或 BM25Reranker
    """
    if prefer_cross_encoder:
        reranker = CrossEncoderReranker()
        if reranker.available:
            return reranker
        logger.info("CrossEncoder 不可用，使用 BM25 reranker")

    return BM25Reranker()
