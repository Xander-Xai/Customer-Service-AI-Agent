"""进程内确定性 RAG provider（issue #52）—— 零网络、零凭据。

存在的理由
----------
仓库 README 承诺"全量测试离线运行，无需 API Key"。在此之前，这条承诺是不成立的：
默认 lane（``.env.test`` → ``make test``）带着**占位**凭据
（``EMBEDDING_API_KEY=sk-placeholder-embedding-test-key``）构造了真实的
``ApiEmbedding``，第一次用向量通道时就会 POST
``https://api.siliconflow.cn/v1/embeddings`` 并收到 401
（实测单次约 40s）。这让默认测试套件既需要出网、又需要真实凭据。

本模块提供同一套契约的**进程内实现**，让默认 lane 在完全不碰网络的前提下，
仍然能跑通 embedding / rerank 的**真实逻辑链路**。

它是什么，不是什么
------------------
``LocalEmbedding`` 是**词法哈希投影**（signed hashing trick + L2 归一化），
**不是**神经网络嵌入：

  - 它**不是** bge-large-zh-v1.5，也没有任何语义泛化能力。同义改写、跨语言、
    隐含语义这些真实 embedding 才有的性质，在这里都不成立。
  - 它**确实**是一个单调、可复现的相似度函数：两段文本共享的 token 越多，
    余弦相似度越高。这足以让"向量通道真的参与了检索"这件事被真实地验证到 ——
    写库、维度校验、Qdrant 检索、RRF 融合、重排、降级元数据全部是生产代码路径。

所以本模块的定位是**测试替身 / 离线 lane 的 provider**，不是生产默认实现。
生产的 embedding 质量没有被、也不应该被这里替代。

确定性
------
用 ``hashlib.blake2b`` 而**不是** Python 内置 ``hash()``：后者对 ``str`` 加了
每进程随机盐，跨进程就不一致了，而跨进程一致正是这里的前提（Qdrant 里的向量由
上一个进程写入，这个进程要能对上）。同样的输入在任何机器、任何时间都得到同样的
向量。

维度契约
--------
输出长度恒等于 ``EMBEDDING_DIM``，且所有元素有限。调用方
（``rag/embedding_status.validate_embedding_vector``、
``cache/response_cache._embed_query``）会独立再校验一次，这里不依赖"下游会兜底"。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from core.logger import get_logger

logger = get_logger("rag.local_provider")

#: 出现在 :data:`LocalEmbedding.model` 里的人类可读名。会出现在 retrieval trace /
#: 指标标签里，所以必须稳定且不含路径或机器信息。
LOCAL_EMBEDDING_MODEL_NAME = "local-hashed-lexical-v1"
LOCAL_RERANKER_MODEL_NAME = "local-lexical-rerank-v1"

#: 文本里既没有 ASCII 字母也没有 CJK 字符时，用它兜底，保证向量永远非零
#: （Qdrant 的 cosine 距离对零向量无定义，而下游校验只拦非有限值、不拦零向量）。
_EMPTY_TOKEN = "<empty>"

#: CJK 表意文字区间（含扩展 A 与兼容区），这些字符没有空格分词，按字/双字切。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
)

#: 拉丁字母/数字/常见符号，作为词级 token。
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: CJK 连续片段。``\U`` 转义在正则里必须补齐 8 位十六进制，所以用 ``:08X``。
_CJK_RUN_RE = re.compile("[" + "".join(f"\\U{lo:08X}-\\U{hi:08X}" for lo, hi in _CJK_RANGES) + "]+")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """把文本切成确定性 token 序列。

    切法（顺序会影响向量，必须固定）：

    1. NFKC 归一化 + casefold —— 让全角/半角、大小写差异归一。
    2. CJK 连续片段：同时产出**字级**与**双字（bigram）** token。双字是必要的，
       因为中文单字歧义太大（"银行"/"行走"），只靠单字会让不相关文本也算得高分。
    3. 拉丁/数字：产出词级 token。

    返回的 token 列表允许重复（是 bag，不是 set）—— 词频信息要保留。
    """
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    tokens: list[str] = []

    # CJK：字 + 双字
    for run in _CJK_RUN_RE.findall(normalized):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    # 拉丁字母 / 数字
    tokens.extend(_LATIN_TOKEN_RE.findall(normalized))

    return tokens


def _hash_token(token: str, dim: int) -> tuple[int, float]:
    """把 token 映射成 ``(维度下标, ±1 符号)``。

    用 blake2b 而不是内置 ``hash()``：``hash(str)`` 每进程加随机盐，跨进程不一致，
    而"上个进程写入 Qdrant 的向量，本进程要能检索出来"要求跨进程一致。
    8 字节摘要提供足够的下标与符号熵；下标取模到 ``dim``，因此 ``dim`` 不必是
    2 的幂也成立。
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value & 1) else -1.0


