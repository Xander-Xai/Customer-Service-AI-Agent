"""
API Embedding 客户端（v6.2 → v6.3 性能优化）
替代本地 sentence-transformers，通过 HTTP API 调用嵌入模型。

支持 OpenAI 兼容的 Embedding API 格式（SiliconFlow / OpenAI / 等）。

v6.3 优化：
- 同步 httpx.post → 异步 httpx.AsyncClient + 连接池复用
- 超时从 30s 降至 10s（embedding 应远快于 completion）
- 保留同步 encode() 兼容旧调用方（内部用 run_in_executor 桥接）
"""

from typing import Any

import httpx
import numpy as np

from core.logger import get_logger

logger = get_logger("rag.api_embedding")

# 模块级异步客户端单例（懒初始化，连接池复用）
_async_client: httpx.AsyncClient | None = None


async def _get_async_client() -> httpx.AsyncClient:
    """获取或创建异步 HTTP 客户端单例（连接池复用，避免每次请求建连）"""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
                keepalive_expiry=60.0,
            ),
        )
    return _async_client


async def close_async_client() -> None:
    """关闭异步客户端（应用关闭时调用）"""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
    _async_client = None


class ApiEmbedding:
    """基于 HTTP API 的文本嵌入客户端，接口兼容 SentenceTransformer.encode()。

    使用方式:
        model = ApiEmbedding(api_key="sk-xxx", model="BAAI/bge-large-zh-v1.5")
        vector = model.encode("查询文本")          # 返回 ndarray (dim,)
        vectors = model.encode(["文本1", "文本2"])  # 返回 ndarray (n, dim)
        vector = await model.aencode("查询文本")    # 异步版本，不阻塞事件循环
    """

    def __init__(
        self,
        api_key: str,
        model: str = "BAAI/bge-large-zh-v1.5",
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout: float = 10.0,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        logger.info(
            f"API Embedding 客户端初始化: {model} @ {base_url} (timeout={timeout}s)"
        )

    @property
    def model(self) -> str:
        return self._model

    async def aencode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """异步编码文本为向量（不阻塞事件循环，推荐在 async 上下文中使用）。

        Args:
            texts: 单个字符串或字符串列表
            **kwargs: 兼容 SentenceTransformer.encode() 的额外参数（忽略）

        Returns:
            单个文本: ndarray 形状 (dim,)
            多个文本: ndarray 形状 (n, dim)
        """
        single = isinstance(texts, str)
        if single:
            texts = [texts]

        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "input": texts,
            "encoding_format": "float",
        }

        try:
            client = await _get_async_client()
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.error(f"Embedding API 超时 ({self._timeout}s): {self._model}")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Embedding API HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
            raise
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            raise

        # 按 index 排序提取 embedding
        embeddings = [
            item["embedding"]
            for item in sorted(data["data"], key=lambda x: x["index"])
        ]

        result = np.array(embeddings, dtype=np.float32)
        if single:
            return result[0]  # 返回 1D
        return result  # 返回 2D

    def encode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """同步编码文本为向量（兼容旧调用方，内部桥接到异步实现）。

        对于已有 async 上下文的调用方，推荐使用 aencode() 避免线程池开销。
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 已在 async 上下文中（如被 run_in_executor 调用），直接用同步 httpx
            return self._sync_encode(texts, **kwargs)

        # 无运行中的事件循环，直接 await
        return asyncio.run(self.aencode(texts, **kwargs))

    def _sync_encode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """纯同步 fallback（仅在已有事件循环的线程池中使用）"""
        single = isinstance(texts, str)
        if single:
            texts = [texts]

        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "input": texts,
            "encoding_format": "float",
        }

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException:
            logger.error(f"Embedding API 超时 ({self._timeout}s): {self._model}")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Embedding API HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
            raise
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            raise

        embeddings = [
            item["embedding"]
            for item in sorted(data["data"], key=lambda x: x["index"])
        ]

        result = np.array(embeddings, dtype=np.float32)
        if single:
            return result[0]
        return result
