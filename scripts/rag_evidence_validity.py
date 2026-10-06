#!/usr/bin/env python3
"""Evidence validity predicate for the formal RAG 649 evaluation (issue #45).

Why this module exists
----------------------
``VERIFIED`` is a claim about **the evidence**, not about the process. A run
that returns 649/649 responses can still be worthless as evidence:

- every request answered on a fallback path (retrieval channel dead for the
  whole population), so the "vector" leg never measured vector retrieval;
- 625/649 gold documents absent from the indexed corpus, so the numbers mostly
  measure a failed ``make rag-eval-import``;
- a required channel unavailable for a leg, so the 4-config ablation silently
  degenerated into fewer configurations.

The historical producer derived ``status=VERIFIED_FULL`` from
``subset_run == false and n_success == N and reranker_ok`` — none of which can
see any of the above, because a degraded fallback answers *successfully*.

Design constraints
------------------
1. **Never infer validity from metric magnitude.** A legitimately poor
   retriever produces low Hit@K; that is a valid measurement. The predicate
   therefore reads only *execution* facts (which channels ran, on how many
   queries, producing how many candidates), *corpus* facts (which gold
   documents are actually indexed) and *integrity* facts (subset run, request
   errors, canonical leg completeness). ``metrics`` is not an input.
2. **Fail closed.** A malformed observation block is INVALID, never VALID.
3. **Two independent implementations, one predicate.** The producer calls
   ``assess_evidence_validity`` and is forbidden from writing VERIFIED_FULL
   unless the verdict is VALID; the consumer
   (``scripts/rag_evidence_status.py``) requires the recorded block AND
   recomputes it from the recorded observations, rejecting any disagreement.
   A hand-edited or legacy artifact therefore cannot certify itself.

The observations block is intentionally flat, JSON-serialisable and free of
application imports, so an auditor can recompute the verdict by hand from
``report.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from eval_contract import (  # noqa: E402
    CHANNELS,
    EXPERIMENT_NAMES,
    FORMAL_PREFLIGHT_STATUS,
    MAX_ACCEPTABLE_DEGRADED_RATIO,
    MAX_GOLD_NOT_INDEXED_RATIO,
    MIN_FULL_GOLD_COVERED_RATIO,
    MIN_RETRIEVAL_ELIGIBLE_RATIO,
    VALIDITY_CONTRACT_VERSION,
    VERDICT_INVALID,
    VERDICT_VALID,
    required_channels,
)

# Gate groups — kept coarse on purpose: they answer "which kind of question was
# the run unable to answer?", which is what a reader needs from a rejected run.
GROUP_EXECUTION_INTEGRITY = "execution_integrity"
GROUP_CHANNEL_AVAILABILITY = "channel_availability"
GROUP_CORPUS_COVERAGE = "corpus_coverage"

# Stable reason codes. These are API: artifacts, audits and tests match on them.
E01_SUBSET_RUN = "E01_SUBSET_RUN"
E02_QUERY_COUNT_INVALID = "E02_QUERY_COUNT_INVALID"
E03_CANONICAL_LEG_MISSING = "E03_CANONICAL_LEG_MISSING"
E04_REQUEST_ERRORS = "E04_REQUEST_ERRORS"
E05_EXECUTION_INCOMPLETE = "E05_EXECUTION_INCOMPLETE"
C01_FULLY_DEGRADED = "C01_FULLY_DEGRADED"
C02_DEGRADED_RATIO_ABOVE_LIMIT = "C02_DEGRADED_RATIO_ABOVE_LIMIT"
C03_REQUIRED_CHANNEL_NEVER_USED = "C03_REQUIRED_CHANNEL_NEVER_USED"
C04_REQUIRED_CHANNEL_EMPTY = "C04_REQUIRED_CHANNEL_EMPTY"
C05_PREFLIGHT_NOT_CLEAN = "C05_PREFLIGHT_NOT_CLEAN"
G01_GOLD_NOT_INDEXED_ABOVE_LIMIT = "G01_GOLD_NOT_INDEXED_ABOVE_LIMIT"
G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR = "G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR"
G03_FULL_GOLD_COVERED_BELOW_FLOOR = "G03_FULL_GOLD_COVERED_BELOW_FLOOR"
S00_OBSERVATIONS_MALFORMED = "S00_OBSERVATIONS_MALFORMED"

FAILURE_LABEL_GOLD_NOT_INDEXED = "GOLD_NOT_INDEXED"


def validity_thresholds() -> dict[str, Any]:
    """The contract thresholds, echoed into every artifact for auditability."""
    return {
        "max_acceptable_degraded_ratio": MAX_ACCEPTABLE_DEGRADED_RATIO,
        "max_gold_not_indexed_ratio": MAX_GOLD_NOT_INDEXED_RATIO,
        "min_retrieval_eligible_ratio": MIN_RETRIEVAL_ELIGIBLE_RATIO,
        "min_full_gold_covered_ratio": MIN_FULL_GOLD_COVERED_RATIO,
        "required_preflight_status": FORMAL_PREFLIGHT_STATUS,
    }


def _reason(code: str, group: str, scope: str, detail: str) -> dict[str, str]:
    return {"code": code, "group": group, "scope": scope, "detail": detail}


def _is_count(value: Any) -> bool:
    """Strict non-negative integer count (bools are not counts)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count_or_zero(value: Any) -> int:
    """The value when it is a valid count, else 0 (missing counts never divide)."""
    return int(value) if _is_count(value) else 0


