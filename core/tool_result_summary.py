"""Optional semantic-summary seam; disabled by default."""

from __future__ import annotations

from typing import Protocol


class ToolResultSummarizerProtocol(Protocol):
    async def summarize(self, content: str) -> str: ...


class DeterministicToolResultSummarizer:
    async def summarize(self, content: str) -> str:
        return content[:240]
