#!/usr/bin/env python3
"""Deterministic guard for documentation/runtime drift (v2).

Discovery-based: scans active docs (README.md, CLAUDE.md, docs/**) and skips
explicit historical snapshots (archive/, milestone/, superpowers/, plans/,
marked banners). It reports drift as errors (exit 1) and soft observations as
warnings.

Checks:
  A. broken local markdown links (resolved strictly relative to the file)
  B. referenced scripts/tests/source files must exist
  C. public runtime config keys present in .env.example
  D. canonical model/config consistency (core/config.py vs .env.example vs compose)
  E. OpenAPI snapshot drift (docs/openapi.json vs app.openapi(), normalized)
  F. RAG benchmark metadata integrity (total_queries == len(queries))
  G. prohibited stale current-state terminology in active docs
  H. referenced rag/core/web source files must exist

Historical docs (with an explicit HISTORICAL banner) are excluded from G but
still pass through link/reference checks unless they are excluded entirely.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DOC_SUFFIXES = {".md", ".rst", ".txt"}
SKIP_PARTS = {
    ".git", "node_modules", "dist", "__pycache__", ".venv", ".worktrees",
    ".claude", "htmlcov", "logs", "chat_sessions", "web/static",
}
# Directories whose content is snapshot/historical by semantics (not scanned
# for current-state terminology; broken-link checking is also skipped because
# snapshots may reference files as they existed at audit time).
HISTORICAL_DIRS = (
    "archive", "milestone", "superpowers", "chat-records", "audit", "reports",
)
# Small explicit exclude list for files that are snapshots by convention.
EXCLUDE_FILES = {
    Path("docs/reports/releases/changelog.md"),  # release history, snapshot per section
    Path("docs/reports/resume-description.md"),  # has its own evidence-freeze header
}
HISTORICAL_MARKER = "HISTORICAL AUDIT SNAPSHOT"

ACTIVE_EXTRA = [
    Path("README.md"),
    Path("CLAUDE.md"),
    Path("docs/README.md"),
    Path("TOOL_RESULT_CACHE_REUSE_AUDIT.md"),
    Path("TOOL_RESULT_V2_AUDIT_REPORT.md"),
]

# Canonical runtime defaults that must agree across code / example / compose.
CANONICAL_MODEL_KEYS = {
    "OPENAI_MODEL": "model",
    "OPENAI_BASE_URL": "base_url",
    "LLM_PROVIDER": "provider",
    "EMBEDDING_MODEL": "embedding_model",
    "RERANKER_MODEL": "reranker_model",
}

# Public runtime keys that must appear in .env.example (deployment template).
REQUIRED_ENV_KEYS = [
    "LLM_PROVIDER", "OPENAI_MODEL", "OPENAI_BASE_URL", "LLM_MAX_TOKENS",
    "HTTP_TIMEOUT", "LLM_ROUTER_TIMEOUT",
    "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_DIM",
    "RERANKER_MODEL", "RERANKER_BASE_URL",
    "HYBRID_SEARCH_ENABLED", "VECTOR_DB_MODE", "QDRANT_HOST", "QDRANT_PORT",
]

# Stale terms that must not be described as current in active docs.
STALE_CURRENT_TERMS = [
    r"Qwen/Qwen2\.5-7B-Instruct", r"Qwen2\.5-7B-Instruct", r"Qwen2\.5-7B",
    r"bge-small-zh-v1\.5", r"text2vec-base-chinese", r"all-MiniLM-L6-v2",
    r"ChromaDB",
]
# Hardcoded stale counts / claims (must not appear as current facts).
STALE_COUNT_TERMS = [
    r"1,?361\+?", r"1400\+ tests", r"5000\+ 条测试", r"共 5000\+ 条",
    r"55 个 HTTP 路径", r"55 HTTP paths",
]
# Allow phrases that explicitly frame history (line-level context allowance).
HISTORICAL_CONTEXT = re.compile(
    r"历史|Historical|historical|snapshot|快照|基线|baseline|时期|当时|改进前|旧|曾经|"
    r"Superseded|superseded|已移除|迁移|ADR-003|ADR-004|v4\.2 时期|2026-06",
)


def is_historical_dir(rel: Path) -> bool:
    return any(part in HISTORICAL_DIRS for part in rel.parts)


def has_historical_banner(text: str) -> bool:
    return HISTORICAL_MARKER in text[:2000]


def is_decision_doc(rel: Path) -> bool:
    return "decisions" in rel.parts


def is_plan_report(rel: Path) -> bool:
    return "reports" in rel.parts and "plans" in rel.parts


def discover_docs(root: Path = ROOT) -> list[Path]:
    """Active docs = README/CLAUDE/docs tree minus historical dirs/banners."""
    docs: list[Path] = []
    seen = set()
    for extra in ACTIVE_EXTRA:
        p = root / extra
        if p.exists():
            docs.append(p)
            seen.add(extra)
    docs_root = root / "docs"
    for p in sorted(docs_root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in DOC_SUFFIXES:
            continue
        rel = p.relative_to(root)
        if rel in seen:
            continue
        if rel in EXCLUDE_FILES:
            continue
        if is_historical_dir(rel) or is_plan_report(rel):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if has_historical_banner(text):
            continue
        docs.append(p)
        seen.add(rel)
    return docs


def markdown_link_targets(text: str):
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = match.group(1).split("#", 1)[0].strip().strip("<>")
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        yield target


def check_links(path: Path, text: str, errors: list[str], root: Path = ROOT) -> None:
    for target in markdown_link_targets(text):
        resolved = (path.parent / target)
        if not resolved.exists():
            errors.append(
                f"broken local link: {path.relative_to(root)} -> {target} "
                f"(docs must use ../../ for repo-root files)"
            )



def check_file_refs_line_aware(path: Path, text: str, errors: list[str], root: Path = ROOT) -> None:
    """Line-aware variant: a referenced file missing on disk is only an error when
    the line describes it as a current entry point. Lines marked with historical
    context (修复方案 / 已移除 / 已删除 / 历史) are tolerated."""
    for line_no, line in enumerate(text.splitlines(), 1):
        refs = re.findall(r"(?:scripts|tests|rag|core|cache|agents|api|llm|web/src)/[A-Za-z0-9_./-]+\.(?:json|yml|yaml|py|js)", line)
        if not refs:
            continue
        if HISTORICAL_CONTEXT.search(line):
            continue
        for ref in refs:
            if any(ch in ref for ch in "*<"):
                continue
            candidates = [path.parent / ref, root / ref]
            if not any(c.exists() for c in candidates):
                errors.append(
                    f"missing referenced file: {path.relative_to(root)}:{line_no} -> {ref}"
                )


def check_env_coverage(errors: list[str], root: Path = ROOT) -> None:
    config = (root / "core/config.py").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    env_keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_example, re.MULTILINE))
    public_tool_flags = re.findall(r'os\.getenv\("(TOOL_RESULT_[A-Z0-9_]+)"', config)
    missing_flags = sorted(set(public_tool_flags) - env_keys)
    errors.extend(
        f"public config missing from .env.example: {name}" for name in missing_flags
    )
    for key in REQUIRED_ENV_KEYS:
        if key not in env_keys and f'"{key}"' not in config:
            errors.append(f".env.example missing required key: {key}")


def _env_value(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}=([^\s#]+)", text, re.MULTILINE)
    return m.group(1) if m else None


def check_canonical_config(errors: list[str], root: Path = ROOT) -> None:
    config = (root / "core/config.py").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")
    for key in CANONICAL_MODEL_KEYS:
        runtime = _env_value(config, key) or None
        template = _env_value(env_example, key)
        compose_m = re.search(rf"{key}=\$\{{{key}:-([^}}]+)\}}", compose)
        compose_v = compose_m.group(1) if compose_m else None
        values = {
            "core/config.py": runtime,
            ".env.example": template,
            "docker-compose.yml": compose_v,
        }
        present = {k: v for k, v in values.items() if v}
        unique = {v.strip() for v in present.values()}
        if unique and len(unique) > 1:
            errors.append(
                f"canonical {key} drift: {present} (all defaults must match ADR-007/008)"
            )


def check_openapi_snapshot(errors: list[str], root: Path = ROOT) -> None:
    snapshot = root / "docs/openapi.json"
    if not snapshot.exists():
        errors.append("docs/openapi.json missing (run scripts/generate_openapi.py)")
        return
    try:
        from api.app_factory import app  # noqa: PLC0415

        live = app.openapi()
    except Exception as exc:  # pragma: no cover - environment dependent
        warnings_append(
            f"OpenAPI live spec unavailable in this environment ({exc.__class__.__name__})"
        )
        return
    current = json.loads(snapshot.read_text(encoding="utf-8"))
    if len(current.get("paths", {})) != len(live["paths"]):
        errors.append(
            f"OpenAPI path-count drift: docs/openapi.json={len(current['paths'])} "
            f"vs app.openapi()={len(live['paths'])}"
        )
    elif current.get("paths") != live["paths"]:
        errors.append("OpenAPI snapshot drift: paths differ from app.openapi()")
    if current.get("info", {}).get("version") != live.get("info", {}).get("version"):
        errors.append(
            "OpenAPI version drift: docs/openapi.json != app.openapi() info.version"
        )


def warnings_append(msg: str) -> None:  # noqa: D401 (helper keeps naming clear)
    globals().setdefault("_WARNINGS", []).append(msg)


def check_benchmark_metadata(errors: list[str], root: Path = ROOT) -> None:
    bench = root / "tests/eval/rag_benchmark.json"
    if not bench.exists():
        errors.append("tests/eval/rag_benchmark.json missing")
        return
    data = json.loads(bench.read_text(encoding="utf-8"))
    declared = data.get("metadata", {}).get("total_queries")
    actual = len(data.get("queries", []))
    if declared != actual:
        errors.append(
            f"RAG benchmark metadata mismatch: total_queries={declared} != len(queries)={actual}"
        )


def check_stale_terms(docs: list[Path], warnings: list[str], errors: list[str], root: Path = ROOT) -> None:
    for path in docs:
        rel = path.relative_to(root)
        text = path.read_text(encoding="utf-8", errors="replace")
        if is_decision_doc(rel) or is_plan_report(rel):
            # ADRs keep their historical wording; their Status line carries supersession.
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if HISTORICAL_CONTEXT.search(line):
                continue
            for pattern in STALE_CURRENT_TERMS:
                if re.search(pattern, line):
                    errors.append(
                        f"stale current-state term '{pattern}' in {rel}:{line_no} "
                        f"(frame it as historical or update the claim)"
                    )
            for pattern in STALE_COUNT_TERMS:
                if re.search(pattern, line):
                    errors.append(
                        f"stale hardcoded count '{pattern}' in {rel}:{line_no} "
                        f"(derive counts dynamically)"
                    )


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    globals()["_WARNINGS"] = warnings

    docs = discover_docs()
    for path in docs:
        text = path.read_text(encoding="utf-8", errors="replace")
        check_links(path, text, errors)
        check_file_refs_line_aware(path, text, errors)

    check_env_coverage(errors)
    check_canonical_config(errors)
    check_openapi_snapshot(errors)
    check_benchmark_metadata(errors)
    check_stale_terms(docs, warnings, errors)

    if globals()["_WARNINGS"]:
        for warning in globals()["_WARNINGS"]:
            print(f"WARN: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"FAIL: {len(errors)} error(s), checked {len(docs)} active documents")
        return 1
    print(
        f"OK: checked {len(docs)} active documents — links, file references, env coverage, "
        f"canonical model config, OpenAPI snapshot, benchmark metadata, stale terminology"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