def _ratio(numerator: Any, denominator: int) -> float | None:
    if not _is_count(numerator) or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def assess_evidence_validity(observations: dict[str, Any]) -> dict[str, Any]:
    """Assess whether a formal run's evidence is valid enough to certify.

    Returns the artifact block::

        {
            "contract": "rag-evidence-validity/v1",
            "verdict": "VALID" | "INVALID",
            "reasons": [ {code, group, scope, detail}, ... ],
            "thresholds": {...},
            "observed": {...},
        }

    ``verdict == VALID`` iff ``reasons`` is empty. The returned ``observed``
    is the exact input that was assessed, so the consumer can recompute the
    verdict from the artifact without trusting a self-declared boolean.
    """
    if not isinstance(observations, dict):
        return {
            "contract": VALIDITY_CONTRACT_VERSION,
            "verdict": VERDICT_INVALID,
            "reasons": [
                _reason(
                    S00_OBSERVATIONS_MALFORMED,
                    GROUP_EXECUTION_INTEGRITY,
                    "run",
                    "evidence_validity.observed must be an object",
                )
            ],
            "thresholds": validity_thresholds(),
            "observed": observations,
        }

    reasons: list[dict[str, str]] = []
    observed = observations

    experiments = observed.get("experiments")
    populations = observed.get("population_counts")
    subset_run = observed.get("subset_run")
    declared = observed.get("declared_queries")
    executed = observed.get("executed_queries")
    preflight_status = observed.get("preflight_status")

    malformed = False
    if not isinstance(experiments, dict):
        malformed = True
        experiments = {}
    if not isinstance(populations, dict):
        malformed = True
        populations = {}
    if malformed:
        reasons.append(
            _reason(
                S00_OBSERVATIONS_MALFORMED,
                GROUP_EXECUTION_INTEGRITY,
                "run",
                "experiments must be an object and population_counts must be an object",
            )
        )

    # ---- execution integrity ------------------------------------------------
    if subset_run is not False:
        reasons.append(
            _reason(
                E01_SUBSET_RUN,
                GROUP_EXECUTION_INTEGRITY,
                "run",
                f"subset_run={subset_run!r}: a partial query set is a diagnostic, "
                "not a formal evaluation",
            )
        )
    declared_n = _count_or_zero(declared)
    executed_n = _count_or_zero(executed)
    if declared_n <= 0 or executed_n <= 0 or declared_n != executed_n:
        reasons.append(
            _reason(
                E02_QUERY_COUNT_INVALID,
                GROUP_EXECUTION_INTEGRITY,
                "run",
                f"declared_queries={declared!r} executed_queries={executed!r}: "
                "the formal run must execute every declared benchmark query",
            )
        )

    missing_legs = [name for name in EXPERIMENT_NAMES if name not in experiments]
    if missing_legs:
        reasons.append(
            _reason(
                E03_CANONICAL_LEG_MISSING,
                GROUP_EXECUTION_INTEGRITY,
                "run",
                f"missing canonical ablation legs: {','.join(missing_legs)}",
            )
        )

    if preflight_status != FORMAL_PREFLIGHT_STATUS:
        reasons.append(
            _reason(
                C05_PREFLIGHT_NOT_CLEAN,
                GROUP_CHANNEL_AVAILABILITY,
                "run",
                f"preflight status={preflight_status!r} (required "
                f"{FORMAL_PREFLIGHT_STATUS!r}): a blocked/partial preflight means "
                "at least one channel was unavailable for the whole run",
            )
        )

    # ---- per-leg execution integrity + channel availability -----------------
    for name in sorted(experiments):
        leg = experiments[name]
        if not isinstance(leg, dict):
            reasons.append(
                _reason(
                    S00_OBSERVATIONS_MALFORMED,
                    GROUP_EXECUTION_INTEGRITY,
                    name,
                    f"leg observations must be an object, got {type(leg).__name__}",
                )
            )
            continue

        n_total = leg.get("n_total")
        n_success = leg.get("n_success")
        n_error = leg.get("n_error")
        n_degraded = leg.get("n_degraded")

        for field_name, value in (
            ("n_total", n_total),
            ("n_success", n_success),
            ("n_error", n_error),
            ("n_degraded", n_degraded),
        ):
            if not _is_count(value):
                reasons.append(
                    _reason(
                        S00_OBSERVATIONS_MALFORMED,
                        GROUP_EXECUTION_INTEGRITY,
                        f"{name}.{field_name}",
                        f"{field_name} must be a non-negative integer, got {value!r}",
                    )
                )

        # Normalised copies: a malformed count has already produced S00 above,
        # and a zero denominator must never silently look like a healthy ratio.
        total = _count_or_zero(n_total)
        errors = _count_or_zero(n_error)
        degraded = _count_or_zero(n_degraded)

        if errors > 0:
            reasons.append(
                _reason(
                    E04_REQUEST_ERRORS,
                    GROUP_EXECUTION_INTEGRITY,
                    name,
                    f"{errors}/{total} requests raised (exception/timeout); a leg "
                    "with request errors cannot certify formal metrics",
                )
            )
        if _is_count(n_total) and _is_count(n_success) and n_success != n_total:
            reasons.append(
                _reason(
                    E05_EXECUTION_INCOMPLETE,
                    GROUP_EXECUTION_INTEGRITY,
                    name,
                    f"n_success={n_success} of n_total={n_total}: partial execution "
                    "is not a formal full run",
                )
            )

        degraded_ratio = _ratio(degraded, total)
        if degraded_ratio is not None and total > 0 and degraded_ratio >= 1.0:
            reasons.append(
                _reason(
                    C01_FULLY_DEGRADED,
                    GROUP_CHANNEL_AVAILABILITY,
                    name,
                    f"{degraded}/{total} queries ran degraded: the retrieval "
                    "channel was dead for the entire population, so this leg did "
                    "not measure the configuration it claims to measure",
                )
            )
        elif degraded_ratio is not None and degraded_ratio > MAX_ACCEPTABLE_DEGRADED_RATIO:
            reasons.append(
                _reason(
                    C02_DEGRADED_RATIO_ABOVE_LIMIT,
                    GROUP_CHANNEL_AVAILABILITY,
                    name,
                    f"degraded ratio {degraded_ratio:.3f} exceeds the contract "
                    f"limit {MAX_ACCEPTABLE_DEGRADED_RATIO}; the metric averages "
                    "two different retrieval regimes",
                )
            )

        channels = leg.get("channels")
        if not isinstance(channels, dict):
            reasons.append(
                _reason(
                    S00_OBSERVATIONS_MALFORMED,
                    GROUP_CHANNEL_AVAILABILITY,
                    f"{name}.channels",
                    "channels must be an object",
                )
            )
            channels = {}

        required = required_channels(name) if name in EXPERIMENT_NAMES else ()
        for channel in CHANNELS:
            if channel not in required:
                continue
            facts = channels.get(channel)
            if not isinstance(facts, dict):
                reasons.append(
                    _reason(
                        C03_REQUIRED_CHANNEL_NEVER_USED,
                        GROUP_CHANNEL_AVAILABILITY,
                        f"{name}.{channel}",
                        f"required channel {channel!r} has no recorded usage facts",
                    )
                )
                continue
            used_count = facts.get("used_count")
            candidate_total = facts.get("candidate_total")
            if not _is_count(used_count) or used_count == 0:
                reasons.append(
                    _reason(
                        C03_REQUIRED_CHANNEL_NEVER_USED,
                        GROUP_CHANNEL_AVAILABILITY,
                        f"{name}.{channel}",
                        f"required channel {channel!r} was used on 0 queries: the leg "
                        "answers a question about a channel that never ran",
                    )
                )
            if not _is_count(candidate_total) or candidate_total == 0:
                reasons.append(
                    _reason(
                        C04_REQUIRED_CHANNEL_EMPTY,
                        GROUP_CHANNEL_AVAILABILITY,
                        f"{name}.{channel}",
                        f"required channel {channel!r} produced 0 candidates across "
                        "the whole run: the configuration retrieved nothing",
                    )
                )

        failure_counts = leg.get("failure_counts")
        gold_missing = (
            failure_counts.get(FAILURE_LABEL_GOLD_NOT_INDEXED)
            if isinstance(failure_counts, dict)
            else None
        )
        gold_missing_ratio = _ratio(gold_missing, total)
        if gold_missing_ratio is not None and gold_missing_ratio > MAX_GOLD_NOT_INDEXED_RATIO:
            reasons.append(
                _reason(
                    G01_GOLD_NOT_INDEXED_ABOVE_LIMIT,
                    GROUP_CORPUS_COVERAGE,
                    name,
                    f"{gold_missing}/{total} queries have no indexed gold "
                    f"document ({gold_missing_ratio:.3f} > "
                    f"{MAX_GOLD_NOT_INDEXED_RATIO}): the run measures corpus "
                    "import, not retrieval",
                )
            )

    # ---- corpus coverage (run level) ---------------------------------------
    all_queries = populations.get("all_queries")
    if _count_or_zero(all_queries) <= 0:
        reasons.append(
            _reason(
                S00_OBSERVATIONS_MALFORMED,
                GROUP_CORPUS_COVERAGE,
                "population_counts.all_queries",
                f"all_queries must be a positive integer, got {all_queries!r}",
            )
        )
    else:
        total_queries = _count_or_zero(all_queries)
        eligible_ratio = _ratio(populations.get("retrieval_eligible"), total_queries)
        covered_ratio = _ratio(populations.get("full_gold_covered"), total_queries)
        if eligible_ratio is None or eligible_ratio < MIN_RETRIEVAL_ELIGIBLE_RATIO:
            reasons.append(
                _reason(
                    G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR,
                    GROUP_CORPUS_COVERAGE,
                    "population_counts",
                    f"retrieval_eligible ratio {eligible_ratio!r} is below the contract "
                    f"floor {MIN_RETRIEVAL_ELIGIBLE_RATIO}",
                )
            )
        if covered_ratio is None or covered_ratio < MIN_FULL_GOLD_COVERED_RATIO:
            reasons.append(
                _reason(
                    G03_FULL_GOLD_COVERED_BELOW_FLOOR,
                    GROUP_CORPUS_COVERAGE,
                    "population_counts",
                    f"full_gold_covered ratio {covered_ratio!r} is below the contract "
                    f"floor {MIN_FULL_GOLD_COVERED_RATIO}",
                )
            )

    return {
        "contract": VALIDITY_CONTRACT_VERSION,
        "verdict": VERDICT_VALID if not reasons else VERDICT_INVALID,
        "reasons": reasons,
        "thresholds": validity_thresholds(),
        "observed": observed,
    }


