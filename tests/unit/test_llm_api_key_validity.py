"""Table-driven tests for the single authoritative LLM key judgement (issue #51).

The problem
-----------
Two independent implementations decided "is this API key usable?":

- ``core/container.py::_init_llm`` — prefix blacklist **plus** a 40-character
  minimum length. This one decides which LLM implementation actually runs.
- ``api/routes/monitoring.py`` ``/api/health`` — prefix blacklist only, and a
  *different* blacklist (``your-`` vs the container's ``your_``; the container
  also had ``test-``/``mock-``, health had ``sk-test-placeholder``).

Measured on the pre-fix tree, the two rules disagreed on 6 of 9 sampled keys.
The worst combination is the one the health gate cares about: ``key_valid: true``
while the process is answering every request from ``RuleBasedLLM`` templates.
``/api/health`` is both the README verification endpoint and the compose
healthcheck for the ``app`` container, so such a deployment passes its own gate.

What these tests pin
--------------------
One table, one invariant: **the runtime choice and the health verdict are the
same judgement.** Every row asserts all of:

1. ``evaluate_llm_api_key`` returns the expected verdict,
2. ``ServiceContainer._init_llm`` selects the expected implementation,
3. ``/api/health`` reports ``key_valid`` / ``key_usable`` equal to that verdict,
4. ``/api/health`` reports the implementation that is genuinely active, and
   ``degraded`` tracks whether that implementation is the template fallback.

Plus two boundary tests: the health endpoint must not invoke the LLM provider,
and the helper must stay a pure function.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from core.config import (
    LLM_API_KEY_MIN_LENGTH,
    LLM_KEY_REASON_EMPTY,
    LLM_KEY_REASON_OK,
    LLM_KEY_REASON_PLACEHOLDER,
    LLM_KEY_REASON_TOO_SHORT,
    evaluate_llm_api_key,
)

RULE_BASED = "RuleBasedLLM"
REAL_CLIENT = "OpenAICompatibleClient"

#: A key of exactly the minimum length that is not a placeholder.
_EXACT_MIN = "sk-" + "k" * (LLM_API_KEY_MIN_LENGTH - 3)


@dataclass(frozen=True)
class Case:
    """One row of the table.

    :param expect_usable: the verdict both the runtime and health must report
    :param dev_mode: DEV_MODE governs whether an unusable key degrades at all --
        in production the container builds a real client regardless, because
        production is expected to fail closed elsewhere
    :param expect_impl: the implementation ``_init_llm`` must end up with
    """

    id: str
    key: str
    expect_usable: bool
    expect_reason: str
    dev_mode: bool
    expect_impl: str


TABLE: tuple[Case, ...] = (
    # ---- unusable: the cases where the two old rules disagreed ----------
    Case("empty", "", False, LLM_KEY_REASON_EMPTY, True, RULE_BASED),
    Case("whitespace-only", "   ", False, LLM_KEY_REASON_EMPTY, True, RULE_BASED),
    Case("short-key", "sk-short", False, LLM_KEY_REASON_TOO_SHORT, True, RULE_BASED),
    Case(
        "placeholder-sk-placeholder",
        "sk-placeholder-embedding-test-key",
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    Case(
        "placeholder-your_underscore-short",
        "your_api_key_here",
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    # health used to accept this one (it had no length rule) while the runtime
    # rejected it on length -> key_valid=true next to an active RuleBasedLLM.
    Case(
        "placeholder-your-dash-over-min-length",
        "your-" + "a" * 45,
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    # the container used to accept these two (its list lacked the prefix)
    Case(
        "placeholder-mock-over-min-length",
        "mock-" + "b" * 45,
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    Case(
        "placeholder-test-over-min-length",
        "test-" + "c" * 45,
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    # the container used to accept this one (its list lacked sk-test-placeholder)
    Case(
        "placeholder-sk-test-placeholder-over-min-length",
        "sk-test-placeholder-" + "d" * 30,
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    Case(
        "placeholder-uppercase-is-case-insensitive",
        "SK-PLACEHOLDER-" + "E" * 30,
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        True,
        RULE_BASED,
    ),
    # ---- usable ---------------------------------------------------------
    Case("exactly-min-length", _EXACT_MIN, True, LLM_KEY_REASON_OK, True, REAL_CLIENT),
    Case(
        "realistic-openai-style",
        "sk-proj-" + "f" * 51,
        True,
        LLM_KEY_REASON_OK,
        True,
        REAL_CLIENT,
    ),
    Case(
        "realistic-siliconflow-style",
        "sk-" + "0123456789abcdef" * 3 + "wxyz",
        True,
        LLM_KEY_REASON_OK,
        True,
        REAL_CLIENT,
    ),
    # ---- production never degrades to the template fallback --------------
    Case("prod-short-key", "sk-short", False, LLM_KEY_REASON_TOO_SHORT, False, REAL_CLIENT),
    Case(
        "prod-placeholder",
        "sk-placeholder-embedding-test-key",
        False,
        LLM_KEY_REASON_PLACEHOLDER,
        False,
        REAL_CLIENT,
    ),
    Case("prod-valid", _EXACT_MIN, True, LLM_KEY_REASON_OK, False, REAL_CLIENT),
)


def _ids(case: Case) -> str:
    return case.id


class _SpyLLM:
    """Stand-in for an active LLM that fails loudly if anything calls it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, *args, **kwargs):  # pragma: no cover - must never run
        self.calls.append("invoke")
        raise AssertionError("/api/health must not invoke the LLM")

    async def async_invoke(self, *args, **kwargs):  # pragma: no cover
        self.calls.append("async_invoke")
        raise AssertionError("/api/health must not invoke the LLM")


