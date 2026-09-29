#!/usr/bin/env python3
"""Small, deterministic guard for documentation/runtime drift.

This intentionally checks stable references and configuration contracts, not prose.
It reports historical terminology as a warning because archived snapshots are allowed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_SUFFIXES = {".md", ".rst", ".txt"}
SKIP_PARTS = {".git", "node_modules", "dist", "__pycache__", ".venv", ".worktrees", ".claude"}
CURRENT_DOCS = {
    Path("README.md"),
    Path("CLAUDE.md"),
    Path("docs/README.md"),
    Path("docs/design/architecture-design.md"),
    Path("docs/design/context-engineering.md"),
    Path("docs/standards/evidence-driven-engineering-loop.md"),
    Path("docs/evaluation/production-evidence.md"),
    Path("docs/security/public-repository-secret-policy.md"),
    Path("docs/checklists/production-readiness-checklist.md"),
    Path("docs/operations/production-operations-guide.md"),
    Path("docs/reports/releases/changelog.md"),
    Path("docs/reports/resume-description.md"),
    Path("docs/design/interview-intro.md"),
    Path("docs/design/interview-deep-dive.md"),
    Path("docs/interview-questions-final.md"),
    Path("docs/decisions/005-dual-layer-cache.md"),
    Path("docs/decisions/006-cache-and-tool-result-context-architecture.md"),
    Path("docs/reports/plans/2026-09-29-code-doc-alignment.md"),
}


def docs() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.suffix.lower() in DOC_SUFFIXES
        and not SKIP_PARTS.intersection(p.parts)
        and p.relative_to(ROOT) in CURRENT_DOCS
    ]


def local_links(path: Path, text: str):
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = match.group(1).split("#", 1)[0].strip().strip("<>")
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        yield target


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    all_docs = docs()
    for path in all_docs:
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in local_links(path, text):
            candidates = [path.parent / target, ROOT / target]
            if not any(candidate.exists() for candidate in candidates):
                errors.append(f"broken local link: {path.relative_to(ROOT)} -> {target}")
        for ref in re.findall(r"(?:scripts|tests)/[A-Za-z0-9_./-]+\.py", text):
            if (
                ref in {"scripts/analyze_query_diversity.py", "scripts/cleanup_expired_sessions.py"}
                and "已移除" in text
            ):
                continue
            if (
                ref in {"scripts/probe_provider_auth.py", "scripts/run_production_evidence.py"}
                and "不包含" in text
            ):
                continue
            if not (ROOT / ref).exists():
                warnings.append(
                    f"missing referenced Python file: {path.relative_to(ROOT)} -> {ref}"
                )
            if path.name in {"README.md", "CLAUDE.md"} and "ChromaDB" in text:
                warnings.append(
                    f"VALID_HISTORICAL_REFERENCE: ChromaDB terminology in {path.relative_to(ROOT)}"
                )

    config = (ROOT / "core/config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    public_tool_flags = re.findall(r'os\.getenv\("(TOOL_RESULT_[A-Z0-9_]+)"', config)
    missing_flags = sorted(
        set(public_tool_flags) - set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_example, re.MULTILINE))
    )
    errors.extend(f"public config missing from .env.example: {name}" for name in missing_flags)
    runtime_default = re.search(
        r'REACT_MAX_ITERATIONS.*_int_env\("REACT_MAX_ITERATIONS",\s*(\d+)\)', config
    )
    example_value = re.search(r"^REACT_MAX_ITERATIONS=(\d+)", env_example, re.MULTILINE)
    if runtime_default and example_value and runtime_default.group(1) != example_value.group(1):
        errors.append("REACT_MAX_ITERATIONS differs between core/config.py and .env.example")

    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"OK: checked {len(all_docs)} text documents, local links, script references, and public env contracts"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
