"""Deterministic Tool Result Context Engineering.

This module owns tool-result shaping before a result enters the LLM message
history. Token values are estimates: the project token counter uses tiktoken
when available and a conservative character fallback otherwise. No LLM call is
made here, so optimization is deterministic and rollback-safe.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from core.session.token_counter import _count_tokens


@dataclass(frozen=True)
class ToolResultPolicy:
    max_tokens: int | None = None
    max_items: int | None = None
    include_fields: set[str] | None = None
    exclude_fields: set[str] | None = None
    preserve_recent: int = 0
    strategy: str = "structured"
    deduplicate: bool = True
    remove_empty: bool = True


@dataclass
class OptimizedToolResult:
    content: str
    raw_size: int
    optimized_size: int
    raw_token_estimate: int
    optimized_token_estimate: int
    compression_ratio: float
    truncated: bool
    item_count_before: int | None = None
    item_count_after: int | None = None
    reference_id: str | None = None
    strategy: str = "json"


DEFAULT_TOOL_POLICIES: dict[str, ToolResultPolicy] = {
    "query_product": ToolResultPolicy(
        max_items=5,
        include_fields={"product_id", "name", "category", "price", "specs", "ingredients", "suitable"},
    ),
    "query_inventory": ToolResultPolicy(
        max_items=5,
        include_fields={"product_id", "product_name", "stock", "warehouse", "updated"},
    ),
    "query_order": ToolResultPolicy(
        max_items=5,
        include_fields={"order_id", "customer_name", "status", "total", "tracking", "created"},
    ),
    "query_customer": ToolResultPolicy(
        max_items=1,
        include_fields={"customer_id", "name", "phone", "level", "total_spent", "total_orders", "address"},
    ),
}


def _json_content(value: Any) -> str:
    if isinstance(value, str):
        return value or "查询完成，无结果"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _filter_value(value: Any, policy: ToolResultPolicy) -> Any:
    if isinstance(value, dict):
        filtered = {}
        for key, item in value.items():
            if policy.include_fields is not None and key not in policy.include_fields:
                continue
            if policy.exclude_fields and key in policy.exclude_fields:
                continue
            item = _filter_value(item, replace(policy, include_fields=None))
            if policy.remove_empty and _is_empty(item):
                continue
            filtered[key] = item
        return filtered
    if isinstance(value, list):
        items = [_filter_value(item, policy) for item in value]
        if policy.remove_empty:
            items = [item for item in items if not _is_empty(item)]
        if policy.deduplicate:
            seen = set()
            deduped = []
            for item in items:
                marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    deduped.append(item)
            items = deduped
        return items
    return value


def _truncate_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if _count_tokens(text) <= max_tokens:
        return text
    marker = "…"
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + marker
        if _count_tokens(candidate) <= max_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or marker


def _fit_structured(value: Any, policy: ToolResultPolicy) -> tuple[Any, bool]:
    content = _json_content(value)
    if policy.max_tokens is None or _count_tokens(content) <= policy.max_tokens:
        return value, False
    if isinstance(value, list):
        working = list(value)
        while working and _count_tokens(_json_content(working)) > policy.max_tokens:
            working.pop()
        return working, len(working) != len(value)
    if isinstance(value, dict):
        working = dict(value)
        # Remove the largest non-identifier values first, preserving fields
        # that are commonly needed for downstream calls.
        protected_keys = {"id", "name", "status", "order_id", "product_id", "customer_id"}
        keys = sorted(
            working,
            key=lambda k: (k in protected_keys or k.endswith("_id"), len(_json_content(working[k]))),
            reverse=False,
        )
        for key in keys:
            if _count_tokens(_json_content(working)) <= policy.max_tokens:
                break
            if key.endswith("_id") or key in {"id", "name", "status", "order_id", "product_id", "customer_id"}:
                continue
            # Drop verbose non-continuation fields as a whole. This keeps the
            # remaining JSON valid and protects IDs/names used by later tools.
            working.pop(key, None)
        if _count_tokens(_json_content(working)) > policy.max_tokens:
            # A valid JSON object is more useful to a tool-calling model than
            # an arbitrary character slice. Keep continuation identifiers and
            # the first small semantic field, then omit verbose payloads.
            protected = [
                key
                for key in working
                if key.endswith("_id") or key in {"id", "name", "status", "order_id", "product_id", "customer_id"}
            ]
            working = {key: working[key] for key in protected}
        return working, _json_content(working) != content
    return value, False


class ToolResultOptimizer:
    """Apply a per-tool or caller-supplied deterministic result policy."""

    def __init__(self, enabled: bool = True, policies: dict[str, ToolResultPolicy] | None = None,
                 store=None, offload_enabled: bool = False, offload_min_tokens: int = 1200,
                 store_ttl_seconds: int = 900, semantic_summary_enabled: bool = False,
                 summarizer=None):
        self.enabled = enabled
        self.policies = policies or DEFAULT_TOOL_POLICIES
        self.store = store
        self.offload_enabled = offload_enabled
        self.offload_min_tokens = offload_min_tokens
        self.store_ttl_seconds = store_ttl_seconds
        self.semantic_summary_enabled = semantic_summary_enabled
        self.summarizer = summarizer

    def policy_for(self, tool_name: str, default: ToolResultPolicy | None = None) -> ToolResultPolicy:
        return self.policies.get(tool_name, default or ToolResultPolicy(strategy="conservative"))

    def optimize(
        self,
        tool_name: str,
        result: Any,
        policy: ToolResultPolicy | None = None,
    ) -> OptimizedToolResult:
        policy = policy or self.policy_for(tool_name)
        from core.tool_result_compressors import compressor_for

        raw_content = _json_content(result)
        structured_result = result
        if isinstance(result, str) and result.strip()[:1] in {"{", "["}:
            try:
                structured_result = json.loads(result)
            except json.JSONDecodeError:
                structured_result = result
        raw_items = len(structured_result) if isinstance(structured_result, list) else (1 if isinstance(structured_result, dict) else None)
        if not self.enabled:
            content = raw_content
            optimized_items = raw_items
            truncated = False
        else:
            structured = structured_result
            compressor = compressor_for(tool_name, structured)
            compressed = compressor.compress(structured, max_tokens=policy.max_tokens, max_items=policy.max_items)
            filtered = _filter_value(compressed.value, policy) if isinstance(compressed.value, dict | list) else compressed.value
            filtered_items = len(filtered) if isinstance(filtered, list) else (1 if isinstance(filtered, dict) else None)
            if isinstance(filtered, list) and policy.max_items is not None:
                filtered = filtered[: max(0, policy.max_items)]
            fitted, budget_truncated = _fit_structured(filtered, policy) if isinstance(filtered, dict | list) else (filtered, False)
            content = _json_content(fitted)
            truncated = compressed.truncated or (
                content != raw_content
                or budget_truncated
                or (policy.max_items is not None and isinstance(filtered, list) and len(filtered) > policy.max_items)
            )
            if policy.max_tokens is not None and _count_tokens(content) > policy.max_tokens and not isinstance(fitted, dict | list):
                content = _truncate_text(content, policy.max_tokens)
                truncated = True
            optimized_items = len(fitted) if isinstance(fitted, list) else filtered_items

        raw_tokens = _count_tokens(raw_content)
        optimized_tokens = _count_tokens(content)
        _record_metrics(tool_name, len(raw_content), len(content), raw_tokens, optimized_tokens, truncated)
        strategy_name = getattr(compressor_for(tool_name, structured_result), "__class__", type(None)).__name__.replace("ResultCompressor", "").lower() or "json"
        with suppress(Exception):
            from core.monitoring import record_tool_result_event
            record_tool_result_event("compressor", strategy=strategy_name)
        return OptimizedToolResult(
            content=content,
            raw_size=len(raw_content),
            optimized_size=len(content),
            raw_token_estimate=raw_tokens,
            optimized_token_estimate=optimized_tokens,
            compression_ratio=(optimized_tokens / raw_tokens if raw_tokens else 1.0),
            truncated=truncated,
            item_count_before=raw_items,
            item_count_after=optimized_items,
            strategy=strategy_name,
        )

    async def optimize_async(self, tool_name: str, result: Any, policy: ToolResultPolicy | None = None,
                             *, scope: dict[str, str] | None = None) -> OptimizedToolResult:
        optimized = self.optimize(tool_name, result, policy)
        if not (self.enabled and self.offload_enabled and self.store and scope and optimized.raw_token_estimate >= self.offload_min_tokens):
            return optimized
        started = time.perf_counter()
        try:
            reference_id = await self.store.put(tool_name, result, scope=scope, ttl_seconds=self.store_ttl_seconds,
                                                metadata={"content_type": "tool_result", "schema": "v1"})
            summary = optimized.content
            if self.semantic_summary_enabled and self.summarizer:
                try:
                    from core.monitoring import record_tool_result_event
                    record_tool_result_event("summary")
                    summary = await asyncio.wait_for(self.summarizer.summarize(optimized.content), timeout=2.0)
                except Exception:
                    from core.monitoring import record_tool_result_event
                    record_tool_result_event("summary_failed")
                    summary = optimized.content
            available_fields: list[str] = []
            source = result if isinstance(result, list) else [result]
            if source and all(isinstance(item, dict) for item in source):
                available_fields = sorted({key for item in source for key in item})[:32]
            preview = {"status": "result_offloaded", "reference_id": reference_id,
                       "summary": str(summary)[:600], "item_count": optimized.item_count_before,
                       "available_fields": available_fields, "recoverable": True}
            optimized.content = _json_content(preview)
            optimized.optimized_size = len(optimized.content)
            optimized.optimized_token_estimate = _count_tokens(optimized.content)
            optimized.compression_ratio = optimized.optimized_token_estimate / optimized.raw_token_estimate if optimized.raw_token_estimate else 1.0
            optimized.reference_id = reference_id
            from core.monitoring import record_tool_result_event
            record_tool_result_event("offloaded", latency_seconds=time.perf_counter() - started)
        except Exception:
            from core.monitoring import record_tool_result_event
            record_tool_result_event("store_error", latency_seconds=time.perf_counter() - started)
        return optimized

    async def recover(self, reference_id: str, *, scope: dict[str, str]) -> Any | None:
        if not self.store:
            return None
        started = time.perf_counter()
        try:
            record = await self.store.get(reference_id, scope=scope)
            if record is None:
                from core.monitoring import record_tool_result_event
                record_tool_result_event("recovery_failed", latency_seconds=time.perf_counter() - started)
                return None
            from core.monitoring import record_tool_result_event
            record_tool_result_event("recovered", latency_seconds=time.perf_counter() - started)
            return record.payload
        except Exception:
            from core.monitoring import record_tool_result_event
            record_tool_result_event("recovery_failed", latency_seconds=time.perf_counter() - started)
            return None


def compact_old_tool_messages(messages: list[Any], preserve_recent: int) -> list[Any]:
    """Replace old ToolMessage content while preserving message protocol order."""
    tool_indexes = [i for i, message in enumerate(messages) if getattr(message, "type", None) == "tool"]
    keep = set(tool_indexes[-max(0, preserve_recent):]) if preserve_recent else set()
    result = list(messages)
    for index in tool_indexes:
        if index in keep:
            continue
        message = result[index]
        if "[older tool result compacted]" in str(getattr(message, "content", "")):
            continue
        tool_name = "unknown"
        for previous in reversed(result[:index]):
            if getattr(previous, "type", None) == "ai":
                for call in getattr(previous, "tool_calls", []) or []:
                    if call.get("id") == getattr(message, "tool_call_id", None):
                        tool_name = call.get("name", tool_name)
                        break
        summary = f"[older tool result compacted]\ntool={tool_name}\nsummary=historical result retained outside active context"
        try:
            parsed = json.loads(message.content)
            summary_fields = {"id", "*_id", "name", "status", "total", "tracking", "created", "stock", "warehouse", "level"}

            def keep_fields(value: Any, allowed: set[str] = summary_fields) -> Any:
                if isinstance(value, list):
                    return [keep_fields(item) for item in value[:2]]
                if isinstance(value, dict):
                    return {
                        key: keep_fields(item)
                        for key, item in value.items()
                        if key in allowed or key.endswith("_id")
                    }
                return value

            compact = keep_fields(parsed)
            summary += f"\nkey_fields={json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"
        except (TypeError, json.JSONDecodeError):
            summary += f"\nkey_fields={str(message.content)[:160]}"
        result[index] = type(message)(content=summary, tool_call_id=message.tool_call_id)
    return result


def _record_metrics(tool_name: str, raw_size: int, optimized_size: int, raw_tokens: int, optimized_tokens: int, truncated: bool) -> None:
    try:
        from core.monitoring import record_tool_result_optimization

        record_tool_result_optimization(tool_name, raw_size, optimized_size, raw_tokens, optimized_tokens, truncated)
    except Exception:
        # Observability must never make a tool call fail.
        return
