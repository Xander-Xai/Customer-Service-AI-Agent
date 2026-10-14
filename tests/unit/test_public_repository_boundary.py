"""Tests for scripts/check_public_repository_boundary.py.

The guard's value is entirely in its *calibration*: a rule that flags every
occurrence of ``resume`` would be worthless in CI, because this repository uses
that word to mean ``Command(resume=...)``. So every rule below carries a
**positive** case (material that must be caught) and a **negative** case (real
engineering text that must not be), and the last two tests assert the invariant
on the actual checkout.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_public_repository_boundary.py"

spec = importlib.util.spec_from_file_location("check_public_repository_boundary", SCRIPT)
boundary = importlib.util.module_from_spec(spec)
sys.modules["check_public_repository_boundary"] = boundary
spec.loader.exec_module(boundary)


def findings(rel: str, text: str) -> set[str]:
    return {f.rule_id for f in boundary.scan_text(rel, rel, text)}


def line_numbers(rel: str, text: str, rule_id: str) -> list[int]:
    out = []
    for f in boundary.scan_text(rel, rel, text):
        if f.rule_id == rule_id:
            out.append(int(f.location.rsplit(":", 1)[1]))
    return out


# ------------------------------------------------------------------ A: career


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("README.md", "面试准备材料不在本仓库内。"),
        ("docs/x.md", "应聘与自荐流程说明"),
        ("docs/x.md", "年终奖与晋升机制"),
        ("docs/x.md", "内推与背调流程"),
    ],
)
def test_a2_catches_chinese_career_terms(rel: str, text: str) -> None:
    assert "A2-career-term-zh" in findings(rel, text)


@pytest.mark.parametrize(
    "text",
    [
        "职责分离：reviewer_id 必须不等于 user_id。",
        "审批人长期不可达时把 run 收口为 DEAD_LETTER。",
        "该 agent 的职责包括路由与工具调用。",
        # A governance line that *says* the material was removed is not the material.
        "本仓库简历部分已删除。",
    ],
)
def test_a2_ignores_engineering_sense_of_chinese_career_words(text: str) -> None:
    assert "A2-career-term-zh" not in findings("docs/a.md", text)


@pytest.mark.parametrize(
    "text",
    [
        "Resume claims for the distributed runtime.",
        "Interview guide: architecture, failures, trade-offs.",
        "Here is my 30-second pitch for the runtime.",
        "Tell me about yourself — focus on the P0 incident.",
        "Why should we hire you? Focus on delivery.",
        "One STAR story about the stranded RUNNING defect.",
    ],
)
def test_a3_catches_interview_phrasing(text: str) -> None:
    assert "A3-career-term-en" in findings("docs/a.md", text)


@pytest.mark.parametrize(
    "text",
    [
        "简历材料不在本仓库内，已删除。",
        "Interview guide has been removed; do not reintroduce it.",
        "We forbid re-adding interview prep material.",
        "The pitch was reframed to production-oriented framing.",
    ],
)
def test_a3_negation_marker_allows_naming_the_thing(text: str) -> None:
    """Naming a forbidden artifact inside a governance rule is not the same as
    shipping the artifact. Without this exemption the guard could not even
    describe its own rules."""
    assert "A3-career-term-en" not in findings("docs/a.md", text)


def test_a4_catches_employment_narrative() -> None:
    assert "A4-career-narrative" in findings("docs/a.md", "This is the post-departure rebuild.")
    assert "A4-career-narrative" in findings("docs/a.md", "I worked at the previous company.")


def test_a5_catches_rehearsed_pitch_cards() -> None:
    assert "A5-pitch-cards" in findings("docs/a.md", "### 8.1 六张母卡")
    assert "A5-pitch-cards" in findings("docs/a.md", "| Question | Current aligned answer |")
    text = "master cards are no longer maintained here."
    assert "A5-pitch-cards" not in findings("docs/a.md", text)


def test_a1_flags_career_filenames_but_not_engineering_paths() -> None:
    assert "A1-career-filename" in findings("docs/finalization/INTERVIEW_GUIDE.md", "# title")
    assert "A1-career-filename" in findings("docs/RESUME_CLAIMS.md", "# title")
    # Same substrings inside engineering paths must not trip the rule.
    for rel in (
        "tests/integration/checkpoint_resume_provider.py",
        "core/hitl/approval_service.py",
        "artifacts/runtime-diagnostics/resumed-run.json",
        "web/src/__e2e__/flows.spec.js",
    ):
        assert "A1-career-filename" not in findings(rel, "# anything")


# --------------------------------------------------- B: internal identifiers


def test_b1_catches_internal_codename_everywhere() -> None:
    rel = "core/config.py"
    assert "B1-internal-codename" in findings(
        rel, 'APP_NAME = "药妆智多星 - Customer Service AI Agent"'
    )


def test_b2_catches_formal_material_disclosure() -> None:
    text = "综合评分: 99.0/100（不作为正规材料参考依据）"
    assert "B2-formal-material-disclosure" in findings("docs/a.md", text)


def test_b3_catches_employer_team_attribution() -> None:
    text = "*药妆智多星开发团队*"
    assert "B3-employer-attribution" in findings("docs/a.md", text)


# ----------------------------------------------------------- C: self-scoring


@pytest.mark.parametrize(
    "text",
    [
        "**状态**: 极致级生产就绪（超越99.9%的生产系统）",
        "**强烈推荐立即上线**",
        "当前评分已经可以放心上线！",
        "**综合评分**: **99.0/100**",
        "| 近乎完美 | ✅ |",
    ],
)
def test_c1_catches_self_awarded_score(text: str) -> None:
    assert "C1-self-awarded-score" in findings("docs/a.md", text)


def test_c1_allows_notice_that_the_hype_was_removed() -> None:
    text = "README「极致级生产就绪/超越99.9%」：改为 production-oriented 框架 + NOT_MEASURED"
    assert "C1-self-awarded-score" not in findings("docs/a.md", text)


# ----------------------------------------------------- D: outbound business KPI


@pytest.mark.parametrize(
    "text",
    [
        "首触解决率 FCR 78% 达成",
        "人效 3 倍提升",
        "LLM 调用降低 65%",
        "Token 成本下降 35%",
    ],
)
def test_d1_catches_reproduced_outbound_kpi_numbers(text: str) -> None:
    assert "D1-outbound-business-kpi" in findings("docs/a.md", text)


def test_d1_allows_the_governance_rule_without_the_figure() -> None:
    text = "| FCR（旧对外口径） | UNVERIFIED | 暂不写 | issue-level FCR、resolution |"
    assert "D1-outbound-business-kpi" not in findings("docs/a.md", text)
    text2 = "LLM 调用量下降（旧对外口径） | UNVERIFIED | 暂不写 |"
    assert "D1-outbound-business-kpi" not in findings("docs/a.md", text2)


# ------------------------------------------------------ E: sample-data PII


@pytest.mark.parametrize(
    "text",
    [
        '{"content": "联系 13812345678 客服"}',
        '{"content": "身份证 110101199003071234"}',
        '{"content": "邮箱 zhangsan@example-shop.cn"}',
        '{"content": "卡号 6222021234567890123"}',
    ],
)
def test_e1_catches_pii_in_seed_corpus(text: str) -> None:
    assert "E1-seed-pii" in findings("data/seed/faq.json", text)


def test_e1_is_scoped_to_the_shipped_corpus() -> None:
    """The same shapes are legitimate inside runtime code (ports, ids)."""
    assert "E1-seed-pii" not in findings("core/config.py", "QDRANT_PORT = 6333")
    assert "E1-seed-pii" not in findings("tests/unit/test_x.py", "PHONE = '13812345678'")


def test_e1_allows_placeholder_and_field_shapes() -> None:
    ok = [
        '{"content": "联系企业专线客服400-XXX-XXXX获取详细报价"}',
        '{"content": "下单时在备注栏填写公司名称、税号、开户行及账号"}',
        '{"content": "批次号格式为字母+数字组合（如AL20260315）"}',
        '{"content": "输入防伪码后6位数字"}',
    ]
    for text in ok:
        assert "E1-seed-pii" not in findings("data/seed/faq.json", text)


def test_e2_catches_live_endpoint_in_seed_corpus() -> None:
    assert "E2-seed-endpoint" in findings("data/seed/faq.json", '{"u": "https://api.vendor.cn/v1"}')
    assert "E2-seed-endpoint" not in findings(
        "data/seed/faq.json", '{"u": "https://example.com/placeholder"}'
    )


# ------------------------------------------------------- F: required surface


def test_required_surface_flags_missing_provenance_doc(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    found = boundary.check_required_surface(tmp_path)
    assert [f.rule_id for f in found] == ["F1-provenance-doc-present"]


def test_required_surface_flags_missing_readme_link(tmp_path: Path) -> None:
    (tmp_path / "DATA_PROVENANCE.md").write_text("# p\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    found = boundary.check_required_surface(tmp_path)
    assert [f.rule_id for f in found] == ["F2-readme-links-provenance"]


def test_required_surface_passes_when_both_present(tmp_path: Path) -> None:
    (tmp_path / "DATA_PROVENANCE.md").write_text("# p\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("see [D](DATA_PROVENANCE.md)\n", encoding="utf-8")
    assert boundary.check_required_surface(tmp_path) == []


# ------------------------------------- false-positive resistance: the point


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        # LangGraph checkpoint resume — the single most likely false positive.
        ("core/hitl/gate.py", "graph.invoke(Command(resume=payload), config)"),
        ("core/hitl/approval_service.py", '"""WHERE resumed_at IS NULL"""  # 只被消费一次'),
        ("api/routes/approvals.py", '"resume_dispatched": dispatched,'),
        ("runtime/bootstrap.py", "async def invoke_graph_with_resume(run_id: str) -> None:"),
        ("db/models.py", "resumed_at = Column(DateTime(timezone=True), nullable=True)"),
        ("core/monitoring.py", '"LangGraph executions resumed from an existing checkpoint",'),
        # "candidate" in retrieval / telemetry / index senses.
        ("cache/response_cache.py", "candidate_keys: set = set()"),
        ("core/checkpoint_retention.py", "class OrphanCandidate:"),
        ("core/tool_result_compressors.py", 'candidate = value[:mid].rstrip() + "…"'),
        ("Makefile", "agent-eval-cases: ## 重新生成候选数据集（全部标为 llm_candidate）"),
        # "offer" inside the Apache-2.0 licence text.
        ("LICENSE", "   use, offer to sell, sell, import, export, distribute"),
        # "offer" as a noun in a system prompt.
        ("agents/sales_agent.py", "我方提供的优惠方案如下："),
        # "production" / "生产" in the ordinary engineering sense.
        ("docs/deployment.md", "生产环境部署变量"),
        ("core/config.py", 'ENVIRONMENT = os.getenv("ENVIRONMENT", "production")'),
        # Production-adjacent words that are NOT career material.
        ("docs/production-readiness.md", "PRODUCTION NOT_VERIFIED"),
        # Separator of duties is a security control.
        ("core/hitl/approval_service.py", "职责分离：reviewer_id 不得等于 user_id"),
    ],
)
def test_benign_engineering_text_is_not_flagged(rel: str, text: str) -> None:
    assert findings(rel, text) == set(), f"false positive on {rel}: {text!r}"


def test_guard_never_returns_matched_content() -> None:
    """A boundary finding must not become a leak channel for the bytes it caught."""
    secret = "SECRET-LITERAL-MARKER-42"
    found = boundary.scan_text("docs/a.md", "docs/a.md", f"简历 {secret}")
    assert found
    rendered = "\n".join(f"{f.location} {f.rule_id} {f.category}" for f in found)
    assert secret not in rendered


# ------------------------------------------------------- real-checkout invariants


def test_rule_ids_are_unique() -> None:
    ids = [r.rule_id for r in boundary.RULES]
    assert len(ids) == len(set(ids))


def test_every_rule_has_a_description() -> None:
    for rule in boundary.RULES:
        assert rule.description.strip(), rule.rule_id


def test_real_checkout_passes_boundary_check() -> None:
    """The invariant: on this repository, at HEAD of the cleanup, the guard is green."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_seed_corpus_is_pii_free() -> None:
    """Independent of the guard: re-scan the shipped corpus with the PII rules."""
    pii_rule = next(r for r in boundary.RULES if r.rule_id == "E1-seed-pii")
    endpoint_rule = next(r for r in boundary.RULES if r.rule_id == "E2-seed-endpoint")
    for corpus in sorted((REPO_ROOT / "data" / "seed").glob("*.json")):
        rel = corpus.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(corpus.read_text(encoding="utf-8").splitlines(), 1):
            for rule in (pii_rule, endpoint_rule):
                if rule.pattern.search(line) and not boundary._anchor_saves(line, rule):
                    pytest.fail(f"{rel}:{number} trips {rule.rule_id}")


