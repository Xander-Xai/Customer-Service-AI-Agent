"""
RAG Reranker（v6.2）
对初始检索结果进行二次排序，提升相关性。

实现策略：
1. ApiReranker（HTTP API）— 调用重排序 API（SiliconFlow / OpenAI 兼容接口）
2. （BM25 词法重排器已移除——历史变更，生产环境始终使用 ApiReranker）

使用方式：
    reranker = create_reranker()
    reranked = reranker.rerank(query, results, top_k=3)          # 兼容：只要 list
    outcome = reranker.rerank_with_outcome(query, results, 3)   # 新：typed 事实

为什么有两个入口
----------------
``rerank()`` 保留历史 list 契约（preflight gate、旧调用方、测试都依赖它）。
``rerank_with_outcome()`` 返回**本次调用自己的**不可变事实：到底有没有真的
重排过。canonical retrieval pipeline 只用后者。

这一点是刻意的：``last_error_status`` 是**实例级共享可变状态**。同一个
reranker 实例被并发请求共享时，
``call A -> rerank() -> 上下文切换 -> call B 改写 last_error_status ->
call A 读 last_error_status`` 会把 B 的失败算到 A 头上。所以 runtime 的
applied/degraded 必须来自本次调用的返回值，不能靠事后反查实例状态。
``last_error_status`` 仅保留给 preflight/评测 gate 诊断，不再参与 runtime 判定。

降级策略不变（不是 fail-closed）
--------------------------------
provider 不可用/失败时**仍然**返回融合后的原始排序，不抛异常、不让整个 RAG
请求失败。可用性优先于严格性。改变的只是：这次降级从此**可见、可观测、
不可伪装成成功**。

隐私边界
--------
``reason`` 是有界枚举；日志只记录 reason 枚举、HTTP 状态码和模型名。
**不记录** query、documents、Authorization、API key，也不记录 provider 响应体
（``e.response.text`` 可能回显用户内容或 provider 细节）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from core.config import RERANKER_API_KEY, RERANKER_BASE_URL, RERANKER_MODEL
from core.logger import get_logger

logger = get_logger("rag.reranker")


class RerankReason(str, Enum):
    """Bounded, machine-readable rerank outcome reasons.

    Deliberately an enum: these values reach ``RetrievalResult.meta``, the
    ``RetrievalTrace`` and trace telemetry, so they must be low-cardinality and
    must never carry an exception message, a response body or user text.
    """

    OK = ""
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"


#: Pipeline-level rerank reasons (not provider outcomes). Kept next to the
#: outcome enum so the whole rerank vocabulary lives in one place.
REASON_RERANK_DISABLED = "rerank_disabled"
REASON_INSUFFICIENT_CANDIDATES = "insufficient_candidates"
#: Generic label used only if an outcome ever degrades with an empty reason, so
#: a degraded RERANK stage is never reported without a cause.
REASON_RERANK_DEGRADED = "reranker_degraded"


@dataclass(frozen=True)
class RerankOutcome:
    """Immutable per-call result of one rerank attempt.

    ``results`` is always usable (the fused/pre-rerank ordering truncated to
    ``top_k`` when reranking did not happen), so availability is preserved.
    ``applied``/``degraded``/``reason`` are the truth about whether a real
    rerank occurred.
    """

    results: list[dict[str, Any]]
    applied: bool
    degraded: bool
    reason: RerankReason
    http_status: int | None = None
    provider_called: bool = False

    @property
    def reason_value(self) -> str:
        """``reason`` as a plain string, for meta/trace/telemetry surfaces."""
        return self.reason.value


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
        """对检索结果进行 API 重排序（历史 list 契约，保持不变）

        兼容入口。**丢弃**了本次调用的 applied/degraded 事实 —— 需要区分
        "真的重排过" 与 "回退到原始排序" 的调用方必须改用
        :meth:`rerank_with_outcome`。
        """
        return self.rerank_with_outcome(query, results, top_k).results

    def rerank_with_outcome(
        self, query: str, results: list[dict[str, Any]], top_k: int = 3
    ) -> RerankOutcome:
        """重排并返回本次调用的**不可变**事实。

        Availability first: every failure path still returns the original
        ordering truncated to ``top_k``. Truthfulness second: each path also
        says whether a real rerank happened, and why not if it did not.
        """
        fallback = list(results[:top_k])

        if not results:
            # Nothing to rerank is not a failure and not a rerank.
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=False,
                reason=RerankReason.OK,
                provider_called=False,
            )

        if not self._available:
            # No key configured: no provider call was made, so this is a
            # configuration state, not a provider failure.
            logger.warning(
                "Reranker 不可用（未配置 API Key），返回原始排序: model=%s",
                self._model,
            )
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=True,
                reason=RerankReason.UNAVAILABLE,
                provider_called=False,
            )

        url = f"{self._base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "query": query,
            "documents": [r.get("content", "") for r in results],
        }

        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=self._timeout
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            self.last_error_status = None
            # A provider request WAS issued and did not complete: fail loud.
            logger.error(
                "Reranker provider 超时，降级为原始排序: reason=%s model=%s timeout_s=%s",
                RerankReason.TIMEOUT.value,
                self._model,
                self._timeout,
            )
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=True,
                reason=RerankReason.TIMEOUT,
                provider_called=True,
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            self.last_error_status = status
            # Status code + bounded reason + model only. The response body is
            # deliberately NOT logged: it can echo user documents or provider
            # internals back into the logs.
            logger.error(
                "Reranker provider HTTP 失败，降级为原始排序: reason=%s "
                "http_status=%s model=%s",
                RerankReason.HTTP_ERROR.value,
                status,
                self._model,
            )
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=True,
                reason=RerankReason.HTTP_ERROR,
                http_status=status,
                provider_called=True,
            )
        except Exception as e:
            self.last_error_status = None
            # Exception *class* only — the message can carry request content.
            logger.error(
                "Reranker provider 调用失败，降级为原始排序: reason=%s "
                "error_type=%s model=%s",
                RerankReason.PROVIDER_ERROR.value,
                type(e).__name__,
                self._model,
            )
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=True,
                reason=RerankReason.PROVIDER_ERROR,
                provider_called=True,
            )

        scored, invalid_reason = self._apply_scores(results, data, top_k)
        if scored is None:
            # HTTP 200 is not proof of reranking. Without usable relevance
            # scores we cannot claim the candidates were actually reranked.
            logger.error(
                "Reranker provider 返回不可用结果，降级为原始排序: reason=%s model=%s",
                invalid_reason.value if invalid_reason else RerankReason.INVALID_RESPONSE.value,
                self._model,
            )
            return RerankOutcome(
                results=fallback,
                applied=False,
                degraded=True,
                reason=invalid_reason or RerankReason.INVALID_RESPONSE,
                http_status=response.status_code,
                provider_called=True,
            )

        return RerankOutcome(
            results=scored,
            applied=True,
            degraded=False,
            reason=RerankReason.OK,
            http_status=response.status_code,
            provider_called=True,
        )

    def _apply_scores(
        self,
        results: list[dict[str, Any]],
        data: Any,
        top_k: int,
    ) -> tuple[list[dict[str, Any]] | None, RerankReason | None]:
        """Map provider relevance scores back onto candidates.

        Returns ``(ordered, None)`` only when the response genuinely proves a
        rerank happened, otherwise ``(None, reason)``. A malformed ``200 OK``
        must never be reported as a successful rerank.
        """
        if not isinstance(data, dict):
            return None, RerankReason.INVALID_RESPONSE
        items = data.get("results")
        if not isinstance(items, list) or not items:
            return None, RerankReason.INVALID_RESPONSE

        usable = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool):
                continue
            if idx < 0 or idx >= len(results):
                continue
            score = item.get("relevance_score")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            results[idx]["rerank_score"] = float(score)
            usable += 1

        if usable == 0:
            # Every entry was unusable: no candidate has a relevance score, so
            # the ordering below would be the input ordering wearing a rerank's
            # clothes.
            return None, RerankReason.INVALID_RESPONSE

        ordered = sorted(
            results,
            key=lambda r: r.get("rerank_score", 0),
            reverse=True,
        )
        return ordered[:top_k], None


def create_reranker() -> ApiReranker:
    """创建 API Reranker（BM25 词法重排器已移除——历史变更，生产环境始终使用 API Reranker）

    Returns:
        ApiReranker 实例（API Key 未配置时 available=False，rerank 回退到原始顺序）
    """
    return ApiReranker()
