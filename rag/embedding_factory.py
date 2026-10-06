"""embed_fn 选择的**唯一**实现（issue #52）。

为什么需要这一个模块
--------------------
"这个 embedding 该不该真的去打 provider" 之前散落在两个地方，各写一份、且都只判
truthiness：

  - ``core/container.py::_init_rag_and_tools`` —— ``try: ApiEmbedding(...)``，
    ``except: None``。构造本身不失败，所以永远成功。
  - ``rag/qdrant_knowledge_base.py::_create_embedding_function`` ——
    ``if not EMBEDDING_API_KEY: return None``。

两处都挡不住**占位 key**：``sk-placeholder-embedding-test-key`` 是真值，于是拿着假
凭据连 api.siliconflow.cn 并吃 401。这正是 issue #52 里
``tests/e2e/test_multimodal.py`` 触发真实 embeddings request 的原因。

仓库对"API key 是否可用"已经有唯一权威判定（issue #51 建立的
``core/config.evaluate_llm_api_key``，前缀黑名单 + 长度下限）。本模块是它在
embedding 侧的对应物，并把选择逻辑收敛到一处，避免出现第三套规则。

三态选择
--------
1. ``provider=local`` → :class:`rag.local_provider.LocalEmbedding`。
   零网络零凭据，确定性。**这是默认测试 lane。**
2. ``provider=remote`` 且凭据是占位值/空 → **不构造 HTTP 客户端**，
   ``embed_fn=None``。向量通道随之被显式禁用，检索诚实降级
   （见 ``rag/embedding_status`` 的 P0-05 契约：绝不伪造随机向量）。
3. ``provider=remote`` 且凭据看起来真实 → :class:`rag.api_embedding.ApiEmbedding`。
   **这就是生产路径，与本 issue 之前逐字节一致。**

``explicit_model`` 参数用于测试注入已有的 embed_fn；``default_fn``（未配置凭据且
provider=remote 时的进程内默认）保留给 :class:`~rag.qdrant_knowledge_base.QdrantKnowledgeBase`
的 ``_create_embedding_function`` 语义。
"""

from __future__ import annotations

from typing import Any

from core import config
from core.logger import get_logger
from rag.local_provider import EmbedderSelection

logger = get_logger("rag.embedding_factory")

#: EmbedderSelection.reason 取值（稳定字符串，进日志与测试断言）。
REASON_LOCAL_PROVIDER = "local_provider"
REASON_REMOTE_PROVIDER = "remote_provider"
REASON_PLACEHOLDER_CREDENTIAL = "placeholder_credential"
REASON_NO_CREDENTIAL = "no_credential"
REASON_EXPLICIT = "explicit"
REASON_REMOTE_UNAVAILABLE = "remote_unavailable"


def create_embed_fn(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
    dim: int | None = None,
    explicit_model: Any | None = None,
    default_fn: Any | None = None,
) -> Any:
    """选出 embed_fn；不可用时返回 ``None``（**不是**一个伪造向量）。

    显式传入的 ``explicit_model`` 优先级最高 —— 调用方（Qdrant 知识库构造函数）
    已经在参数里拿到 embed_fn 时不该再选一次。
    """
    selection = select_embed_fn(
        api_key=api_key,
        model=model,
        base_url=base_url,
        provider=provider,
        dim=dim,
        explicit_model=explicit_model,
        default_fn=default_fn,
    )
    return selection.embed_fn


def select_embed_fn(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
    dim: int | None = None,
    explicit_model: Any | None = None,
    default_fn: Any | None = None,
) -> EmbedderSelection:
    """:func:`create_embed_fn` 的带元数据版本，返回 :class:`EmbedderSelection`。

    与 :func:`create_embed_fn` 的差别只是把"选了谁 / 为什么"一并返回，供检索
    元数据与指标标签使用 —— 让"向量通道用了什么"是可观测的，而不是猜的。
    """
    if explicit_model is not None:
        return EmbedderSelection(
            embed_fn=explicit_model,
            provider="explicit",
            model=str(getattr(explicit_model, "model", "unknown")),
            reason=REASON_EXPLICIT,
        )

    resolved_provider = (
        (provider or config.EMBEDDING_PROVIDER or config.PROVIDER_REMOTE).strip().lower()
    )
    resolved_model = model or config.EMBEDDING_MODEL
    resolved_dim = dim or config.EMBEDDING_DIM

    # ---- 态 1：local ----
    if resolved_provider == config.PROVIDER_LOCAL:
        from rag.local_provider import LOCAL_EMBEDDING_MODEL_NAME, LocalEmbedding

        return EmbedderSelection(
            embed_fn=LocalEmbedding(dim=resolved_dim),
            provider=config.PROVIDER_LOCAL,
            model=LOCAL_EMBEDDING_MODEL_NAME,
            reason=REASON_LOCAL_PROVIDER,
        )

    # ---- 态 2：remote，但凭据不可信 → 不构造 HTTP 客户端 ----
    resolved_key = config.EMBEDDING_API_KEY if api_key is None else api_key
    if config.is_placeholder_api_key(resolved_key):
        reason = (
            REASON_NO_CREDENTIAL
            if not (resolved_key or "").strip()
            else REASON_PLACEHOLDER_CREDENTIAL
        )
        logger.warning(
            "远程 embedding 通道未启用：EMBEDDING_API_KEY %s，向量通道将被禁用"
            "（不生成随机向量），检索降级到词法/BM25 通道",
            "未配置" if reason == REASON_NO_CREDENTIAL else "是占位值",
        )
        if default_fn is not None:
            return EmbedderSelection(
                embed_fn=default_fn(),
                provider=config.PROVIDER_REMOTE,
                model=str(getattr(default_fn, "model", resolved_model)),
                reason=REASON_REMOTE_UNAVAILABLE,
            )
        return EmbedderSelection(
            embed_fn=None,
            provider=config.PROVIDER_REMOTE,
            model=resolved_model,
            reason=reason,
        )

    # ---- 态 3：remote + 真实凭据 = 生产路径（不变）----
    from rag.api_embedding import ApiEmbedding

    resolved_base_url = base_url or config.EMBEDDING_BASE_URL
    return EmbedderSelection(
        embed_fn=ApiEmbedding(
            api_key=resolved_key,
            model=resolved_model,
            base_url=resolved_base_url,
        ),
        provider=config.PROVIDER_REMOTE,
        model=resolved_model,
        reason=REASON_REMOTE_PROVIDER,
    )
