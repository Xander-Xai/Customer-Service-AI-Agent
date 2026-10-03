"""
API Embedding 客户端（v6.2 → v6.3 性能优化）
替代本地 sentence-transformers，通过 HTTP API 调用嵌入模型。

支持 OpenAI 兼容的 Embedding API 格式（SiliconFlow / OpenAI / 等）。

v6.3 优化：
- 同步 httpx.post → 异步 httpx.AsyncClient + 连接池复用
- 超时从 30s 降至 10s（embedding 应远快于 completion）
- 保留同步 encode() 兼容旧调用方

HTTP client 所有权（两条路径各自独立，互不借用）：
- **异步 aencode()**：client 归属**当前运行的事件循环**。httpx 的 keep-alive
  连接池绑定创建它的 loop，跨 loop 复用会抛
  ``RuntimeError: Event loop is closed``（连接已关）或
  ``... is bound to a different event loop``（池里的 Event 已绑到别的 loop）。
  因此单例按归属 loop 分区，归属 loop 不再是当前 loop 时重建。
- **同步 encode()**：**不碰** async client。同步路径是 RAG 检索的主力入口 ——
  ``rag/qdrant_knowledge_base.py::_embed_texts`` 总是被
  ``loop.run_in_executor(None, ...)`` 派发，而 run_in_executor 给的是**没有
  运行 loop 的工作线程**。旧实现在这里 ``asyncio.run()`` 起一个一次性 loop、
  把全局 client 绑上去、再随 loop 关闭一起废掉，于是之后所有落在别的 loop
  上的调用都被毒化。现在同步路径用调用内自建自销的 ``httpx.Client``，生命周期
  完整落在单次调用内，不跨线程、不跨 loop、不留下任何需要清理的全局状态。

两条路径都**不吞异常**：transport 生命周期错误与 provider HTTP 错误
（401/500）各自按原类型上抛，交由调用方显式降级，不做静默兜底。
"""

import asyncio
import threading
from typing import Any

import httpx
import numpy as np

from core.logger import get_logger

logger = get_logger("rag.api_embedding")

# 模块级异步客户端单例（懒初始化，连接池复用），按归属事件循环分区。
# _async_client_loop 记录该 client 的连接池绑定在哪个 loop 上：httpx 不会因为
# loop 被销毁而把 client 标记为 is_closed，所以只有显式记住归属 loop 才能阻止
# 跨 loop 复用。
_async_client: httpx.AsyncClient | None = None
_async_client_loop: asyncio.AbstractEventLoop | None = None
_client_lock = threading.Lock()


def _new_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=3.0),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=5,
            keepalive_expiry=60.0,
        ),
    )


async def _get_async_client() -> httpx.AsyncClient:
    """获取当前运行事件循环自己的异步 HTTP 客户端（连接池复用，避免每次请求建连）。

    归属 loop 与当前运行 loop 不一致（含已销毁）时直接重建，绝不把上一个 loop
    的 client 交给当前 loop。丢弃的旧 client 不会被 ``aclose()``：它的 socket
    transport 记着那个已销毁的 loop，跨 loop 去 await ``aclose()`` 只会再抛一次
    ``RuntimeError: Event loop is closed``；此时它持有的连接早已随 loop 关闭，
    交由 GC 回收引用即可。
    """
    global _async_client, _async_client_loop
    running = asyncio.get_running_loop()
    with _client_lock:
        if (
            _async_client is not None
            and not _async_client.is_closed
            and _async_client_loop is running
        ):
            return _async_client
        previous_loop = _async_client_loop
        client = _new_async_client()
        _async_client = client
        _async_client_loop = running
    if previous_loop is not None and previous_loop is not running:
        logger.debug(
            "重建 embedding AsyncClient：归属事件循环已失效"
            f"（closed={previous_loop.is_closed()}），不复用其连接池"
        )
    return client


async def close_async_client() -> None:
    """关闭异步客户端（应用关闭时调用，可重复调用）。

    只 ``aclose()`` 归属当前运行 loop 的 client；归属 loop 已被销毁时直接清空
    引用 —— 强行 await ``aclose()`` 会顺着连接池去关 socket，而 socket 的
    transport 记着自己创建时的那个已销毁 loop，于是关闭路径本身会抛
    ``RuntimeError: Event loop is closed``。
    """
    global _async_client, _async_client_loop
    with _client_lock:
        client, owner_loop = _async_client, _async_client_loop
        _async_client = None
        _async_client_loop = None
    if client is None or client.is_closed:
        return
    if owner_loop is not asyncio.get_running_loop():
        logger.debug(
            "跳过 embedding AsyncClient 的 aclose：归属事件循环已失效"
            f"（closed={owner_loop.is_closed() if owner_loop is not None else None}）"
        )
        return
    await client.aclose()


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
        """同步编码文本为向量（兼容旧调用方，如 run_in_executor 派发的检索/导入）。

        走的是纯同步 httpx.Client（见 ``_sync_encode``），**不借用** aencode() 的
        模块级 async client：调用方线程通常没有运行中的事件循环（这正是
        ``run_in_executor`` / ``asyncio.to_thread`` 的形状），旧实现在这里
        ``asyncio.run()`` 起一次性 loop 会毒化那个全局 client。代价是同步路径
        不复用长连接；真正在长期存活的 loop 上跑批量的调用方应当直接用
        ``aencode()`` 以拿到连接池复用。
        """
        return self._sync_encode(texts, **kwargs)

    def _sync_encode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """纯同步实现：client 在单次调用内自建自销，不留任何跨调用状态。"""
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
