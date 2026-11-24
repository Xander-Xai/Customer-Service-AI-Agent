"""Opt-in, case-scoped accounting for actual chat HTTP attempts.

A request is counted before transport; failed/cancelled/uncounted usage remains
unknown. This is diagnostic accounting, not an invoice or spending cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attempt:
    usage: tuple[int, int] | None = None

    def observe(self, payload: Any) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        if (
            type(prompt) is int
            and type(completion) is int
            and prompt >= 0
            and completion >= 0
            and (total is None or (type(total) is int and total == prompt + completion))
        ):
            self.usage = (prompt, completion)


@dataclass
class UsageScope:
    attempts: list[Attempt] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        known = [attempt.usage for attempt in self.attempts if attempt.usage is not None]
        complete = len(known) == len(self.attempts)
        return {
            "chat_http_attempts": len(self.attempts),
            "attempts_with_usage": len(known),
            "unknown_usage_attempts": len(self.attempts) - len(known),
            "input_tokens": sum(item[0] for item in known) if complete else None,
            "output_tokens": sum(item[1] for item in known) if complete else None,
            "usage_complete": complete,
        }


_scope: ContextVar[UsageScope | None] = ContextVar("provider_usage_scope", default=None)


@contextmanager
def collect_usage() -> Iterator[UsageScope]:
    scope = UsageScope()
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def begin_attempt() -> Attempt | None:
    scope = _scope.get()
    if scope is None:
        return None
    attempt = Attempt()
    scope.attempts.append(attempt)
    return attempt
