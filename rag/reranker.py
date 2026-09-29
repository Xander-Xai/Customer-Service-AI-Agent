"""
RAG Reranker（v6.2）
对初始检索结果进行二次排序，提升相关性。

实现策略：
1. ApiReranker（HTTP API）— 调用重排序 API（SiliconFlow / OpenAI 兼容接口）
2. （BM25 词法重排器已移除——历史变更，生产环境始终使用 ApiReranker）

使用方式：
    reranker = create_reranker()
    reranked = reranker.rerank(query, results, top_k=3)
"""

from typing import Any

import httpx

from core.config import RERANKER_API_KEY, RERANKER_BASE_URL, RERANKER_MODEL
from core.logger import get_logger

logger = get_logger("rag.reranker")


class ApiReranker:
    """基于 HTTP API 的重排序（替代本地 CrossEncoder）"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        # v6.3: 区分显式传入空字符串（表示禁用）和未传入（使用默认配置）
        # api_key="" 时不应 fallback 到 RERANKER_API_KEY
        if api_key is None:
            self._api_key = RERANKER_API_KEY
        else:
            self._api_key = api_key
        self._model = model or RERANKER_MODEL
        self._base_url = (base_url or RERANKER_BASE_URL).rstrip("/")
        self._timeout = timeout

        if not self._api_key:
            logger.warning("RERANKER_API_KEY 未配置，API reranker 不可用")
            self._available = False
        else:
            self._available = True
            logger.info(f"API Reranker 初始化: {self._model} @ {self._base_url}")
        # 最近一次 API 失败的 HTTP 状态码（仅供 preflight/评测 gate 诊断，
        # 不含任何凭据材料；成功时不更新）
        self.last_error_status: int | None = None

    @property
    def available(self) -> bool:
        return self._available

    def rerank(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        """对检索结果进行 API 重排序"""
        if not self._available or not results:
            return results[:top_k]

        documents = [r.get("content", "") for r in results]

        url = f"{self._base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
        }

        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=self._timeout
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            self.last_error_status = None
            logger.warning(f"Reranker API 超时 ({self._timeout}s)，使用原始排序")
            return results[:top_k]
        except httpx.HTTPStatusError as e:
            self.last_error_status = e.response.status_code
            logger.warning(
                f"Reranker API HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
            return results[:top_k]
        except Exception as e:
            self.last_error_status = None
            logger.warning(f"Reranker API 调用失败: {e}")
            return results[:top_k]

        # 将分数附加到结果中，按分数降序排列
        for item in data.get("results", []):
            idx = item["index"]
            if idx < len(results):
                results[idx]["rerank_score"] = item.get("relevance_score", 0.0)

        results.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)
        return results[:top_k]


def create_reranker() -> ApiReranker:
    """创建 API Reranker（BM25 词法重排器已移除——历史变更，生产环境始终使用 API Reranker）

    Returns:
        ApiReranker 实例（API Key 未配置时 available=False，rerank 回退到原始顺序）
    """
    return ApiReranker()
