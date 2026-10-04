"""Embedding provider 工厂 —— RAG 层「用哪个 embedder」的**唯一**决策点。

为什么要有工厂
--------------
构造 embedder 的地方有两处：

* :meth:`core.container.ServiceContainer._init_rag_and_tools`（容器级单例）
* :meth:`rag.qdrant_knowledge_base.QdrantKnowledgeBase._create_embedding_function`
  （知识库自带 fallback）

两处都曾经无条件 ``ApiEmbedding(...)``，也就是「离线 lane 也会去打真实 provider」。
工厂把选择收成一处，两条路径不可能再分叉。

Fail closed
-----------
未知的 ``EMBEDDING_PROVIDER`` 直接抛 :class:`ValueError`，**不**静默回退到
``api`` —— 静默回退等于把配置拼写错误变成一次真实的出网请求，正是本 issue 要消除
的行为。``api`` / ``offline`` 之外的拼写错误必须在启动时就炸。
"""

from __future__ import annotations

from typing import Any

from core.config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    EMBEDDING_PROVIDERS,
)
from core.logger import get_logger

logger = get_logger("rag.embedding_factory")

__all__ = ["create_embedding_model", "EMBEDDING_PROVIDERS"]


def create_embedding_model(
    provider: str = EMBEDDING_PROVIDER,
    *,
    api_key: str = EMBEDDING_API_KEY,
    model: str = EMBEDDING_MODEL,
    base_url: str = EMBEDDING_BASE_URL,
) -> Any:
    """按 ``provider`` 构造 embedder。

    Args:
        provider: ``"api"``（真实 HTTP provider）或 ``"offline"``（确定性无网络）。
            其余值抛 :class:`ValueError`。
        api_key / model / base_url: 仅 ``api`` 使用；显式传入便于单测与脚本覆写。

    Returns:
        具备 ``model`` / ``encode`` / ``aencode`` 的对象。
    """
    key = (provider or "").strip().lower()

    if key == "offline":
        from rag.offline_embedding import OfflineEmbedding

        logger.info(
            "Embedding provider=offline：使用确定性无网络 embedder"
            "（检索质量指标在此模式下无意义，仅供离线 lane 使用）"
        )
        return OfflineEmbedding()

    if key == "api":
        from rag.api_embedding import ApiEmbedding

        return ApiEmbedding(api_key=api_key, model=model, base_url=base_url)

    raise ValueError(
        f"unknown EMBEDDING_PROVIDER={provider!r}; expected one of {'/'.join(EMBEDDING_PROVIDERS)}"
    )