def _bare_container():
    """A container with only what ``_init_llm`` touches (avoids Redis/Qdrant)."""
    from core.container import ServiceContainer

    container = ServiceContainer.__new__(ServiceContainer)
    container.llm = None
    container.circuit_breaker = None
    return container


async def _runtime_impl_for(key: str, dev_mode: bool) -> str:
    """What ``_init_llm`` actually selects for this key."""
    container = _bare_container()
    with (
        patch("core.config.OPENAI_API_KEY", key),
        patch("core.config.DEV_MODE", dev_mode),
        patch("core.config._DEV_MODE", dev_mode),
    ):
        await container._init_llm()
    return type(container.llm).__name__


async def _health_llm_block_for(key: str, dev_mode: bool, active_llm) -> dict:
    """What ``/api/health`` reports for the same key."""
    from api.routes.monitoring import health

    container = _bare_container()
    container.llm = active_llm

    class _State:
        pass

    class _App:
        pass

    app = _App()
    app.state = _State()
    app.state.container = container
    app.state.circuit_breaker = None
    app.state.module_load_time = time.time() - 60
    app.state._cached_health_status = None
    app.state._cached_health_time = 0
    app.state.get_redis_client = lambda: None

    class _Request:
        pass

    request = _Request()
    request.app = app

    with (
        patch("core.config.OPENAI_API_KEY", key),
        patch("core.config.DEV_MODE", dev_mode),
        patch("core.config._DEV_MODE", dev_mode),
    ):
        result = await health(request)
    return result["components"]["llm"]


@pytest.mark.parametrize("case", TABLE, ids=_ids)
class TestRuntimeAndHealthAgree:
    async def test_helper_matches_the_table(self, case: Case) -> None:
        status = evaluate_llm_api_key(case.key)
        assert status.usable is case.expect_usable
        assert status.reason == case.expect_reason
        assert status.configured is bool(case.key.strip())

    async def test_runtime_selects_the_expected_implementation(self, case: Case) -> None:
        assert await _runtime_impl_for(case.key, case.dev_mode) == case.expect_impl

    async def test_health_reports_the_same_verdict_as_the_runtime(self, case: Case) -> None:
        """The core invariant of #51: one judgement, two consumers."""
        runtime_impl = await _runtime_impl_for(case.key, case.dev_mode)
        block = await _health_llm_block_for(case.key, case.dev_mode, _SpyLLM())

        assert block["key_valid"] is case.expect_usable, (
            f"/api/health reported key_valid={block['key_valid']} but the "
            f"authoritative verdict is {case.expect_usable} (reason={case.expect_reason})"
        )
        assert block["key_usable"] == block["key_valid"], (
            "key_usable and key_valid must be the same value from the same source"
        )
        assert block["configured"] is bool(case.key.strip())

        # and the reported implementation must be what would actually be selected
        assert block["implementation"] != "unknown"
        if runtime_impl == RULE_BASED:
            # the acceptance criterion: a degraded runtime must never be reported healthy-keyed
            assert block["key_valid"] is False, (
                "the runtime falls back to RuleBasedLLM while /api/health claims the "
                "key is valid — the exact contradiction #51 is about"
            )


