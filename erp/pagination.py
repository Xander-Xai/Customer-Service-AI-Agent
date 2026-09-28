"""Small, deterministic offset pagination contract for adapters that support it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ERPPage:
    items: list[dict]
    next_cursor: str | None
    has_more: bool


def paginate_records(records: list[dict], *, limit: int = 50, cursor: str | None = None, max_limit: int = 100) -> ERPPage:
    if limit <= 0 or limit > max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}")
    offset = 0
    if cursor is not None:
        if not cursor.isdigit() or int(cursor) < 0:
            raise ValueError("cursor must be a non-negative offset")
        offset = int(cursor)
    ordered = list(records)
    items = ordered[offset : offset + limit]
    next_offset = offset + len(items)
    has_more = next_offset < len(ordered)
    return ERPPage(items=items, next_cursor=str(next_offset) if has_more else None, has_more=has_more)
