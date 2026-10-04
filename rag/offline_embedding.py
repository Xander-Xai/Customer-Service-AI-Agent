"""OfflineEmbedding — 确定性、无网络的 embedding provider（测试 lane 用）。

为什么需要它
------------
``EMBEDDING_PROVIDER=api``（生产默认）会构造 :class:`rag.api_embedding.ApiEmbedding`，
向 ``EMBEDDING_BASE_URL`` 发真实 HTTP 请求。离线 lane（``.env.test`` / ``make test``）
如果也走这条路径，就会：

1. 需要出网 + 真实凭据 —— 而离线 lane 的凭据只能是占位符；
2. 拿到 401 后被 :meth:`rag.qdrant_knowledge_base.QdrantKnowledgeBase._embed_texts`
   转成 ``EmbeddingUnavailableError``（P0-05 契约：provider 失败**绝不伪造向量**），
   于是异常从 app lifespan 冒出来，测试挂在一个与被测行为无关的地方；
3. 即使不失败，provider 返回的向量也是不可复现的，测试 lane 谈不上 deterministic。

所以这里提供一个**显式配置**的离线 provider：同一段文本在任何进程、任何机器上
都得到同一组向量，不需要网络、不需要凭据。

它是什么 / 不是什么
--------------------
**是**：一个可复现的向量来源，让离线 lane 能真实跑完 embedding → Qdrant →
BM25 → retrieval → app lifespan 这条链路，并让「向量通道可用」这个前提为真。

**不是**：语义相似度模型。向量由文本的 SHA-256 计数器模式展开得到，相邻文本之间
**没有任何**语义关系。检索质量、Recall@K 之类的指标在 ``offline`` 下毫无意义 ——
评测必须用 ``EMBEDDING_PROVIDER=api`` + 真实 provider 跑。

它也**不是** P0-05 禁止的那种「失败时随便造个向量」：那是 provider 不可用时的
静默兜底，会把降级伪装成成功；这里是运维显式选择的 provider，
:func:`core.config.validate_embedding_provider` 在生产模式下直接拒绝它启动。

向量构造
--------
``sha256(text_utf8 || counter_be32)`` 逐块展开到 ``dim * 4`` 字节，按 ``<u4``
读成整数，映射到 ``[-1, 1)``，最后做 L2 归一化（Qdrant COSINE 距离要求非零向量）。

刻意**不用** ``random.Random(seed)`` 之类的 PRNG：CPython 的随机数实现细节不属于
语言规范，跨版本漂移会让「确定性」悄悄失效。SHA-256 由规范固定，跨进程、
跨平台、跨 Python 版本结果一致。
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from core.logger import get_logger

logger = get_logger("rag.offline_embedding")

#: 输出维度。必须与 ``rag.qdrant_knowledge_base._EMBEDDING_DIM``（= 1024，
#: bge-large-zh-v1.5）一致，否则 ``validate_embedding_vector`` 会拒收向量。
#: ``tests/unit/test_offline_embedding.py`` 里有一条契约测试盯住这个常量。
OFFLINE_EMBEDDING_DIM = 1024

#: 32 字节摘要 = 8 个 uint32 = 8 个 float32 lane
_LANES_PER_DIGEST = 8
_UINT32_SCALE = 4294967295.0  # 2**32 - 1


class OfflineEmbedding:
    """确定性、进程内、无网络的文本嵌入客户端。

    接口与 :class:`rag.api_embedding.ApiEmbedding` 对齐（``model`` / ``encode`` /
    ``aencode``），因此 :class:`rag.qdrant_knowledge_base.QdrantKnowledgeBase` 无需
    区分两者：

        >>> m = OfflineEmbedding()
        >>> m.encode("烟酰胺精华").shape
        (1024,)
        >>> m.encode(["a", "b"]).shape
        (2, 1024)
    """

    #: 供日志 / 观测面识别「这不是真实 provider」
    is_offline = True

    def __init__(
        self,
        dim: int = OFFLINE_EMBEDDING_DIM,
        *,
        model: str = "offline/deterministic",
    ) -> None:
        if dim <= 0:
            raise ValueError(f"OfflineEmbedding dim must be positive, got {dim}")
        self._dim = dim
        self._model = model
        logger.info(f"OfflineEmbedding 初始化（无网络）: dim={dim}")

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """同步编码。返回 ``(dim,)``（单个文本）或 ``(n, dim)``（文本列表）。"""
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        result = np.array([self._vector(t) for t in items], dtype=np.float32)
        return result[0] if single else result

    async def aencode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """异步编码（与 :meth:`ApiEmbedding.aencode` 同签名）。

        纯 CPU 计算，没有 IO 可等待；保留 async 只是为了不改动调用方。
        """
        return self.encode(texts, **kwargs)

    def _vector(self, text: str) -> np.ndarray:
        """单条文本 → 单位 L2 的 ``float32`` 向量（确定性）。"""
        payload = str(text).encode("utf-8")
        digest_bytes = self._dim * 4
        buf = bytearray()
        counter = 0
        while len(buf) < digest_bytes:
            buf += hashlib.sha256(payload + counter.to_bytes(4, "big")).digest()
            counter += 1

        lanes = np.frombuffer(bytes(buf[:digest_bytes]), dtype="<u4").astype(np.float64)
        vec = (lanes / _UINT32_SCALE) * 2.0 - 1.0  # [-1, 1)

        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            # 概率上不可达（需要 1024 个 uint32 全为同一中值），但零向量会被
            # Qdrant COSINE 距离拒绝。与其静默产出坏向量，不如给出确定的基向量。
            vec = np.zeros(self._dim, dtype=np.float64)
            vec[0] = 1.0
            return vec.astype(np.float32)

        return (vec / norm).astype(np.float32)