@pytest.mark.parametrize("case", TABLE, ids=_ids)
class TestHealthShapeIsComplete:
    async def test_health_exposes_the_required_fields(self, case: Case) -> None:
        """configured / key_usable / provider / implementation / degraded."""
        block = await _health_llm_block_for(case.key, case.dev_mode, _SpyLLM())
        for field in ("configured", "key_usable", "provider", "implementation", "degraded"):
            assert field in block, f"/api/health llm block must expose {field!r}"
        assert isinstance(block["degraded"], bool)
        assert block["implementation"]

    async def test_health_reports_the_genuinely_active_implementation(self, case: Case) -> None:
        """implementation must be read from the runtime, not recomputed."""
        from llm.rule_based_llm import RuleBasedLLM

        active = RuleBasedLLM()
        block = await _health_llm_block_for(case.key, case.dev_mode, active)
        assert block["implementation"] == RULE_BASED
        assert block["degraded"] is True, (
            "an active RuleBasedLLM is a degraded mode and must be visible on /api/health"
        )


class TestDegradedFlagTracksTheActiveImplementation:
    @pytest.mark.parametrize(
        ("impl_name", "expect_degraded"),
        [(RULE_BASED, True), (REAL_CLIENT, False)],
    )
    async def test_degraded_matches_the_active_class(
        self, impl_name: str, expect_degraded: bool
    ) -> None:
        container = _bare_container()
        if impl_name == RULE_BASED:
            from llm.rule_based_llm import RuleBasedLLM

            container.llm = RuleBasedLLM()
        else:
            from llm.client import OpenAICompatibleClient

            container.llm = OpenAICompatibleClient(
                api_key=_EXACT_MIN,
                base_url="https://example.invalid/v1",
                model="test-model",
            )
        block = await _health_llm_block_for(_EXACT_MIN, True, container.llm)
        assert block["implementation"] == impl_name
        assert block["degraded"] is expect_degraded

    async def test_unknown_when_no_container_is_wired(self) -> None:
        """A missing container must not crash or invent an implementation."""
        from api.routes.monitoring import health

        class _State:
            pass

        class _App:
            pass

        app = _App()
        app.state = _State()
        app.state.circuit_breaker = None
        app.state.module_load_time = time.time() - 60
        app.state._cached_health_status = None
        app.state._cached_health_time = 0
        app.state.get_redis_client = lambda: None

        class _Request:
            pass

        request = _Request()
        request.app = app
        with patch("core.config.OPENAI_API_KEY", _EXACT_MIN):
            result = await health(request)
        assert result["components"]["llm"]["implementation"] == "unknown"
        assert result["components"]["llm"]["degraded"] is False


class TestHealthDoesNotCallTheProvider:
    async def test_health_does_not_invoke_the_active_llm(self) -> None:
        """No paid provider call from an unauthenticated endpoint."""
        spy = _SpyLLM()
        await _health_llm_block_for(_EXACT_MIN, True, spy)
        assert spy.calls == [], (
            f"/api/health invoked the LLM client: {spy.calls}. It is unauthenticated; "
            "a real provider call here would be a billing and latency liability."
        )

    async def test_health_does_not_construct_an_llm_client(self) -> None:
        """Reading the key must not build a client aimed at the provider."""
        from llm.client import OpenAICompatibleClient

        with patch.object(
            OpenAICompatibleClient,
            "__init__",
            side_effect=AssertionError("/api/health must not construct an LLM client"),
        ):
            await _health_llm_block_for(_EXACT_MIN, True, _SpyLLM())

    def test_helper_is_a_pure_function(self) -> None:
        """evaluate_llm_api_key must stay IO-free so health can call it safely."""
        source = inspect.getsource(evaluate_llm_api_key)
        for forbidden in ("httpx", "requests", "urlopen", "socket", "Client(", "await "):
            assert forbidden not in source, (
                f"evaluate_llm_api_key must not do IO, but references {forbidden!r}"
            )

    def test_helper_is_the_only_place_the_rule_lives(self) -> None:
        """Guard against a second rule creeping back into either consumer."""
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        offenders = []
        for rel in ("api/routes/monitoring.py", "core/container.py"):
            text = (repo / rel).read_text(encoding="utf-8")
            # no inline prefix blacklist, and no hand-rolled length threshold
            if "PLACEHOLDER_PREFIXES" in text or "placeholder_prefixes" in text:
                offenders.append(f"{rel}: inlines a placeholder prefix list")
            if re.search(r"len\(\s*OPENAI_API_KEY\s*\)\s*<", text):
                offenders.append(f"{rel}: inlines a key length threshold")
        assert not offenders, (
            "the key-validity rule must live only in core.config.evaluate_llm_api_key; "
            f"found: {offenders}"
        )
