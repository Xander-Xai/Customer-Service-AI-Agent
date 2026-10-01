"""Regression tests for merged-PR review debt (post-merge truth convergence).

One focused test per reviewer finding that was LIVE on current ``main``:

- RAG evaluator: all_queries denominator, rerank uplift semantics, unconfigured
  reranker blocker, requested-experiment preflight scope.
- Provider adapter: streaming ``choices[0]`` traversal, malformed JSON on 200.
- Provider runner: warmup authentication failure, worst-case cost/attempt cap.
- Evidence rendering: JSON/Markdown status parity.
- project_facts: env-independent fallback facts + field-specific doc checks.
- Security: quoted generic API credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_script("evaluate_rag", "scripts/evaluate_rag.py")
facts = _load_script("project_facts", "scripts/project_facts.py")


# ---------------------------------------------------------------- RAG evaluator


class _FakeKB:
    def __init__(self, behavior: dict[str, str] | None = None):
        self._hybrid_enabled = True
        self._embed_fn = object()
        self.behavior = behavior or {}

    async def retrieve(self, request):
        spec = self.behavior.get(request.query, "normal")
        if spec == "raise":
            raise RuntimeError("provider exploded")
        if spec == "timeout":
            raise TimeoutError("timeout")
        from rag.retrieval_contract import RetrievalResult

        # Successful queries retrieve a gold document so the only failures in
        # the denominator test are the scripted exception/timeout.
        return RetrievalResult(
            [{"id": "doc_0", "content": "c"}],
            meta={"retrieval_degraded": False},
            trace=None,
        )


def _queries(n: int) -> list[dict[str, Any]]:
    return [
        {
            "query_id": f"bench_{i:04d}",
            "query": f"查询 {i}",
            "expected_doc_ids": ["doc_0", "doc_1"],
            "category": "c",
            "difficulty": "easy",
        }
        for i in range(n)
    ]


def test_all_queries_denominator_includes_failed_requests() -> None:
    queries = _queries(4)
    kb = _FakeKB({queries[0]["query"]: "raise", queries[1]["query"]: "timeout"})
    res = asyncio.run(
        ev.run_experiment(
            kb,
            "hybrid_no_rerank",
            ev.Override(),
            queries,
            corpus_ids={"doc_0", "doc_1"},
            top_k=8,
            ks=(1, 3),
            timeout_s=30.0,
            warmup_n=0,
        )
    )
    assert res["n_success"] == 2
    assert res["n_failed"] == 2
    pm = res["population_metrics"]
    # Primary end-to-end view must count every query, not only survivors.
    assert pm["all_queries"]["query_count"] == res["n_total"] == 4
    # Failed requests contribute zero-valued metrics: 2 hits / 4 queries = 0.5,
    # NOT the survivors-only 1.0.
    assert pm["all_queries"]["metrics"]["hit@1"] == pytest.approx(0.5)
    assert res["metrics"]["hit@1"] == pytest.approx(1.0)  # n_success diagnostic view
    # Latency never fabricates samples for unfinished requests.
    assert pm["all_queries"]["latency"]["n"] == 2


def _row(rank: float) -> dict[str, Any]:
    return {"first_relevant_rank": rank, "metrics": {}}


def _uplift(base_ranks: dict[str, float], target_ranks: dict[str, float]) -> dict[str, Any]:
    results = {
        "hybrid_no_rerank": {"rows": [_row(r) | {"query_id": q} for q, r in base_ranks.items()],
                             "metrics": {}, "latency": {"p95_ms": 0.0}},
        "hybrid_rerank": {"rows": [_row(r) | {"query_id": q} for q, r in target_ranks.items()],
                          "metrics": {}, "latency": {"p95_ms": 0.0}},
    }
    return ev.ablation_analysis(results)["reranker_uplift"]


def test_rerank_uplift_treats_miss_as_worse_than_any_hit() -> None:
    # miss -> hit must be IMPROVED; hit -> miss DEGRADED.
    up = _uplift({"q": 0.0}, {"q": 2.0})
    assert up["improved_queries"] == 1 and up["degraded_queries"] == 0
    down = _uplift({"q": 2.0}, {"q": 0.0})
    assert down["degraded_queries"] == 1 and down["improved_queries"] == 0


def test_rerank_uplift_rank_direction() -> None:
    better = _uplift({"q": 5.0}, {"q": 2.0})
    assert better["improved_queries"] == 1
    worse = _uplift({"q": 2.0}, {"q": 5.0})
    assert worse["degraded_queries"] == 1
    same = _uplift({"q": 0.0}, {"q": 0.0})
    assert same["unchanged_queries"] == 1


def test_unconfigured_reranker_blocks_only_hybrid_rerank() -> None:
    out = ev.derive_blockers(
        embedding_configured=True,
        embedding_probe="ok",
        embedding_http_status=None,
        reranker_configured=False,
        reranker_probe=None,
        reranker_http_status=None,
        qdrant_total_points=5000,
        requested_experiments=("hybrid_rerank", "vector_only"),
    )
    codes = {b["code"] for b in out["blockers"]}
    assert "RERANKER_PROVIDER_UNAVAILABLE" in codes
    rer = next(b for b in out["blockers"] if b["code"] == "RERANKER_PROVIDER_UNAVAILABLE")
    assert rer["blocks_experiments"] == ["hybrid_rerank"]
    assert out["status"] == "PARTIAL"


def test_requested_experiments_scope_blockers() -> None:
    # bm25_only does not need the embedding provider: an embedding outage must
    # not BLOCK a BM25-only run.
    out = ev.derive_blockers(
        embedding_configured=True,
        embedding_probe="failed",
        embedding_http_status=401,
        reranker_configured=True,
        reranker_probe="ok",
        reranker_http_status=None,
        qdrant_total_points=5000,
        requested_experiments=("bm25_only",),
    )
    assert out["status"] != "BLOCKED"
    embedding = next(b for b in out["blockers"] if b["stage"] == "embedding")
    assert embedding["blocking"] is False
    assert "bm25_only" not in embedding["blocks_experiments"]


# ---------------------------------------------------------------- provider adapter


def test_nested_traverses_list_indexes() -> None:
    from evaluation.provider_adapter import _nested

    event = {"choices": [{"delta": {"content": "hi"}}]}
    assert _nested(event, "choices", 0, "delta", "content") == "hi"
    assert _nested(event, "choices", 5, "delta") is None


class _SSEResponse:
    status_code = 200
    headers = {"x-request-id": "req-1"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield 'data: {"id":"x","choices":[{"delta":{"content":"OK"}}]}'
        yield "data: [DONE]"


class _SSEClient:
    def __init__(self, *a, **k):
        self._response = _SSEResponse()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def stream(self, *a, **k):
        return self._response


def test_streaming_reads_content_from_choices_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.provider_adapter import ProviderAdapter

    monkeypatch.setattr("evaluation.provider_adapter.httpx.Client", lambda **k: _SSEClient())
    result = ProviderAdapter(
        api_key="test-key", base_url="https://x/v1", model="m", timeout=1, max_attempts=1
    ).stream_chat([{"role": "user", "content": "hi"}], max_tokens=8, provider="p")
    assert result.final_status == "SUCCESS"
    assert result.response_nonempty is True
    assert result.ttft_ms is not None


class _MalformedJSONResponse:
    status_code = 200
    headers = {"x-request-id": "req-2"}

    def json(self):
        raise ValueError("Expecting value: line 1 column 1")


class _MalformedClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def post(self, *a, **k):
        return _MalformedJSONResponse()


def test_malformed_json_on_200_is_structured_not_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.provider_adapter import probe_chat_completion

    monkeypatch.setattr("evaluation.provider_adapter.httpx.Client", lambda **k: _MalformedClient())
    result = probe_chat_completion(api_key="test-key", base_url="https://x/v1", model="m")
    assert result.status_code == 200
    assert result.category == "INVALID_PROVIDER_RESPONSE"
    assert result.response_nonempty is False


# ---------------------------------------------------------------- provider runner


def _failing_adapter() -> type:
    from evaluation.evidence_schema import EvidenceSource, Measurement
    from evaluation.provider_adapter import NormalizedProviderUsage, ProviderCallResult

    missing = EvidenceSource.NOT_AVAILABLE
    usage = NormalizedProviderUsage(
        provider="t", model="m", request_id=None,
        input_tokens=Measurement.unavailable(missing),
        output_tokens=Measurement.unavailable(missing),
        cached_tokens=Measurement.unavailable(missing),
        reasoning_tokens=Measurement.unavailable(missing),
        provider_cost=Measurement.unavailable(missing),
    )

    class _Adapter:
        def __init__(self, **_k):
            pass

        def stream_chat(self, *_a, **_k):
            return ProviderCallResult(
                usage=usage, ttft_ms=None, e2e_ms=1.0, network_ms=1.0,
                attempt_count=1, retry_count=0, timeout_count=0,
                final_status="FAILURE", response_nonempty=False,
                error_code="HTTP_401", error_category="AUTH_FAILED", http_status=401,
            )

    return _Adapter


def test_warmup_authentication_failure_is_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.provider_runner import run_provider_staging

    monkeypatch.setenv("EVAL_REAL_PROVIDER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    monkeypatch.setenv("EVAL_PROVIDER_MAX_ATTEMPTS", "1")
    monkeypatch.setattr("evaluation.provider_runner.ProviderAdapter", _failing_adapter())
    result = run_provider_staging(
        repeat=1, warmup=1, max_requests=20,
        estimated_cost_cap=1.0,
        estimated_input_cost_per_1k=0.0015,
        estimated_output_cost_per_1k=0.006,
    )
    assert result.final_status == "BLOCKED_BY_AUTHENTICATION"
    assert result.metadata["authentication_circuit_breaker"] is True
    assert result.failure_count >= 1


def test_cost_cap_counts_warmup_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.provider_runner import run_provider_staging

    monkeypatch.setenv("EVAL_REAL_PROVIDER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    monkeypatch.setenv("EVAL_PROVIDER_MAX_ATTEMPTS", "2")
    # A cap that only covers the measured repeat must be rejected once warmup
    # and worst-case retries are accounted.
    with pytest.raises(RuntimeError, match="estimated cost cap"):
        run_provider_staging(
            repeat=1, warmup=1, max_requests=60,
            estimated_cost_cap=0.02,
            estimated_input_cost_per_1k=0.5,
            estimated_output_cost_per_1k=0.5,
        )


# ---------------------------------------------------------------- evidence render


def test_render_markdown_status_and_provider_boundary_from_payload() -> None:
    from evaluation.runner import render_markdown

    base = {
        "run_id": "r", "git_sha": "s", "workload_id": "w",
        "request_count": 1, "success_count": 1,
        "latency": {"source": "FIXTURE", "p50_ms": 1, "p95_ms": 2, "p99_ms": 3},
    }
    local = render_markdown({**base, "environment": "LOCAL_FIXTURE"})
    assert "- status: `LOCAL_ONLY`" in local
    staging = render_markdown({
        **base,
        "environment": "CONTROLLED_STAGING",
        "final_status": "PASS",
        "provider_cost": {"source": "PROVIDER_REPORTED"},
    })
    assert "- status: `PASS`" in staging
    assert "Provider-reported" in staging
    unverified = render_markdown({
        **base, "environment": "NOT_VERIFIED", "final_status": None,
    })
    assert "- status: `NOT_VERIFIED`" in unverified


# ---------------------------------------------------------------- project facts


def test_fallback_facts_ignore_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = facts._llm_facts()
    monkeypatch.setenv("OPENAI_MODEL", "locally/overridden-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
    assert facts._llm_facts() == baseline


def test_field_specific_check_rejects_stale_agent_count(tmp_path: Path) -> None:
    real = PROJECT_ROOT / "docs" / "reference" / "current-state.md"
    text = real.read_text(encoding="utf-8")
    assert "1024" in text  # embedding dim present elsewhere in the doc
    stale = text.replace(
        "Agent roles (`core/container.py::_init_agents`): **9**",
        "Agent roles (`core/container.py::_init_agents`): **10**",
    )
    doc = tmp_path / "current-state.md"
    doc.write_text(stale, encoding="utf-8")
    assert facts.check_doc(doc) == 1


# ---------------------------------------------------------------- secret guard


def test_generic_quoted_api_key_is_detected() -> None:
    from scripts.check_secrets import scan_text

    for sample in (
        "api_key=abcdefghijklmnopqrstuv",
        'api_key="abcdefghijklmnopqrstuv"',
        "api_key='abcdefghijklmnopqrstuv'",
        'API_KEY = "abcdefghijklmnopqrstuv"',
    ):
        categories = {f.category for f in scan_text("x.env", sample)}
        assert "generic-api-key" in categories, sample


# ---------------------------------------------------------------- misc debt


def test_tracking_only_url_query_is_removed() -> None:
    from core.tool_result_compressors import _clean_url

    assert _clean_url("https://x.test/?utm_source=a") == "https://x.test/"
    assert _clean_url("https://x.test/p?utm_source=a&keep=1") == "https://x.test/p?keep=1"


def test_erp_not_found_string_is_not_cacheable() -> None:
    from core.tool_result_cache import ToolCachePolicy, cacheable_result

    policy = ToolCachePolicy(enabled=True, ttl_seconds=30)
    assert cacheable_result("未找到库存信息", policy) is False
    assert cacheable_result("未找到订单信息", policy) is False


def test_private_erp_tools_require_authorization() -> None:
    from tools.erp_tools import create_erp_tools

    class _Adapter:
        pass

    registry = create_erp_tools(_Adapter())
    assert registry.cache_policy_for("query_order").authorization_required is True
    assert registry.cache_policy_for("query_customer").authorization_required is True
    # Public resources keep caching.
    assert registry.cache_policy_for("query_product").authorization_required is False


class _FakePoint:
    def __init__(self, payload):
        self.payload = payload


class _FakeScrollClient:
    def __init__(self, payloads):
        self._payloads = payloads

    def scroll(self, **kwargs):
        return ([_FakePoint(p) for p in self._payloads], None)


class _FakeImportKB:
    def __init__(self, payloads, count):
        self._client = _FakeScrollClient(payloads)
        self._count = count
        self.added: list[str] = []

    def get_collection_count(self, collection):
        return self._count

    def add_documents(self, collection, documents, metadatas, ids):
        self.added.extend(ids)


def test_import_corpus_reimports_foreign_collection_with_matching_count() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "import_eval_corpus", PROJECT_ROOT / "scripts" / "import_eval_corpus.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["import_eval_corpus"] = mod
    spec.loader.exec_module(mod)

    docs = [
        {"id": "d1", "content": "c1", "scene": ["售前咨询"]},
        {"id": "d2", "content": "c2", "scene": ["售前咨询"]},
    ]
    # Same point COUNT as expected, but foreign identities/payload hash.
    foreign = _FakeImportKB([{"doc_id": "old1"}, {"doc_id": "old2"}], count=2)
    counts, identity = asyncio.run(mod.import_corpus(foreign, docs, 64, "newhash"))
    assert identity["product_knowledge"]["action"] == "imported"
    assert identity["product_knowledge"]["missing_before"] == 2
    assert set(foreign.added) == {"d1", "d2"}


def test_import_corpus_skips_only_on_verified_identity() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "import_eval_corpus2", PROJECT_ROOT / "scripts" / "import_eval_corpus.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["import_eval_corpus2"] = mod
    spec.loader.exec_module(mod)

    docs = [{"id": "d1", "content": "c1", "scene": ["售前咨询"]}]
    matching = _FakeImportKB([{"doc_id": "d1", "eval_corpus_hash": "hash1"}], count=1)
    _, identity = asyncio.run(mod.import_corpus(matching, docs, 64, "hash1"))
    assert identity["product_knowledge"]["action"] == "skipped"
    assert matching.added == []


def test_import_eval_corpus_uses_configured_qdrant_endpoint() -> None:
    source = (PROJECT_ROOT / "scripts" / "import_eval_corpus.py").read_text(encoding="utf-8")
    assert "QDRANT_HOST" in source and "QDRANT_PORT" in source
    assert "QdrantKnowledgeBase(host=\"localhost\", port=6333)" not in source


def test_migration_dry_run_matches_execute_deletion_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    import rag.point_id_migration as mig

    stable_b = mig.document_id_to_point_id("c", "docB")

    def _rec(point_id, doc_id):
        r = MagicMock()
        r.id = point_id
        r.payload = {"doc_id": doc_id}
        r.vector = [0.1]
        return r

    # docA's legacy point sits at docB's stable id; docB is present at that id.
    records = [_rec(stable_b, "docA"), _rec(stable_b, "docB")]
    client = MagicMock()
    client.scroll.return_value = (records, None)
    report = mig.rebuild_collection_point_ids(client, "c", dry_run=True)
    # Execution protects docB's stable id; the dry-run preview must too.
    assert stable_b not in report.legacy_point_ids


def test_pre_commit_hook_pattern_catches_quoted_key(tmp_path: Path) -> None:
    config = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = re.search(r'"(sk-[^"]+)"', config)
    assert match, "pre-commit credential pattern not found"
    pattern = match.group(1)
    sample = tmp_path / "sample.env"
    sample.write_text('api_key="abcdefghijklmnopqrstuv"\n', encoding="utf-8")
    result = subprocess.run(
        ["grep", "-Eiq", pattern, str(sample)], capture_output=True
    )
    assert result.returncode == 0