def reason_codes(validity: dict[str, Any]) -> list[str]:
    """Stable reason-code list, for compact assertions and log lines."""
    reasons = validity.get("reasons") if isinstance(validity, dict) else None
    if not isinstance(reasons, list):
        return []
    return [str(r.get("code")) for r in reasons if isinstance(r, dict) and r.get("code")]


def observations_are_consistent(observed: Any, report: dict[str, Any], executed: int) -> bool:
    """Cross-check recorded observations against the artifact's own facts.

    The consumer cannot re-run the evaluation, so it must at least prove the
    observations were not edited to fit the verdict: every count the predicate
    relies on is also present, independently, in ``run_summary`` /
    ``evaluation_populations`` / ``benchmark`` / ``subset_run``. Any
    disagreement means the artifact is internally inconsistent, which is
    rejected exactly like an invalid verdict.
    """
    if not isinstance(observed, dict) or not isinstance(report, dict):
        return False
    if observed.get("subset_run") is not report.get("subset_run"):
        return False
    if observed.get("declared_queries") != executed:
        return False
    if observed.get("executed_queries") != executed:
        return False

    preflight = report.get("preflight")
    if not isinstance(preflight, dict):
        return False
    if observed.get("preflight_status") != preflight.get("status"):
        return False

    populations = report.get("evaluation_populations")
    if not isinstance(populations, dict):
        return False
    counts = populations.get("counts")
    if not isinstance(counts, dict) or observed.get("population_counts") != counts:
        return False

    run_summary = report.get("run_summary")
    if not isinstance(run_summary, dict):
        return False
    experiments = observed.get("experiments")
    if not isinstance(experiments, dict) or set(experiments) != set(run_summary):
        return False
    for name, leg in experiments.items():
        if not isinstance(leg, dict):
            return False
        summary = run_summary.get(name)
        if not isinstance(summary, dict):
            return False
        for field_name in ("n_total", "n_success", "n_error", "n_degraded"):
            if leg.get(field_name) != summary.get(field_name):
                return False
        if leg.get("failure_counts") != summary.get("failure_counts"):
            return False
        # The recorded requirement must equal the contract-derived one, so an
        # artifact cannot present a weakened channel requirement as its own.
        channels = leg.get("channels")
        if not isinstance(channels, dict) or set(channels) != set(CHANNELS):
            return False
        expected_required = set(required_channels(name)) if name in EXPERIMENT_NAMES else set()
        for channel in CHANNELS:
            facts = channels.get(channel)
            if not isinstance(facts, dict):
                return False
            if bool(facts.get("required")) is not (channel in expected_required):
                return False
    return True


__all__ = [
    "C01_FULLY_DEGRADED",
    "C02_DEGRADED_RATIO_ABOVE_LIMIT",
    "C03_REQUIRED_CHANNEL_NEVER_USED",
    "C04_REQUIRED_CHANNEL_EMPTY",
    "C05_PREFLIGHT_NOT_CLEAN",
    "CHANNELS",
    "E01_SUBSET_RUN",
    "E02_QUERY_COUNT_INVALID",
    "E03_CANONICAL_LEG_MISSING",
    "E04_REQUEST_ERRORS",
    "E05_EXECUTION_INCOMPLETE",
    "G01_GOLD_NOT_INDEXED_ABOVE_LIMIT",
    "G02_RETRIEVAL_ELIGIBLE_BELOW_FLOOR",
    "G03_FULL_GOLD_COVERED_BELOW_FLOOR",
    "S00_OBSERVATIONS_MALFORMED",
    "assess_evidence_validity",
    "observations_are_consistent",
    "reason_codes",
    "required_channels",
    "validity_thresholds",
]