# ------------------------------------------------------- history batch parser


def _batch_stream(frames: list[tuple[str, str, bytes]]) -> bytes:
    """Render exactly what ``git cat-file --batch`` writes: an ASCII header line
    (``<sha> <type> <size>``), the raw payload, then one newline."""
    out = b""
    for sha, kind, payload in frames:
        kind_text = kind.decode() if isinstance(kind, bytes) else kind
        out += f"{sha} {kind_text} {len(payload)}\n".encode("ascii") + payload + b"\n"
    return out


def test_iter_batch_blobs_skips_non_blob_objects() -> None:
    """Regression: comparing the type token against ``"blob"`` (str) instead of
    ``b"blob"`` makes the whole history scan a silent no-op that reports clean."""
    stream = _batch_stream(
        [
            ("a" * 40, b"tag", b"tag payload"),
            ("b" * 40, b"tree", b"tree payload"),
            ("c" * 40, b"blob", b"# resume claims\n"),
            ("d" * 40, b"blob", b"binary"),
        ]
    )
    entries = [[sha, f"path{i}"] for i, sha in enumerate(["a" * 40, "b" * 40, "c" * 40, "d" * 40])]
    got = list(boundary.iter_batch_blobs(stream, entries))
    assert [sha for sha, _, _ in got] == ["c" * 40, "d" * 40]
    assert got[0][1] == "path2"
    assert got[0][2] == b"# resume claims\n"


def test_iter_batch_blobs_handles_multibyte_payloads() -> None:
    """Offset arithmetic must be byte-exact, not character-exact."""
    payload = "简历\n".encode()
    stream = _batch_stream([("a" * 40, b"blob", payload)])
    got = list(boundary.iter_batch_blobs(stream, [["a" * 40, "docs/x.md"]]))
    assert len(got) == 1
    assert boundary._decode(got[0][2]) == "简历\n"
