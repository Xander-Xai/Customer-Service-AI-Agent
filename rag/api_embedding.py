"""
API Embedding 客户端（v6.2 → v6.3 性能优化）
替代本地 sentence-transformers，通过 HTTP API 调用嵌入模型。

支持 OpenAI 兼容的 Embedding API 格式（SiliconFlow / OpenAI / 等）。

v6.3 优化：
- 同步 httpx.post → 异步 httpx.AsyncClient + 连接池复用
- 超时从 30s 降至 10s（embedding 应远快于 completion）
- 保留同步 encode() 兼容旧调用方

HTTP client 所有权（两条路径各自独立，互不借用）：
- **异步 aencode()**：client 归属**当前运行的事件循环**，按 loop 分区存放
  （``_clients_by_loop``）。httpx 的 keep-alive 连接池绑定创建它的 loop，跨 loop
  复用会抛 ``RuntimeError: Event loop is closed``（连接已关）或
  ``... is bound to a different event loop``（池里的 Event 已绑到别的 loop）。
  httpx 不会因为 loop 被销毁就把 client 标记为 ``is_closed``，所以分区键必须是
  loop 本身：**每个存活 loop 各持有一个 client，A→B→A 的交替不会顶掉 A 的
  client，也不会重复建 client**。分区表按 loop 分片而不是「最近一次使用者」，
  正是为了让同时存活的多条 loop 各自保住连接池。
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
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from core.logger import get_logger

logger = get_logger("rag.api_embedding")

# 归属 loop → 该 loop 自己的 AsyncClient（懒初始化，连接池复用）。
#
# 用强引用作 key 是刻意的：httpx 的 keep-alive 连接持有 socket transport，而
# transport 反过来强引用创建它的 loop，所以「弱 key + GC 自动回收」这条路走不通
# —— 值把键钉住了，条目永远等不到 GC（WeakKeyDictionary 在这里不会触发）。
# 回收改由 ``_drop_dead_owners_locked()`` 显式做：丢弃 client 引用才真正断开
# client → transport → loop 这条引用链。回收是惰性的 —— 每次
# ``_get_async_client()`` / ``close_async_client()`` 都会顺带清掉已销毁归属，
# 于是分区表规模的上界是「存活 loop 数 + 上次访问后新死的 loop 数」，不会随
# 一次性 loop 的调用次数无界增长。
_registry_lock = threading.Lock()
_clients_by_loop: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


@dataclass(frozen=True, slots=True)
class AsyncClientCloseReport:
    """``close_async_client()`` 的诚实回执：说清楚关了什么、没关什么。

    - ``closed``：调用 loop 自己的 client 是否被 ``aclose()`` 了。
    - ``dropped_dead_owners``：归属 loop 已销毁、只能丢弃引用（无法安全关闭）
      的条目数。
    - ``foreign_live_owners``：归属**其他存活 loop**、既没关闭也没摘除的条目数。
      它们仍然可用，需要关闭时必须在各自的 owning loop 上调用本函数。
    """

    closed: bool
    dropped_dead_owners: int
    foreign_live_owners: int


def _new_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=3.0),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=5,
            keepalive_expiry=60.0,
        ),
    )


def _drop_dead_owners_locked() -> int:
    """丢弃归属 loop 已销毁的条目，返回丢弃条数。必须在持锁状态下调用。

    这些 client 不 ``aclose()``：它们的 socket transport 记着那个已销毁的 loop，
    跨 loop 去 await ``aclose()`` 只会再抛一次 ``RuntimeError: Event loop is
    closed``。此时连接早已随 loop 关闭，丢弃引用即完成回收。
    """
    if not _clients_by_loop:
        return 0
    dead = [loop for loop in _clients_by_loop if loop.is_closed()]
    for loop in dead:
        del _clients_by_loop[loop]
    return len(dead)


async def _get_async_client() -> httpx.AsyncClient:
    """获取当前运行事件循环自己的异步 HTTP 客户端（连接池复用，避免每次请求建连）。

    分区规则：client 以**归属 loop** 为键。连续调用复用同一个 client；另一条
    存活 loop 上的调用只会新建/复用它自己的条目，绝不顶掉本 loop 的 client。
    已经 ``is_closed`` 的条目（被调用方显式关过）按新建处理。

    锁只在纯同步的 dict 操作上持有，``threading.Lock`` 不跨任何 ``await``；
    ``httpx.AsyncClient()`` 的构造不接触事件循环，因此可以留在临界区内，让
    「每个 loop 恰好一个 client」在并发首调下也成立。
    """
    running = asyncio.get_running_loop()
    with _registry_lock:
        dropped = _drop_dead_owners_locked()
        existing = _clients_by_loop.get(running)
        if existing is None or existing.is_closed:
            client = _new_async_client()
            _clients_by_loop[running] = client
        else:
            client = existing
    if dropped:
        logger.debug(f"丢弃 {dropped} 个归属事件循环已销毁的 embedding AsyncClient")
    return client


def async_client_registry_snapshot() -> tuple[
    tuple[asyncio.AbstractEventLoop, httpx.AsyncClient], ...
]:
    """只读快照：分区表当前持有的 (归属 loop, client) 对。诊断与测试用。"""
    with _registry_lock:
        return tuple(_clients_by_loop.items())


async def close_async_client() -> AsyncClientCloseReport:
    """关闭**调用 loop 自己**的异步客户端（可重复调用）。

    契约（刻意不做「全局关闭」，因为跨 loop 关闭在物理上不成立）：

    - 只在**归属 loop 就是当前运行 loop** 时 ``await client.aclose()``；绝不对
      其他 loop 的 client 发起 ``aclose()`` —— 顺着连接池去关 socket，socket 的
      transport 记着自己创建时的 loop，在别的 loop 上 await 会把应用关闭 / 测试
      清理变成 ``RuntimeError: Event loop is closed`` 崩溃。
    - 归属 loop 已销毁的条目：只丢弃引用（连接已随 loop 死），计入
      ``dropped_dead_owners``。
    - 归属**其他存活 loop** 的条目：既不关闭也不摘除，计入
      ``foreign_live_owners``，它们之后仍可继续复用 —— 摘掉会逼那条 loop 重建
      连接池。要关它们，必须在各自的 owning loop 上调用本函数。
    """
    running = asyncio.get_running_loop()
    with _registry_lock:
        dropped = _drop_dead_owners_locked()
        client: httpx.AsyncClient | None = _clients_by_loop.pop(running, None)
        foreign = len(_clients_by_loop)
    closed = client is not None and not client.is_closed
    if client is not None and closed:
        await client.aclose()
    if dropped or foreign:
        logger.debug(
            f"embedding AsyncClient 关闭回执：closed={closed}"
            f"，丢弃已销毁归属 {dropped} 个，未关闭的其他存活归属 {foreign} 个"
        )
    return AsyncClientCloseReport(
        closed=closed,
        dropped_dead_owners=dropped,
        foreign_live_owners=foreign,
    )


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
