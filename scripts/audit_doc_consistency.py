#!/usr/bin/env python3
"""Deterministic guard for documentation/runtime drift (v3).

Discovery-based: scans active docs (README.md, CLAUDE.md, docs/**) and skips
explicit historical snapshots (archive/, milestone/, superpowers/, plans/,
marked banners). It reports drift as errors (exit 1) and soft observations as
warnings.

Checks:
  A. broken local markdown links (resolved strictly relative to the file)
  B. referenced scripts/tests/source files must exist
  C. public runtime config keys present in .env.example (strict)
  D. canonical model/config consistency: runtime fallback (AST-extracted from
     core/config.py) vs .env.example vs docker-compose explicit defaults
  E. OpenAPI snapshot drift (docs/openapi.json vs app.openapi() API surface —
     path→method map; full-JSON equality is fastapi/pydantic-version sensitive
     because requirements.txt floats those packages, so only the surface is a
     portable contract)
  F. RAG benchmark metadata integrity (total_queries == len(queries))
  G. prohibited stale current-state terminology in active docs
  H. referenced rag/core/web source files must exist
  I. hardcoded test counts framed as current truth (v3, rule A)
  J. canonical RAG evaluation command/doc references (v3, rules B/C)
  K. unproven current formal RAG metric claims while artifacts derive
      NOT_VERIFIED (v3, rule D)
   L. make targets referenced by current-truth docs must exist (v3, rule E)
   M. tracked+ignored repository hygiene (v3, rule F)
   Final-closeout guards: N. negative-existence claims (doc says a repo file
      is missing while it exists); P. stale fail-open embedding semantics
      (random-vector fallback); Q. unsupported latency absolutes (零延迟/
      亚毫秒/<10ms) without benchmark/estimate/historical context;
      R. production-grade status claims while production evidence is
      NOT_VERIFIED; S. env-shaped tokens in active docs must resolve to a
      real config surface or be marked historical/negative on the line.

Historical docs (with an explicit HISTORICAL banner) are excluded from
terminology checks but still pass through link/reference checks unless they
are excluded entirely.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
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

# ---- Guard N: active docs claiming a repo file does NOT exist while it does.
# Binds the negative claim to the path (before via 不包含…, after via 已删除/…)
# so unrelated "移除" wording on the same line does not false-positive.
NEG_CONTAINS_RE = re.compile(r"不包含|不含|不包括|不提供|not (?:include|ship)")
NEG_AFTER_RE = re.compile(r"已删除|已弃用|不存在|not exist|no longer|removed")
REPO_FILE_RE = re.compile(
    r"(?:scripts|tests|rag|core|cache|agents|api|llm|web/src|docs|deploy|auth|db|erp|media|alerts|knowledge)"
    r"/[A-Za-z0-9_./-]+\.(?:py|js|ts|json|yml|yaml|sh|md|html)"
)

# ---- Guard P: stale fail-open embedding semantics. The current
# cache/response_cache.py is fail-closed (embedding unavailable -> skip L2,
# NEVER a random-vector fallback); any doc/source text reintroducing the
# random-vector fallback claim is stale.
STALE_RANDOM_VECTOR_RE = re.compile(r"随机向量|random\s+vector|random-vector", re.IGNORECASE)
STALE_RANDOM_VECTOR_NEGATED_RE = re.compile(
    r"绝不|不要|禁止|不使用|不用|不再使用|无随机向量|没有随机向量|"
    r"never\s+use|no\s+random|without\s+random|never\s+creates?",
    re.IGNORECASE,
)

# ---- Guard Q: unsupported latency absolutes in current-truth/active docs.
LATENCY_ABSOLUTE_RE = re.compile(
    r"零延迟|零耗时|(?<![0-9])0\s*ms|零毫秒|亚毫秒|微秒级|毫秒级响应|"
    r"(?:<|&lt;|低于|≤|以内)\s*\d+\s*ms",
    re.IGNORECASE,
)
LATENCY_ALLOWED_RE = re.compile(
    r"估算|估计|estimate|设计目标|design target|目标值|不宣称|无法保证|取决于|"
    r"视.*而定|未测量|NOT_MEASURED|benchmark|基准|历史|Historical|snapshot|快照|当时|"
    r"旧|曾经|Superseded|迁移|已移除|2026-06"
)

# ---- Guard R: production-readiness framing. Production validation evidence
# (provider auth / billing / latency / real ERP) is NOT_VERIFIED; active docs
# may describe production-oriented design but must not claim production-grade
# status. Changelog/version-table rows are historical by construction.
PRODUCTION_CLAIM_RE = re.compile(
    r"(?:做到|达到)(?:了)?\s*生产级|已(?:做到|达到)生产级|生产级系统|生产级架构|"
    r"生产级多智能体|生产级平台|生产级客服|已完成生产验收|生产验收完成|生产验收通过|"
    r"已上线|上线验收通过|production-ready(?:\s+system)?|production validated",
    re.IGNORECASE,
)
PRODUCTION_ALLOWED_RE = re.compile(
    r"不宣称|不会宣称|不能直接宣称|不得宣称|无法宣称|尚未|未完成|没有完成|"
    r"NOT_VERIFIED|NOT_MEASURED|NOT_AVAILABLE|生产级要求|生产级目标|生产级设计|"
    r"生产化|production-oriented|面向生产|目标|estimate|估算|历史|Historical|"
    r"snapshot|快照|当时|Superseded|2026-06"
)
VERSION_TABLE_ROW_RE = re.compile(r"^\|\s*\*{0,2}v\d+\.\d+", re.IGNORECASE)

# ---- Guard S: env-shaped tokens referenced in active docs must resolve to a
# real config surface (core/config.py constant, env read in code,
# .env.example/.env.test key, compose/workflow env) or be marked
# historical/negative on the same line. Mechanizes the stale-env-name drift
# class (e.g. CACHE_TTL_PRODUCT / DB_POOL_SIZE previously drifted through
# production-operations-guide.md unchecked).
ENV_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# A line only carries an env-reference risk when the token appears in an
# env-usage context: KEY=value, yaml env entry (`KEY:` at line start), or the
# line names it as a 环境变量/.env/export/getenv surface. Bare mentions of
# code constants (NEGATION_PAIRS), audit labels, or shell placeholders stay
# out of scope.
ENV_USAGE_CONTEXT_RE = re.compile(r"环境变量|\.env|export\s|getenv|ENV\b|env\s*:", re.IGNORECASE)
ENV_ASSIGN_SHAPE_RE = re.compile(r"^\s*(?:-\s*)?[A-Z][A-Z0-9_]*\s*[:=]")
ENV_NEGATIVE_BEFORE_RE = re.compile(
    r"(?:不存在|已移除|已删除|已弃用|已废弃|废止|无|removed|deleted|deprecated|"
    r"no\s+longer)[\s/A-Za-z0-9_.,、（）()／]*$",
    re.IGNORECASE,
)
# Tokens that are env-shaped but are not environment variables: evidence
# status codes, failure-taxonomy labels, ERP table names, placeholders,
# timestamp formats, script-internal constants.
ENV_NON_VAR_ALLOWLIST = {
    "APPLICATION_MEASURED",
    "ATTACKER_IP",
    "AUTH_FAILED",
    "BD_CUSTOMER",
    "BD_MATERIAL",
    "BLOCKED_BY_AUTHENTICATION",
    "BLOCKED_VECTOR_INDEX",
    "EMBEDDING_PROVIDER_AUTH",
    "EMBEDDING_PROVIDER_UNAVAILABLE",
    "FUSION_RRF",
    "GOLD_NOT_INDEXED",
    "HALF_OPEN",
    "LOCAL_ONLY",
    "LOW_RANK",
    "MISS_ALL",
    "NON_RETRYABLE",
    "NOT_AVAILABLE",
    "NOT_MEASURED",
    "NOT_VERIFIED",
    "PROVIDER_AUTH",
    "PROVIDER_ERROR",
    "PROVIDER_REPORTED",
    "RERANKER_PROVIDER_AUTH",
    "RERANKER_PROVIDER_DEGRADED",
    "SAL_ORDER",
    "STK_INVENTORY",
    "VECTOR_INDEX_EMPTY",
    "WARM_QUERIES",
    "WELCOME_HTML",
    "YYYYMMDD_HHMMSS",
    "YOUR_API_KEY",
}


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
    # Strict contract: every REQUIRED_ENV_KEY must be present in .env.example.
    # Presence in core/config.py does NOT satisfy this — internal-only runtime
    # config must be removed from REQUIRED_ENV_KEYS (with a reason), not
    # quietly excused because it happens to exist in config.py.
    for key in REQUIRED_ENV_KEYS:
        if key not in env_keys:
            errors.append(f".env.example missing required key: {key}")


# Defaults accepted by config.py's env helpers. Values are AST literal
# constants, so we can read them without executing the module.
_ENV_HELPER_NAMES = {"os.getenv", "getenv", "_int_env", "_float_env"}


def _call_key_and_default(call: ast.Call) -> tuple[str | None, object | None]:
    """Extract (env_key, default) from a helper call like os.getenv("K", "d")."""
    if not call.args:
        return None, None
    key_node = call.args[0]
    if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
        return None, None
    key = key_node.value
    default = None
    if len(call.args) >= 2:
        default = getattr(call.args[1], "value", None)
    elif call.keywords:
        for kw in call.keywords:
            if kw.arg in {"default", "default_value"}:
                default = getattr(kw.value, "value", None)
    return key, default


def extract_runtime_env_defaults(config_path: Path) -> dict[str, str]:
    """AST-extract env-var fallback defaults from core/config.py.

    Recognizes assignments of the form
      KEY = os.getenv("KEY", "default")
      KEY = _int_env("KEY", 123)
      KEY = _float_env("KEY", 1.0)
    and returns {KEY: default} (as strings, matching .env/compose semantics).
    Values that are not plain constants (conditional expressions, computed
    fallbacks) are skipped rather than guessed.
    """
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    defaults: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0] if len(node.targets) == 1 else None
        if not isinstance(target, ast.Name) or not isinstance(target.id, str):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        dotted = ""
        if isinstance(func, ast.Attribute):
            base = getattr(func.value, "id", "")
            dotted = f"{base}.{func.attr}" if base else func.attr
        elif isinstance(func, ast.Name):
            dotted = func.id
        if dotted not in _ENV_HELPER_NAMES:
            continue
        key, default = _call_key_and_default(node.value)
        if key is None or default is None:
            continue
        if isinstance(default, bool):  # os.getenv(..., "true") str vs bool guard
            continue
        defaults[key] = str(default)
    return defaults


def _compose_default(compose_text: str, key: str) -> str | None:
    """Explicit Compose default like KEY=${KEY:-value}; None when Compose does
    not override the key at all (which is allowed)."""
    m = re.search(rf"{key}=\$\{{{key}:-([^}}]+)\}}", compose_text)
    return m.group(1) if m else None


def _env_value(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}=([^\s#]+)", text, re.MULTILINE)
    return m.group(1) if m else None


def check_canonical_config(errors: list[str], root: Path = ROOT) -> None:
    config_path = root / "core/config.py"
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")
    runtime_defaults = extract_runtime_env_defaults(config_path)
    for key in CANONICAL_MODEL_KEYS:
        runtime = runtime_defaults.get(key)
        template = _env_value(env_example, key)
        compose_v = _compose_default(compose, key)
        values = {
            "core/config.py": runtime,
            ".env.example": template,
            "docker-compose.yml": compose_v,
        }
        present = {k: v.strip() for k, v in values.items() if v}
        if runtime is not None and len(set(present.values())) > 1:
            errors.append(
                f"canonical {key} drift: {present} (all present defaults must "
                f"match runtime fallback per ADR-007/008)"
            )


def _openapi_surface(paths: dict) -> dict[str, list[str]]:
    """Environment-portable API surface: path → sorted method list.

    Full JSON equality of the paths object is fastapi/pydantic-version
    sensitive (requirements.txt floats both), so the drift contract is the
    surface, not the serialized schema content.
    """
    return {path: sorted(ops.keys()) for path, ops in sorted(paths.items())}


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
    live_surface = _openapi_surface(live["paths"])
    snapshot_surface = _openapi_surface(current.get("paths", {}))
    if len(snapshot_surface) != len(live_surface):
        errors.append(
            f"OpenAPI path-count drift: docs/openapi.json={len(snapshot_surface)} "
            f"vs app.openapi()={len(live_surface)}"
        )
    elif snapshot_surface != live_surface:
        errors.append(
            "OpenAPI snapshot drift: API surface (path→methods) differs from app.openapi()"
        )
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


# ---------------------------------------------------------------------------
# v3 drift rules (RAG evidence pipeline epoch)
# ---------------------------------------------------------------------------

# (I) Hardcoded test-collector counts framed as current truth. Active docs
# must derive counts dynamically (`pytest --collect-only -q` / `npm test`);
# dated historical snapshots are exempt via discovery/historical banners.
CURRENT_TEST_COUNT_PATTERNS = [
    (re.compile(r"收集\D{0,10}\d{3,}\s*(?:个|条)?\s*(?:测试|用例|tests?)"),
     "hardcoded pytest collected count framed as current"),
    (re.compile(r"pytest --collect-only.{0,80}?\d{3,}\s*(?:个|条)?\s*(?:测试|用例|tests?)"),
     "hardcoded pytest collected count framed as current"),
    (re.compile(r"(?:用例|测试)(?:总数|数量)\s*(?:约|=|为|:：)?\s*\d{3,}"),
     "hardcoded test-count claim framed as current"),
    (re.compile(r"npm test\b[^|\n]{0,40}=\s*\d+\s*/\s*\d+"),
     "hardcoded npm test N/N framing (current-truth claim)"),
    (re.compile(r"(?:all\s*|所有\s*|全部\s*)\d+\s*tests?\s*(?:pass|passed|通过)", re.IGNORECASE),
     "fixed 'all N tests pass' framing"),
]
# Lines that explicitly frame counts as dynamic command output are allowed.
DYNAMIC_COUNT_FRAMING = re.compile(
    r"npm test`?[^。\n]{0,12}(?:输出|结果)|输出为准|当前输出|以命令输出"
)

# (J) Canonical RAG documentation reference required in entry-point docs.
CANONICAL_RAG_DOC = "docs/reference/rag-evaluation.md"
RAG_ENTRY_DOCS = [
    "README.md",
    "CLAUDE.md",
    "docs/reference/current-state.md",
]
# Canonical RAG evaluation make targets that must be wired to the canonical
# evaluation scripts in the Makefile.
RAG_EVAL_MAKE_TARGETS = (
    "rag-eval-649", "rag-eval-649-preflight", "rag-eval-649-smoke", "rag-eval-import",
)

# (K) Metric families that must not be claimed as current formal numbers while
# the artifact-derived status is NOT_VERIFIED (via rag_evidence_status.py).
UNPROVEN_CURRENT_METRIC = re.compile(
    r"current.{0,24}(?:Hit@?\d|MRR|NDCG|Recall@\d)\D{0,12}\d{1,3}(?:\.\d+)?\s*%"
    r"|当前(?:的)?\s*(?:Hit@?\d|MRR|NDCG|Recall@\d).{0,20}\d{1,3}(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
METRIC_PROVENANCE_RE = re.compile(
    r"artifact|provenance|历史|historical|快照|snapshot|估算|estimate|fixture|"
    r"预计|目标|design goal|待重跑|待验证|NOT_VERIFIED|未验证",
    re.IGNORECASE,
)

# (L) make targets referenced by current-truth docs must be defined.
# Guard O generalization: ANY active doc referencing `make <target>` (not just
# the RAG eval targets or a fixed doc list) must point at a defined target.
MAKE_TARGET_REF_RE = re.compile(
    r"\bmake\s+(rag-eval-649-preflight|rag-eval-649-smoke|rag-eval-import|rag-eval-649|eval-rag)\b"
)
MAKE_ANY_TARGET_RE = re.compile(r"\bmake\s+([A-Za-z][A-Za-z0-9_-]*)")
# English-verb false positives ("can make a cached read stale").
MAKE_STOP_TARGETS = {
    "a", "an", "the", "it", "its", "sure", "use", "up", "of", "in", "on", "at",
    "to", "for", "and", "or", "that", "this", "all", "any", "no", "not", "me",
    "my", "your", "our", "them", "they", "we", "be", "been", "is", "are", "was",
    "were", "do", "does", "did", "done", "made", "sense", "difference",
    "decision", "call", "note", "few", "lot", "one",
}

# (M) Tracked files the repo deliberately keeps despite ignore patterns
# (documented in docs/reports/audit/**).
TRACKED_IGNORED_ALLOWED = {"CLAUDE.md"}


def text_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def formal_rag_metrics(root: Path = ROOT) -> str:
    """Artifact-derived RAG formal-metric status (drives rule K).

    Single source of truth: ``scripts/rag_evidence_status.py`` derives the
    status ONLY from provenance-bearing formal artifacts under
    ``artifacts/evaluation/rag-649/**/report.json``. Markdown text is never
    consulted here — docs cannot own (or promote) the evidence state.
    Returns "VERIFIED" or NOT_VERIFIED (fail-closed).
    """
    from rag_evidence_status import derive_rag_formal_status

    return derive_rag_formal_status(root)["rag_formal_metrics_status"]


def check_test_count_framing(docs: list[Path], errors: list[str], root: Path = ROOT) -> None:
    """Rule I: numbered test counts must not be written as current truth in
    active docs; dynamic framing (以 … 输出为准) is allowed."""
    for path in docs:
        rel = path.relative_to(root)
        for line_no, line in enumerate(text_lines(path), 1):
            for pattern, reason in CURRENT_TEST_COUNT_PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                if DYNAMIC_COUNT_FRAMING.search(line):
                    continue
                errors.append(
                    f"hardcoded test count framed as current truth ({reason}) "
                    f"in {rel}:{line_no} — derive counts with "
                    f"`pytest --collect-only -q` / `npm test`"
                )


def check_rag_eval_references(docs: list[Path], errors: list[str], root: Path = ROOT) -> None:
    """Rules J: the canonical RAG evaluation process must be resolvable —
    canonical doc exists, Makefile wires the documented targets to the
    canonical scripts, entry docs link the canonical reference, and the
    canonical doc itself carries the evidence-pipeline vocabulary."""

    canonical = root / CANONICAL_RAG_DOC
    if not canonical.exists():
        errors.append(
            f"canonical RAG evaluation reference missing: {CANONICAL_RAG_DOC}"
        )
        return

    makefile = root / "Makefile"
    if not makefile.exists():
        errors.append("Makefile missing: canonical RAG evaluation commands cannot be resolved")
    else:
        mk = makefile.read_text(encoding="utf-8")
        # Canonical RAG evaluation make targets and the scripts they must invoke.
        for name in RAG_EVAL_MAKE_TARGETS:
            if not re.search(
                rf"^{name}:[^\n]*\n(?:\t[^\n]*\n)*?\tpython3 scripts/(evaluate_rag|import_eval_corpus)\.py",
                mk,
                re.MULTILINE,
            ):
                errors.append(
                    f"Makefile target `{name}` missing or not wired to the "
                    f"canonical evaluation script "
                    f"(scripts/evaluate_rag.py / import_eval_corpus.py)"
                )

    entry_rels = {Path(rel_str) for rel_str in RAG_ENTRY_DOCS}
    for doc in docs:
        rel = doc.relative_to(root)
        if rel not in entry_rels or doc == canonical:
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        if "rag-evaluation.md" not in text:
            errors.append(
                f"{rel} presents RAG evaluation content but does not link the "
                f"canonical reference {CANONICAL_RAG_DOC}"
            )

    ctext = canonical.read_text(encoding="utf-8", errors="replace")
    for token in (
        "vector_only", "bm25_only", "hybrid_no_rerank", "hybrid_rerank",
        "all_queries", "retrieval_eligible", "full_gold_covered",
    ):
        if token not in ctext:
            errors.append(
                f"canonical RAG doc missing evidence-pipeline token: {token}"
            )


def check_unproven_current_metrics(docs: list[Path], errors: list[str], root: Path = ROOT) -> None:
    """Rule K: metric claims must follow the ARTIFACT-derived status.

    - NOT_VERIFIED (no valid formal artifact): current-truth docs must not
      claim current Hit/MRR/NDCG percentages without a provenance qualifier.
    - VERIFIED (valid formal artifact): claims are allowed but must still bind
      to provenance (artifact reference / provenance wording) on the line;
      the provenance check is NEVER fully disabled.
    """
    from rag_evidence_status import (
        STATUS_NOT_VERIFIED,
        STATUS_VERIFIED,
        derive_rag_formal_status,
    )

    evidence = derive_rag_formal_status(root)
    status = evidence["rag_formal_metrics_status"]
    if status not in (STATUS_NOT_VERIFIED, STATUS_VERIFIED):
        return  # defensive: nothing else may pass the gate
    truth_rels = {
        Path("README.md"),
        Path("CLAUDE.md"),
        Path("docs/reference/current-state.md"),
        Path("docs/reference/rag-evaluation.md"),
        Path("docs/evaluation/production-evidence.md"),
    }
    for path in docs:
        rel = path.relative_to(root)
        if rel not in truth_rels or rel == Path("docs/reference/rag-evaluation.md"):
            # The canonical doc states metric semantics and inherits the
            # artifact-derived status (that's rendered either way);
            continue
        for line_no, line in enumerate(text_lines(path), 1):
            if HISTORICAL_CONTEXT.search(line):
                continue
            if not UNPROVEN_CURRENT_METRIC.search(line):
                continue
            if METRIC_PROVENANCE_RE.search(line):
                continue
            if status == STATUS_NOT_VERIFIED:
                errors.append(
                    f"unproven current RAG metric claim in {rel}:{line_no} — "
                    f"formal 649-query metrics are NOT_VERIFIED (no valid formal "
                    f"artifact); add provenance or drop the number"
                )
            else:
                errors.append(
                    f"uncurrent-provenance RAG metric claim in {rel}:{line_no} — "
                    f"formal metrics derive VERIFIED from evidence artifact "
                    f"{evidence['rag_formal_artifact_path']}; bind the number to provenance "
                    f"(artifact reference) on the line"
                )


def check_makefile_doc_targets(
    errors: list[str], root: Path = ROOT, docs: list[Path] | None = None
) -> None:
    """Rule L / Guard O: make targets referenced by active docs must exist in
    the Makefile (a missing Makefile is only tolerated when nothing is
    referenced — synthetic repos)."""
    mk_path = root / "Makefile"
    mk = mk_path.read_text(encoding="utf-8") if mk_path.exists() else None
    defined = set(re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", mk, re.MULTILINE)) if mk else set()
    referenced: list[tuple[str, Path]] = []
    if docs is None:
        docs = [
            root / rel
            for rel in ("README.md", "CLAUDE.md", "docs/reference/current-state.md")
            if (root / rel).exists()
        ]
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        for hit in re.findall(MAKE_TARGET_REF_RE, text):
            referenced.append((hit, doc))
        for hit in re.findall(MAKE_ANY_TARGET_RE, text):
            if hit in MAKE_STOP_TARGETS or hit.startswith("-"):
                continue
            referenced.append((hit, doc))
    for target, doc in referenced:
        if target not in defined:
            context = (
                "not defined in the Makefile"
                if mk is not None
                else "but the Makefile does not exist at all"
            )
            errors.append(
                f"{doc.relative_to(root) if root in doc.parents else doc} references "
                f"make target `{target}` {context}"
            )


def check_tracked_ignored_files(errors: list[str], warnings: list[str], root: Path = ROOT) -> None:
    """Rule M: repository hygiene — files both git-tracked and ignored must be
    surfaced (runtime sidecars/secrets accumulate here silently)."""

    tracked_ignored = git_tracked_ignored(root)
    if tracked_ignored is None:
        return
    for path in tracked_ignored:
        if path in TRACKED_IGNORED_ALLOWED:
            continue
        errors.append(
            f"repository hygiene: tracked+ignored file not removed from index: {path} "
            f"(git rm --cached it)"
        )


def git_tracked_ignored(root: Path = ROOT) -> list[str] | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "-ci", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # not a git repo (e.g. synthetic test fixture): skip quietly
        return None
    return [p for p in out.stdout.splitlines() if p.strip()]


def check_negative_existence_claims(
    docs: list[Path], errors: list[str], root: Path = ROOT
) -> None:
    """Guard N: active docs must not claim a repo file is missing while it
    exists (and vice versa is covered by check_file_refs_line_aware). The
    negative claim must bind to the path (直接前缀 不包含…，或路径后紧跟
    已删除/已移除…), so unrelated "移除" wording on the same line is ignored."""

    for path in docs:
        rel = path.relative_to(root)
        for line_no, line in enumerate(text_lines(path), 1):
            refs = REPO_FILE_RE.findall(line)
            if not refs:
                continue
            for ref in refs:
                if any(ch in ref for ch in "*<"):
                    continue
                if not (root / ref).exists():
                    continue  # missing-file refs handled by file-ref guards
                claimed_missing = False
                for hit in re.finditer(re.escape(ref), line):
                    before = line[max(0, hit.start() - 40): hit.start()]
                    after = line[hit.end(): hit.end() + 24]
                    if NEG_CONTAINS_RE.search(before) or NEG_AFTER_RE.match(after.strip()):
                        claimed_missing = True
                        break
                if claimed_missing:
                    errors.append(
                        f"negative-existence claim drift: {rel}:{line_no} says "
                        f"`{ref}` does not exist but the file is present in the "
                        f"repo — update the doc or the claim"
                    )


def check_stale_embedding_fallback(
    docs: list[Path], errors: list[str], root: Path = ROOT
) -> None:
    """Guard P: the response cache is fail-closed on embedding failure
    (EmbeddingUnavailableError -> skip L2; never a random vector). Ban the
    random-vector fallback wording from cache source and active docs."""

    targets: list[tuple[Path, bool]] = [
        (root / "cache" / "response_cache.py", False),
    ]
    for doc in docs:
        targets.append((doc, True))
    for path, is_doc in targets:
        if not path.exists():
            continue
        for line_no, line in enumerate(text_lines(path), 1):
            if not STALE_RANDOM_VECTOR_RE.search(line):
                continue
            if not is_doc and STALE_RANDOM_VECTOR_NEGATED_RE.search(line):
                continue
            if is_doc and (
                HISTORICAL_CONTEXT.search(line)
                or STALE_RANDOM_VECTOR_NEGATED_RE.search(line)
            ):
                continue
            where = (
                path.relative_to(root) if root in path.parents or path.parent == root
                else path
            )
            errors.append(
                f"stale embedding fallback semantics: {where}:{line_no} claims a "
                f"random-vector fallback — response cache is fail-closed "
                f"(embedding unavailable -> skip L2, no random vector)"
            )


def check_latency_absolutes(
    docs: list[Path], errors: list[str], root: Path = ROOT
) -> None:
    """Guard Q: current-truth/active docs must not state latency absolutes
    (零延迟/亚毫秒/<10ms/…) without a same-line benchmark/estimate/historical
    qualifier. In-process mechanism descriptions (no numbers) stay allowed."""

    for path in docs:
        rel = path.relative_to(root)
        if is_decision_doc(rel):
            continue  # ADR bodies are frozen decision records (status line rules)
        for line_no, line in enumerate(text_lines(path), 1):
            hit = LATENCY_ABSOLUTE_RE.search(line)
            if not hit:
                continue
            if LATENCY_ALLOWED_RE.search(line):
                continue
            errors.append(
                f"unsupported latency absolute ({hit.group(0)}) in {rel}:{line_no} "
                f"— state the mechanism (skip Router/Agent/LLM) instead of an "
                f"unmeasured latency number, or bind it to a benchmark/historical "
                f"artifact"
            )


def check_production_claims(
    docs: list[Path], errors: list[str], root: Path = ROOT
) -> None:
    """Guard R: production-grade status claims are not allowed as current
    facts while provider/production evidence is NOT_VERIFIED. Design-oriented
    framing (生产化/面向生产/生产级要求) and disclaimers stay allowed;
    changelog/version-table rows are historical."""

    for path in docs:
        rel = path.relative_to(root)
        if is_decision_doc(rel):
            continue  # ADR bodies are frozen decision records (status line rules)
        for line_no, line in enumerate(text_lines(path), 1):
            if not PRODUCTION_CLAIM_RE.search(line):
                continue
            if VERSION_TABLE_ROW_RE.match(line.strip()) or PRODUCTION_ALLOWED_RE.search(line):
                continue
            errors.append(
                f"unsupported production-grade claim in {rel}:{line_no} — "
                f"production validation evidence is NOT_VERIFIED; use "
                f"production-oriented framing (生产化架构/生产工程化设计) or a "
                f"disclaimer instead"
            )


def collect_known_env_keys(root: Path = ROOT) -> frozenset[str]:
    """Machine-derived env surface: core/config.py constants + env reads in
    any Python module + .env.example/.env.test keys + compose/deploy/workflow
    env references. Over-collection is safe (guard fails toward allowing)."""

    keys: set[str] = set()

    config_path = root / "core" / "config.py"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8", errors="replace")
        keys |= set(re.findall(r"^([A-Z][A-Z0-9_]{2,})\s*=", text, re.MULTILINE))

    py_read_re = re.compile(
        r"(?:os\.getenv|(?<![A-Za-z_])getenv|_int_env|_float_env|_str_env|"
        r"os\.environ\.get)\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
        r"|os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)[\"']\s*\]"
    )
    for path in root.rglob("*.py"):
        if SKIP_PARTS.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in py_read_re.finditer(text):
            keys.add(match.group(1) or match.group(2))

    for name in (".env.example", ".env.test"):
        p = root / name
        if p.exists():
            keys |= set(
                re.findall(
                    r"^([A-Z][A-Z0-9_]+)=",
                    p.read_text(encoding="utf-8", errors="replace"),
                    re.MULTILINE,
                )
            )

    yml_paths: list[Path] = list(root.glob("docker-compose*.yml"))
    deploy_dir = root / "deploy"
    if deploy_dir.is_dir():
        yml_paths.extend(deploy_dir.rglob("*.yml"))
        yml_paths.extend(deploy_dir.rglob("*.yaml"))
    workflows_dir = root / ".github" / "workflows"
    if workflows_dir.is_dir():
        yml_paths.extend(workflows_dir.glob("*.yml"))
        yml_paths.extend(workflows_dir.glob("*.yaml"))
    for path in yml_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        keys |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", text))
        keys |= set(
            re.findall(
                r"^\s*(?:-\s*)?([A-Z][A-Z0-9_]{2,})\s*[:=]\s*[\"']?\S",
                text,
                re.MULTILINE,
            )
        )

    return frozenset(keys)


def check_env_references(docs: list[Path], errors: list[str], root: Path = ROOT) -> None:
    """Guard S: env-shaped tokens in active docs must resolve to a real config
    surface, or the line must mark them historical/negative (不存在/已移除/…).
    This is the doc-side counterpart of check_env_coverage."""

    known = collect_known_env_keys(root)
    for path in docs:
        rel = path.relative_to(root)
        for line_no, line in enumerate(text_lines(path), 1):
            if HISTORICAL_CONTEXT.search(line):
                continue
            if not ENV_USAGE_CONTEXT_RE.search(line):
                continue
            reported: set[str] = set()
            for match in ENV_TOKEN_RE.finditer(line):
                token = match.group(0)
                if token in known or token in ENV_NON_VAR_ALLOWLIST or token in reported:
                    continue
                tail = line[match.end():]
                # Flag when the token is assigned (KEY=value / KEY: value) or
                # the line explicitly frames it as env usage (环境变量/.env/
                # export/getenv). Bare prose mentions of code constants stay
                # out of scope.
                assigned = bool(re.match(r"\s*[:=]", tail)) or bool(
                    ENV_ASSIGN_SHAPE_RE.search(line.strip())
                    and line.strip().startswith(token)
                )
                if not assigned and not ENV_USAGE_CONTEXT_RE.search(line):
                    continue
                if ENV_NEGATIVE_BEFORE_RE.search(line[: match.start()]):
                    continue
                reported.add(token)
                errors.append(
                    f"unresolved env reference: {rel}:{line_no} mentions "
                    f"`{token}` — not found in core/config.py, code env reads, "
                    f".env.example/.env.test, or compose/workflow env; fix the "
                    f"name or mark the line historical/negative"
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
    check_test_count_framing(docs, errors)
    check_rag_eval_references(docs, errors)
    check_unproven_current_metrics(docs, errors)
    check_makefile_doc_targets(errors, docs=docs)
    check_tracked_ignored_files(errors, warnings)
    # Final-closeout guards (2026-09-30): reverse existence claims, stale
    # embedding semantics, latency absolutes, production framing.
    check_negative_existence_claims(docs, errors)
    check_stale_embedding_fallback(docs, errors)
    check_latency_absolutes(docs, errors)
    check_production_claims(docs, errors)
    check_env_references(docs, errors)

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
        f"canonical model config, OpenAPI snapshot, benchmark metadata, stale terminology, "
        f"test-count framing, RAG eval references, unproven metric claims, "
        f"make targets (all active docs), tracked-ignored hygiene, "
        f"negative-existence claims, stale embedding-fallback semantics, "
        f"latency absolutes, production framing, env references"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
