#!/usr/bin/env python3
"""Guard the public-repository boundary of this project.

Three things drift back into a public repo long after the cleanup that removed
them: job-seeking material, internal/employer identifiers, and unsanitised
sample data. This script is the mechanical counter to that drift, and it is
deliberately *narrow*: every rule is a rule that has actually been violated in
this repository's own history, plus the PII shapes a seed corpus must never
carry.

Design rules that keep the guard honest:

* **Content is never printed.** A finding is ``path:line`` + rule id + category.
  A boundary violation must never become a way to leak the very bytes it caught.
* **Every rule carries an exemption.** A keyword rule without an exemption for
  the technical meaning of the same keyword is a false-positive generator; see
  ``docs/limitations.md`` and the ``RESUME``/``CANDIDATE`` anchors below.
* **New rules need tests.** ``tests/unit/test_public_repository_boundary.py``
  carries a positive and a negative case per rule.

Scope of the default mode is the tracked worktree, matching
``scripts/check_secrets.py``. ``--history`` additionally scans every unique
historical blob; it is expected to report the retired files listed in
``DATA_PROVENANCE.md`` §4 until an authorised history rewrite happens.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROVENANCE_DOC = ROOT / "DATA_PROVENANCE.md"
SECRET_GUARD = ROOT / "scripts" / "check_secrets.py"


@dataclass(frozen=True)
class Finding:
    location: str
    rule_id: str
    category: str


@dataclass(frozen=True)
class Rule:
    """One boundary rule.

    ``scopes``       repo-relative glob prefixes; empty means "every tracked file".
    ``exempt_paths`` exact repo-relative paths that never trip this rule.
    ``exempt_prefixes`` path prefixes (directory or file) that never trip it.
    ``anchors``      if the *same line* matches any anchor, the rule does not
                     fire. This is how "resume"/"candidate"/"offer" keep their
                     engineering meaning.
    """

    rule_id: str
    category: str
    pattern: re.Pattern[str]
    description: str
    scopes: tuple[str, ...] = ()
    exempt_paths: frozenset[str] = frozenset()
    exempt_prefixes: tuple[str, ...] = ()
    anchors: tuple[re.Pattern[str], ...] = ()


def _c(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# --------------------------------------------------------------------------
# Technical anchors. A line that carries one of these is talking about the
# software, not about a job, and keyword rules stand down.
# --------------------------------------------------------------------------
_LANGGRAPH_ANCHOR = _c(
    r"langgraph|stategraph|checkpoint|interrupt\b|command\s*\(|invoke_graph|"
    r"resumed_at|resume_dispatched|mark_resumed|consume_resume|"
    r"thread|retry|retries|dead[_-]?letter|\bdlq\b|lease|worker|celery|"
    r"reviewer|approv|separat|audit|privacy|redact"
)
_RETRIEVAL_ANCHOR = _c(
    r"retriev|\brag\b|bm25|rrf|rerank|index|gold|eval|embedding|qdrant|"
    r"candidate|span|truncat|compress|slice|chunk|quer(y|ies)"
)
_SECURITY_ANCHOR = _c(
    r"\bsre\b|\bacl\b|\biam\b|\bseo\b|offer\s+to\s+sell|offers\s+no|"
    r"option\s+to\s+sell|recruit|candidate\s+(?:queue|set|result)"
)

# Files whose content is a quoted *specification* of forbidden material: they
# exist to say "do not do this". Naming the thing is not doing it.
_NEGATION_MARKER = _c(
    r"不得|禁止|不应|不包含|不宣称|已删除|已移除|已废弃|不再|移除|改为|改写|"
    r"无\s*provenance|未验证|漂移|forbid|never|do not|don't|must not|"
    r"no longer|removed|retired|reframe|out of scope|"
    r"NOT_MEASURED|NOT_VERIFIED|NOT_APPLICABLE"
)

_DOCS_SCOPES = ("README.md", "CLAUDE.md", "DATA_PROVENANCE.md", "docs/", "web/", "src/")

# The guard and its tests necessarily *contain* the forbidden literals: they are
# the pattern source and the positive test cases. They are the only files exempt
# from every keyword rule, and nothing else is.
GUARD_SELF_PATHS = frozenset(
    {
        "scripts/check_public_repository_boundary.py",
        "tests/unit/test_public_repository_boundary.py",
    }
)

RULES: tuple[Rule, ...] = (
    # ---------------------------------------------------------------- A: career
    Rule(
        rule_id="A1-career-filename",
        category="career-material/filename",
        pattern=_c(r"."),
        description=(
            "A tracked path looks like job-seeking material "
            "(resume / CV / interview guide / cover letter / portfolio)."
        ),
        exempt_prefixes=(
            # Engineering files that merely contain the substrings.
            "tests/",
            "scripts/",
            "alembic/",
            "artifacts/",
            "web/src/",
            "runtime/",
            "core/",
            "api/",
        ),
    ),
    Rule(
        rule_id="A2-career-term-zh",
        category="career-material/chinese-term",
        pattern=_c(
            r"简历|面试|求职|应聘|自荐|招聘|履历|内推|实习|转正|晋升|年终奖|谈薪|"
            r"背调|离职证明|入职|述职|绩效考核"
        ),
        description="Chinese job-seeking / hiring vocabulary in a public engineering repo.",
        anchors=(_NEGATION_MARKER,),
    ),
    Rule(
        rule_id="A3-career-term-en",
        category="career-material/english-phrase",
        pattern=_c(
            r"resume\s+(?:claim|bullet|line)s?|curriculum\s+vitae|"
            r"interview\s+(?:guide|question|answer|script|prep)|"
            r"(?:30|90)[- ]second\s+pitch|elevator\s+pitch|"
            r"tell me about yourself|why should we hire|"
            r"what you must not say|things you must not say|"
            r"behavioural\s+question|behavioral\s+question|\bSTAR\s+story\b|"
            r"job\s+description|\bJD\b\s*(?:for|of)\b"
        ),
        description="English job-seeking / interview-prep phrasing.",
        anchors=(_NEGATION_MARKER,),
    ),
    Rule(
        rule_id="A4-career-narrative",
        category="career-material/personal-narrative",
        pattern=_c(
            r"post[- ]departure|离职后|任职期间|at my previous (?:company|employer)|"
            r"I worked at|我的上一家|前东家|原公司"
        ),
        description="First-person employment narrative about the maintainer.",
        anchors=(_NEGATION_MARKER,),
    ),
    Rule(
        rule_id="A5-pitch-cards",
        category="career-material/rehearsed-pitch",
        pattern=_c(
            r"六张母卡|aligned\s+talking\s+points|master\s+cards?|"
            r"current\s+aligned\s+answer|aligned\s+wording|"
            r"30 秒介绍|90 秒介绍"
        ),
        description="Rehearsed external-review pitch cards rather than engineering docs.",
        anchors=(_NEGATION_MARKER,),
    ),
    # ------------------------------------------------- B: internal identifiers
    Rule(
        rule_id="B1-internal-codename",
        category="internal-identity/product-codename",
        pattern=re.compile("药妆智多星"),
        description="Internal product codename of a non-public product line.",
    ),
    Rule(
        rule_id="B2-formal-material-disclosure",
        category="internal-identity/formal-material-disclosure",
        pattern=_c(r"正规材料|正式材料(?:参考|依据)|官方(?:开源|发布)项目"),
        description="Text disclosing the artifact's official/career submission purpose.",
        anchors=(_NEGATION_MARKER,),
    ),
    Rule(
        rule_id="B3-employer-attribution",
        category="internal-identity/employer-team",
        pattern=_c(r"开发团队\s*\*?\*?\s*$|our\s+(?:company|employer|org)\b"),
        description="Text attributing the work to an employer org rather than this repo.",
        anchors=(_NEGATION_MARKER,),
    ),
    # ------------------------------------------------------- C: self-scoring
    Rule(
        rule_id="C1-self-awarded-score",
        category="self-promotion/self-awarded-score",
        pattern=_c(
            r"极致级|超越\s*99\.9%|强烈推荐立即上线|可以放心上线|近乎完美|"
            r"综合评分\s*\*{0,2}\s*[:：|]?\s*\*{0,2}9[0-9](?:\.[0-9])?\s*/\s*100|"
            r"评分提升\s*[:：]?\s*9[0-9](?:\.[0-9])?\s*→"
        ),
        description="Self-awarded acceptance score / go-live hype in a repo that declares NOT_VERIFIED.",
        anchors=(_NEGATION_MARKER,),
    ),
    # ------------------------------------------- D: outbound business metrics
    Rule(
        rule_id="D1-outbound-business-kpi",
        category="internal-business/outbound-kpi-numbers",
        pattern=_c(
            r"FCR\s*7[0-9]\s*%|人效\s*[23]\s*[倍x]|人工人效提升\s*[23]\s*倍|"
            r"(?:LLM\s*调用(?:量)?(?:降低|减少)|Token\s*成本(?:下降|降低))\s*\d+\s*%"
        ),
        description=(
            "A specific outbound business KPI (first-contact-resolution rate, "
            "labour-efficiency multiple, LLM-call / token-cost reduction) reproduced "
            "as a number. The governance rule may stay; the figure may not."
        ),
        anchors=(_NEGATION_MARKER,),
    ),
    # ----------------------------------------------------- E: sample data PII
    Rule(
        rule_id="E1-seed-pii",
        category="sample-data/pii-literal",
        pattern=_c(
            r"(?<!\d)1[3-9]\d{9}(?!\d)"  # CN mobile
            r"|(?<!\d)\d{17}[\dXx](?!\d)"  # CN national ID
            r"|(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
            r"|\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"  # e-mail
            r"|(?<!\d)(?:\d[ -]?){16,19}(?!\d)"  # card-like digit runs
        ),
        description=(
            "The shipped sample corpus must be synthetic: no phone numbers, "
            "national IDs, e-mail addresses or card-shaped digit runs."
        ),
        scopes=("data/seed/",),
        exempt_prefixes=("data/seed/README",),
    ),
    Rule(
        rule_id="E2-seed-endpoint",
        category="sample-data/live-endpoint",
        pattern=_c(r"https?://(?!localhost|127\.0\.0\.1|example\.(?:com|org|net))[a-z0-9.-]+"),
        description="A non-placeholder URL inside the shipped sample corpus.",
        scopes=("data/seed/",),
    ),
)

# Filename patterns for A1, kept separate so the rule body stays declarative.
CAREER_FILENAME_RE = _c(
    r"(?:^|/)(?:interview[-_ ]?guide|resume|cv|curriculum[-_ ]?vitae|"
    r"cover[-_ ]?letter|portfolio|job[-_ ]?description)"
    r"(?:[-_ ][a-z0-9]+)*\b"
)
CAREER_FILENAME_SUFFIX_RE = _c(r"\.(?:md|rst|txt)$")

_FENCE_RE = _c(r"^\s*```|^\s*(?:>>>|\.\.\s|//|#\s{0,3})")
_WORDY_SUFFIX_RE = re.compile(r"[一-鿿]")


# --------------------------------------------------------------------------
# scope helpers
# --------------------------------------------------------------------------
def _matches_scope(rel: str, scopes: tuple[str, ...]) -> bool:
    if not scopes:
        return True
    return any(rel == s or rel.startswith(s) for s in scopes)


def _is_exempt(rel: str, rule: Rule) -> bool:
    if rel in rule.exempt_paths:
        return True
    return any(rel.startswith(p) for p in rule.exempt_prefixes)


def _anchor_saves(line: str, rule: Rule) -> bool:
    return any(a.search(line) for a in rule.anchors)


def iter_lines(text: str) -> Iterator[tuple[int, str]]:
    return enumerate(text.splitlines(), 1)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------
def scan_text(location: str, rel: str, text: str) -> list[Finding]:
    """Return boundary findings for one file. Content is never returned."""
    if rel in GUARD_SELF_PATHS:
        return []

    findings: list[Finding] = []

    if CAREER_FILENAME_RE.search(rel) and CAREER_FILENAME_SUFFIX_RE.search(rel):
        a1 = next(r for r in RULES if r.rule_id == "A1-career-filename")
        if not _is_exempt(rel, a1):
            findings.append(Finding(location, a1.rule_id, a1.category))

    for index, line in iter_lines(text):
        for rule in RULES:
            if rule.rule_id == "A1-career-filename":
                continue
            if not _matches_scope(rel, rule.scopes):
                continue
            if _is_exempt(rel, rule):
                continue
            if not rule.pattern.search(line):
                continue
            if _anchor_saves(line, rule):
                continue
            findings.append(Finding(f"{location}:{index}", rule.rule_id, rule.category))
    return findings


def tracked_paths() -> Iterable[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    for raw in output.split(b"\0"):
        if raw:
            yield Path(raw.decode("utf-8"))


def scan_current_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_paths():
        absolute = root / path
        if not absolute.is_file():
            continue
        text = _decode(absolute.read_bytes())
        if text is None:
            continue
        rel = path.as_posix()
        findings.extend(scan_text(rel, rel, text))
    return findings


def iter_batch_blobs(stdout: bytes, entries: list[list[str]]) -> Iterator[tuple[str, str, bytes]]:
    """Walk ``git cat-file --batch`` output and yield ``(sha, path, payload)``.

    ``git cat-file --batch`` frames every object as ``<sha> <type> <size>\\n``
    followed by ``size`` payload bytes and one ``\\n``. The type token is
    **bytes**, so it must be compared against ``b"blob"``; comparing against
    ``"blob"`` silently skips every object and turns the history scan into a
    no-op that reports "clean" — the worst possible failure mode for a guard.
    """
    stream = memoryview(stdout)
    offset = 0
    for entry in entries:
        object_id, path = entry[0], entry[1]
        header_end = stdout.find(b"\n", offset)
        if header_end == -1:
            return
        header = stdout[offset:header_end].split()
        offset = header_end + 1
        if len(header) < 3:
            continue
        size = int(header[2])
        payload = bytes(stream[offset : offset + size])
        offset += size + 1
        if header[1] != b"blob":
            continue
        yield object_id, path, payload


def known_history_blob_ids() -> set[str]:
    """Resolve the reviewed baseline snapshot; missing baseline fails closed."""
    baseline = Path(__file__).with_name("public_history_baseline.json")
    refs = json.loads(baseline.read_text(encoding="utf-8"))["refs"]
    present: list[str] = []
    for ref in refs:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}^{{object}}"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            present.append(ref)
    if not present:
        raise RuntimeError("No reviewed historical baseline refs are available")
    output = subprocess.check_output(["git", "rev-list", "--objects", *present])
    return {line.split(" ", 1)[0] for line in output.decode("utf-8", errors="replace").splitlines()}


def scan_history(new_only: bool = False) -> list[Finding]:
    """Audit reachable historical blobs; optionally flag only post-baseline ones.

    Full inventory returns a nonzero status for known legacy material; scheduled
    gate checks only objects not reachable from the immutable reviewed snapshot.
    """
    findings: list[Finding] = []
    objects = subprocess.check_output(["git", "rev-list", "--objects", "--all"]).decode(
        "utf-8", errors="replace"
    )
    entries = [line.split(" ", 1) for line in objects.splitlines() if " " in line]
    if new_only:
        known = known_history_blob_ids()
        entries = [entry for entry in entries if entry[0] not in known]
    if not entries:
        return findings
    process = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=("\n".join(entry[0] for entry in entries) + "\n").encode("ascii"),
        capture_output=True,
        check=True,
    )
    seen: set[str] = set()
    for object_id, path, payload in iter_batch_blobs(process.stdout, entries):
        if object_id in seen:
            continue
        seen.add(object_id)
        text = _decode(payload)
        if text is None:
            continue
        findings.extend(scan_text(f"{object_id[:12]}:{path}", path, text))
    return findings


def run_secret_guard(root: Path, history: bool = False, new_only: bool = False) -> list[Finding]:
    """Delegate to the existing credential scanner instead of duplicating it."""
    if not SECRET_GUARD.is_file():
        return [Finding(str(SECRET_GUARD), "G1-secret-guard-missing", "governance/missing-tool")]
    cmd = [sys.executable, str(SECRET_GUARD)]
    if history:
        cmd.append("--history")
    if new_only:
        cmd.append("--new-only")
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if result.returncode == 0:
        return []
    return [
        Finding(
            "<scripts/check_secrets.py>", "G1-secret-guard-failed", "credentials/high-confidence"
        )
        for _ in range(len([ln for ln in result.stdout.splitlines() if ln.startswith("- ")]))
    ] or [
        Finding(
            "<scripts/check_secrets.py>", "G1-secret-guard-failed", "credentials/high-confidence"
        )
    ]


def check_required_surface(root: Path) -> list[Finding]:
    """Structural rules that cannot be expressed as line patterns."""
    findings: list[Finding] = []
    if not (root / "DATA_PROVENANCE.md").is_file():
        findings.append(
            Finding(
                "DATA_PROVENANCE.md",
                "F1-provenance-doc-present",
                "governance/missing-provenance-doc",
            )
        )
        return findings
    readme = (root / "README.md").read_text(encoding="utf-8", errors="replace")
    if "DATA_PROVENANCE.md" not in readme:
        findings.append(
            Finding(
                "README.md",
                "F2-readme-links-provenance",
                "governance/readme-missing-provenance-link",
            )
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan every unique historical blob (slow; expected to report retired files)",
    )
    parser.add_argument(
        "--skip-secrets",
        action="store_true",
        help="do not delegate to scripts/check_secrets.py (used by the unit tests)",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="with --history, block newly reachable historical violations beyond the baseline",
    )
    args = parser.parse_args(argv)
    if args.new_only and not args.history:
        parser.error("--new-only requires --history")

    root = ROOT
    findings = check_required_surface(root)
    findings.extend(scan_current_tree(root))
    if not args.skip_secrets:
        findings.extend(run_secret_guard(root))
    if args.history:
        history_findings = scan_history(new_only=args.new_only)
        if not args.skip_secrets:
            history_findings.extend(
                run_secret_guard(root, history=True, new_only=args.new_only)
            )
    else:
        history_findings = []

    if findings:
        print("Public-repository boundary findings (content intentionally omitted):")
        for finding in findings:
            print(f"- {finding.location}: {finding.rule_id} [{finding.category}]")
        print(f"FAIL: {len(findings)} boundary violation(s) in the current tree.")
        print("See DATA_PROVENANCE.md for the full boundary contract.")
        return 1

    print(
        "Public-repository boundary OK: no career material, internal identifiers, "
        "self-awarded scores, outbound business KPIs or PII literals in tracked files; "
        "provenance surface present; credential guard green."
    )
    if history_findings:
        print(
            f"HISTORY: {len(history_findings)} finding(s) in reachable history "
            "(expected until an authorised history rewrite — see DATA_PROVENANCE.md §4)."
        )
        for finding in history_findings[:20]:
            print(f"- {finding.location}: {finding.rule_id} [{finding.category}]")
        if len(history_findings) > 20:
            print(f"- … and {len(history_findings) - 20} more")
        return 1 if args.new_only else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