class LocalEmbedding:
    """确定性词法嵌入，接口与 :class:`rag.api_embedding.ApiEmbedding` 一致。

    兼容点（调用方依赖的）：
      - ``model`` 属性（检索 trace / 指标标签会读）
      - ``encode(str) -> ndarray(dim,)`` 与 ``encode(list[str]) -> ndarray(n, dim)``
      - ``aencode`` 异步版本

    刻意的**不**兼容：不做归一化前的 ``normalize_embeddings`` 参数处理，也不接受
    SentenceTransformer 的其它 kwargs —— 这里的调用方是 RAG 检索，不接受那些参数，
    接住它们只会掩盖错误。
    """

    def __init__(self, dim: int = 1024) -> None:
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError(f"LocalEmbedding dim must be a positive int, got {dim!r}")
        self._dim = dim
        logger.info(f"本地确定性 Embedding 初始化: {LOCAL_EMBEDDING_MODEL_NAME} (dim={dim})")

    @property
    def model(self) -> str:
        return LOCAL_EMBEDDING_MODEL_NAME

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """编码为 L2 归一化的稠密向量。

        Args:
            texts: 单串或字符串列表。
            **kwargs: 接受并忽略（只为与 SentenceTransformer 风格的调用方兼容）。

        Returns:
            单串 → 形状 ``(dim,)``；列表 → 形状 ``(n, dim)``。

        Raises:
            TypeError: 元素不是字符串（不做静默 ``str()`` 转换 —— 静默转换会把
                ``None`` 变成 ``"None"`` 并伪造出一个有意义的向量）。
        """
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)

        vectors = np.zeros((len(items), self._dim), dtype=np.float32)
        for row, item in enumerate(items):
            if not isinstance(item, str):
                raise TypeError(f"LocalEmbedding.encode expects str, got {type(item).__name__}")
            vectors[row] = self._encode_one(item)

        return vectors[0] if single else vectors

    async def aencode(self, texts: str | list[str], **kwargs: Any) -> np.ndarray:
        """异步版本。本实现无 IO，直接同步算完返回。"""
        return self.encode(texts, **kwargs)

    def _encode_one(self, text: str) -> np.ndarray:
        tokens = tokenize(text)
        if not tokens:
            tokens = [_EMPTY_TOKEN]

        # 先聚合词频再投影：对每个 token 只做一次哈希，权重用次线性词频
        # 1 + log(tf)，这样一个词出现 10 次不会得到 10 倍权重。
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1

        vector = np.zeros(self._dim, dtype=np.float32)
        for token, tf in counts.items():
            index, sign = _hash_token(token, self._dim)
            vector[index] += sign * (1.0 + math.log(tf))

        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            # 正负抵消理论上可能让向量全零；返回一个确定的单位向量，保证下游
            # 拿到的是合法有限向量而不是零向量（Qdrant 的 cosine 距离对零向量
            # 无定义，而下游校验只拦非有限值、不拦零向量）。
            fallback_index, fallback_sign = _hash_token(_EMPTY_TOKEN, self._dim)
            vector[fallback_index] = fallback_sign
            norm = float(np.linalg.norm(vector))
        return vector / norm


class LocalReranker:
    """确定性词法重排器，接口与 :class:`rag.reranker.ApiReranker` 一致。

    打分 = 查询与候选内容的 token 覆盖率（含双字），叠加一个很小的先验分
    ``1 / (rank + 1)``，使得"融合排序已经把它排前面"这件事不会被完全抹掉。
    纯本地计算、零网络，且**真的重新排了序**（不是把原顺序穿上重排的衣服），
    所以 ``applied=True`` 是诚实的。
    """

    def __init__(self, model: str | None = None) -> None:
        self._model = model or LOCAL_RERANKER_MODEL_NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        """本地实现永远可用 —— 它不依赖任何外部服务。"""
        return True

    def rerank_with_outcome(self, query: str, results: list[dict[str, Any]], top_k: int = 3) -> Any:
        from rag.reranker import RerankOutcome, RerankReason

        fallback = list(results[:top_k])

        if not results:
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=False,
                reason=RerankReason.OK,
                provider_called=False,
            )

        query_tokens = set(tokenize(query))
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for rank, candidate in enumerate(results):
            content = candidate.get("content") or ""
            candidate_tokens = set(tokenize(content))
            overlap = len(query_tokens & candidate_tokens)
            coverage = overlap / len(query_tokens) if query_tokens else 0.0
            # 先验分权重刻意很小（0.05）：足以打破平局，不足以压倒词法证据。
            score = coverage + 0.05 / (rank + 1)
            candidate["rerank_score"] = float(score)
            scored.append((float(score), rank, candidate))

        # 稳定排序：分数相同时保持输入顺序（rank 参与比较键），保证可复现。
        scored.sort(key=lambda item: (-item[0], item[1]))
        return RerankOutcome(
            results=[c for _, _, c in scored[:top_k]],
            applied=True,
            degraded=False,
            reason=RerankReason.OK,
            provider_called=False,
        )

    def rerank(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        """历史 list 契约（与 :meth:`rag.reranker.ApiReranker.rerank` 语义一致）。"""
        return self.rerank_with_outcome(query, results, top_k).results


@dataclass(frozen=True, slots=True)
class EmbedderSelection:
    """``rag.embedding_factory.create_embed_fn`` 的结论。

    ``embed_fn`` 为 ``None`` 表示向量通道不可用 —— 调用方必须**显式禁用**向量
    通道并诚实标注降级，绝不能因此造一个随机/哈希向量去凑（见
    ``rag.embedding_status`` 的 P0-05 契约）。

    ``provider`` / ``model`` 是给检索元数据与指标标签用的稳定字符串。
    """

    embed_fn: Any | None
    provider: str
    model: str
    reason: str


__all__ = [
    "LOCAL_EMBEDDING_MODEL_NAME",
    "LOCAL_RERANKER_MODEL_NAME",
    "EmbedderSelection",
    "LocalEmbedding",
    "LocalReranker",
    "tokenize",
]
