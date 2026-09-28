"""Deterministic compressors for untrusted tool-result payloads."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.session.token_counter import _count_tokens


@dataclass(frozen=True)
class CompressedPayload:
    value: Any
    strategy: str
    truncated: bool = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _fit_text(value: str, max_tokens: int) -> str:
    if _count_tokens(value) <= max_tokens:
        return value
    lo, hi, best = 0, len(value), "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = value[:mid].rstrip() + "…"
        if _count_tokens(candidate) <= max_tokens:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    return best


def _clean_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query if not query else urlencode(query), ""))
    except ValueError:
        return url


class SearchResultCompressor:
    def compress(self, value: Any, *, max_tokens: int | None = None, max_items: int | None = None, snippet_tokens: int = 80) -> CompressedPayload:
        rows = value if isinstance(value, list) else value.get("results", []) if isinstance(value, dict) else []
        output, seen = [], set()
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or row.get("source") or "")
            title = str(row.get("title") or row.get("name") or "")
            snippet = str(row.get("snippet") or row.get("description") or "")
            key = (title.strip().lower(), _clean_url(url).lower())
            if key in seen:
                continue
            seen.add(key)
            output.append({"rank": rank, "title": title, "url": _clean_url(url), "snippet": _fit_text(snippet, snippet_tokens), **({"score": row["score"]} if "score" in row else {})})
            if max_items is not None and len(output) >= max_items:
                break
        truncated = len(output) < len(rows)
        if max_tokens is not None:
            while output and _count_tokens(_json(output)) > max_tokens:
                output.pop()
                truncated = True
        return CompressedPayload(output, "search", truncated)


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self.links: list[str] = []
        self._ignored = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self._ignored += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self._ignored:
            self._ignored -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._ignored:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self._in_title:
            self.title += " " + cleaned
        self.parts.append(cleaned)


class HTMLResultCompressor:
    def compress(self, value: Any, *, max_tokens: int | None = None, max_items: int | None = None) -> CompressedPayload:
        source = value if isinstance(value, str) else _json(value)
        parser = _VisibleTextParser()
        try:
            parser.feed(source)
            parser.close()
        except Exception:
            # HTMLParser is intentionally best effort for malformed pages.
            with suppress(Exception):
                parser.close()
        text = " ".join(parser.parts)
        if parser.title and parser.title.strip() not in text:
            text = parser.title.strip() + " " + text
        truncated = bool(max_tokens and _count_tokens(text) > max_tokens)
        return CompressedPayload(_fit_text(text, max_tokens) if max_tokens else text, "html", truncated)


class JSONResultCompressor:
    def compress(self, value: Any, *, max_tokens: int | None = None, max_items: int | None = None) -> CompressedPayload:
        return CompressedPayload(value, "json", False)


def compressor_for(tool_name: str, value: Any):
    name = tool_name.lower()
    if "search" in name or "web" in name:
        return SearchResultCompressor()
    if "html" in name or (isinstance(value, str) and re.search(r"<\s*(html|body|article|main|h[1-6])\b", value, re.I)):
        return HTMLResultCompressor()
    return JSONResultCompressor()
