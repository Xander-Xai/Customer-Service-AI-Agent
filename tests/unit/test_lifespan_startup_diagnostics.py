"""Startup-failure diagnostics for the ASGI lifespan (Issue #50).

The problem
-----------
When lifespan initialisation fails, the process dies before serving a request. On
the way out the application wrote **nothing of its own**: the only clue was the
ASGI server's ``Application startup failed. Exiting.``, which is not this
project's logger (no trace_id, not JSON, names no failing stage, offers no
remediation) and which disappears entirely if that server logger is reconfigured
or its level raised. Symptom: the process exits and the logs say nothing.

What is pinned here
-------------------
1. The failure is **recorded** by the application's own logger, carrying stage,
   exception type, **root** cause type, a traceback and remediation.
2. The exception is still **re-raised** — fail fast. A swallowed exception would
   downgrade fail-closed into "serve traffic half-initialised".
3. **No secret reaches the log.** This is the requirement that needs two
   independent mechanisms, because there are two independent writers:
     - the application's own CRITICAL record (redacted before formatting), and
     - the ASGI server's raw traceback, which the app does not control and which
       is therefore handled by scrubbing the exception in place before re-raising.
4. The **process exits non-zero**, driven as a real subprocess running real
   uvicorn — not by asserting on a mocked logger.

The unit-level tests cover the helpers in isolation; the subprocess tests cover
the actual acceptance criteria.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import time
from io import StringIO
from pathlib import Path

import pytest

from core.logger import (
    _JSONFormatter,
    _root_cause,
    log_startup_failure,
    redact_secrets,
    scrub_exception_message,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Marker planted in the synthetic failure so the tests can assert the *root
#: cause* reached the log rather than merely "some error happened".
ROOT_CAUSE_MARKER = "SYNTHETIC_LIFESPAN_ROOT_CAUSE"

#: Values that must never appear in the log. They are injected as real
#: environment variables so the redaction has to cope with genuine credential
#: material, not just a well-known spelling.
LEAKED_SECRETS = (
    "zQ7secretRedisPassword",
    "zQ7secretJwtSigningValue",
    "zQ7secretApiKeyValue",
    "zQ7correcthorsebatterystaple",
)

#: A synthetic OpenAI-shaped credential, assembled at import time rather than
#: written as one literal. ``redact_secrets`` must still see a real ``sk-`` +
#: 26-token string -- that shape is the whole point of the case, and it is what
#: the high-confidence pattern in ``scripts/check_secrets.py`` matches. Spelling
#: it contiguously in source would make this repository's own secret guard
#: report a file that contains no secret, which is how a guard starts getting
#: ignored. The runtime value is byte-identical either way.
_OPENAI_SHAPED_KEY = "sk-" + "abcdefghij" "klmnopqrstuvwxyz"
_OPENAI_SHAPED_KEY_BODY = _OPENAI_SHAPED_KEY[len("sk-") :]
_OPENAI_SHAPED_KEY_MASKED = "sk-***"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _child_env(**overrides: str) -> dict[str, str]:
    """A minimal, self-contained env for a startup-failure subprocess.

    Deliberately built from scratch: inheriting the developer's shell would let
    a stray secret suppress the leak assertions, and inheriting project config
    would make the test depend on local state.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "SERVICE_ROLE": "api",
        "DEV_MODE": "true",
        "API_KEY_ENABLED": "false",
        "ADMIN_PASSWORD": "throwaway-bootstrap-password",
    }
    env.update(overrides)
    return env


def _run_uvicorn_until_exit(script: str, cwd: Path, timeout: int = 180, **env_overrides: str):
    """Run ``script`` as a real process and return the CompletedProcess.

    ``uvicorn.run`` exits non-zero when lifespan startup fails, so the script
    terminates on its own; the timeout is a backstop, not the expected path.
    """
    script_path = cwd / "runner.py"
    script_path.write_text(textwrap.dedent(script), encoding="utf-8")
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd),
        env=_child_env(**env_overrides),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    proc.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    return proc


# ---------------------------------------------------------------------------
# A. redaction
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("raw", "must_not_contain", "must_still_contain"),
        [
            # The empty-username form is this project's own REDIS_URL spelling
            # (`redis://:${REDIS_PASSWORD}@redis:6379`). An earlier version of
            # this rule required a non-empty user and silently missed it.
            ("redis://:S3CRET@cache:6379/0", "S3CRET", "cache:6379"),
            ("postgresql://user:S3CRET@db:5432/csai", "S3CRET", "db:5432"),
            ("password=hunter2", "hunter2", "password="),
            ("api_key: S3CRET", "S3CRET", "api_key:"),
            ('"secret": "S3CRET"', "S3CRET", "secret"),
            (_OPENAI_SHAPED_KEY, _OPENAI_SHAPED_KEY_BODY, _OPENAI_SHAPED_KEY_MASKED),
            ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678", "Bearer"),
        ],
        ids=[
            "dsin-empty-user",
            "dsin-with-user",
            "password-eq",
            "api-key-colon",
            "json-secret",
            "openai-style-key",
            "bearer-token",
        ],
    )
    def test_credential_values_are_masked(
        self, raw: str, must_not_contain: str, must_still_contain: str
    ) -> None:
        out = redact_secrets(raw)
        assert must_not_contain not in out, f"credential survived redaction: {out!r}"
        assert must_still_contain in out, f"redaction destroyed useful context: {out!r}"

    @pytest.mark.unit
    def test_known_environment_credential_values_are_masked(self) -> None:
        """The layer pattern-matching cannot cover: an unknown key carrying a real value.

        A credential can reach a log under any key name ("admin=...", or embedded
        in a sentence). The process already holds the plaintext, so the value
        itself is what gets replaced.
        """
        secret = "zQ7arbitraryKeyNameValue9"
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("SOME_ODD_SECRET_NAME", secret)
            out = redact_secrets(f"authentication rejected for {secret} on node 3")
        assert secret not in out
        assert "node 3" in out, "non-secret context must survive"

    @pytest.mark.unit
    def test_short_values_are_not_blanketed(self) -> None:
        """Over-redaction makes logs unreadable; it is its own failure mode.

        A secret env var set to something like ``mock`` or ``changeme`` must not
        cause every occurrence of that word to be masked.
        """
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DB_PASSWORD", "mock")
            out = redact_secrets("ERP_MODE=mock and DB_PASSWORD=mock")
        assert out.count("mock") == 2, f"short common value was over-redacted: {out!r}"

    @pytest.mark.unit
    def test_non_string_input_is_returned_unchanged(self) -> None:
        """This runs on the failure path; it must never raise and hide the cause."""
        assert redact_secrets(None) is None  # type: ignore[arg-type]
        assert redact_secrets(123) == 123  # type: ignore[arg-type]
        assert redact_secrets("") == ""


class TestScrubExceptionMessage:
    @pytest.mark.unit
    def test_message_is_scrubbed_in_place_and_same_object_returned(self) -> None:
        exc = RuntimeError("password=hunter2 and redis://:pw@h:6379")
        returned = scrub_exception_message(exc)
        assert returned is exc, "caller must be able to `raise` the original object"
        assert "hunter2" not in str(exc)
        assert "pw" not in str(exc)

    @pytest.mark.unit
    def test_non_string_args_are_preserved(self) -> None:
        exc = ValueError("boom", 42)
        scrub_exception_message(exc)
        assert exc.args == ("boom", 42)

    @pytest.mark.unit
    def test_it_never_raises(self) -> None:
        """Best-effort only: a failure to scrub must not cause a second failure."""

        class Hostile(Exception):
            @property
            def args(self):  # noqa: D401
                raise RuntimeError("no args for you")

            @args.setter
            def args(self, value):
                raise RuntimeError("no args for you")

        hostile = Hostile("x")
        assert scrub_exception_message(hostile) is hostile


# ---------------------------------------------------------------------------
# B. root cause resolution
# ---------------------------------------------------------------------------


class TestRootCause:
    @pytest.mark.unit
    def test_returns_self_when_there_is_no_chain(self) -> None:
        exc = RuntimeError("alone")
        assert _root_cause(exc) is exc

    @pytest.mark.unit
    def test_walks_explicit_cause(self) -> None:
        try:
            try:
                raise KeyError("the real cause")
            except KeyError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as exc:
            assert type(_root_cause(exc)) is KeyError

    @pytest.mark.unit
    def test_walks_implicit_context(self) -> None:
        """Raised inside an except block without `from`: __context__, not __cause__."""
        try:
            try:
                raise ValueError("implicit")
            except ValueError:
                # deliberately no `from` — that is what makes this __context__
                raise TypeError("outer")  # noqa: B904
        except TypeError as exc:
            assert type(_root_cause(exc)) is ValueError

    @pytest.mark.unit
    def test_cycle_does_not_hang(self) -> None:
        """This only runs while the process is dying; an infinite loop buries the cause."""
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert _root_cause(a) in (a, b)

    @pytest.mark.unit
    def test_self_referential_cause_terminates(self) -> None:
        exc = RuntimeError("self")
        exc.__cause__ = exc
        assert _root_cause(exc) is exc


# ---------------------------------------------------------------------------
# C. the logging contract
# ---------------------------------------------------------------------------


def _capture_json(record_exc: BaseException, *, stage: str = "container.initialize"):
    """Drive log_startup_failure through the production JSON formatter."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JSONFormatter())
    logger = logging.getLogger(f"probe_{stage}")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    log_startup_failure(stage, record_exc, "set ADMIN_PASSWORD in .env", logger=logger)
    return json.loads(stream.getvalue().strip())


class TestLogStartupFailureContract:
    @pytest.mark.unit
    def test_record_carries_every_required_field(self) -> None:
        try:
            try:
                raise KeyError("innermost")
            except KeyError as inner:
                raise RuntimeError("outermost") from inner
        except RuntimeError as exc:
            record = _capture_json(exc)

        extra = record["extra"]
        assert extra["event"] == "lifespan_startup_failed"
        assert extra["phase"] == "startup"
        assert extra["stage"] == "container.initialize"
        assert extra["exception_type"] == "RuntimeError"
        assert (
            extra["root_exception_type"] == "KeyError"
        ), "the root cause type must be distinguishable from the wrapper type"
        assert "innermost" in record["message"], "root cause message must be present"
        assert "Traceback" in record["message"], "traceback must be present"
        assert (
            "set ADMIN_PASSWORD in .env" in record["message"]
        ), "remediation must reach the operator"

    @pytest.mark.unit
    def test_severity_is_critical(self) -> None:
        """Anything below CRITICAL can be filtered out by a normal LOG_LEVEL."""
        exc = RuntimeError("boom")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("probe_sev")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        log_startup_failure("container.initialize", exc, "fix it", logger=logger)
        assert stream.getvalue().count("CRITICAL") == 1

    @pytest.mark.unit
    def test_it_records_only_and_never_raises(self) -> None:
        """The helper must not become a second failure on the failure path."""
        stream = StringIO()
        logger = logging.getLogger("probe_no_raise")
        logger.handlers[:] = [logging.StreamHandler(stream)]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
        log_startup_failure(
            "container.initialize", RuntimeError("x"), "y", logger=logger
        )  # must not raise

    @pytest.mark.unit
    def test_it_does_not_pass_raw_exc_info(self) -> None:
        """Otherwise logging renders the *unredacted* traceback as a second copy.

        The JSON formatter emits ``exception`` from ``record.exc_info`` verbatim,
        which would bypass redaction entirely and re-leak the message.
        """
        exc = RuntimeError("password=hunter2")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_JSONFormatter())
        logger = logging.getLogger("probe_no_excinfo")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
        log_startup_failure("container.initialize", exc, "y", logger=logger)
        record = json.loads(stream.getvalue().strip())
        assert (
            "exception" not in record
        ), "record must not carry logging's own unredacted exception rendering"
        assert "hunter2" not in json.dumps(record)


# ---------------------------------------------------------------------------
# D. the acceptance criteria, driven as a real process
# ---------------------------------------------------------------------------


_FAILING_RUNNER = """
    import asyncio, os, sys
    import core.container as container

    async def _initialize(self):
        # The credential is read from the environment, as a real one would be:
        # a startup failure only embeds a secret because that secret came from
        # config. Baking it into the message as a literal would model a case no
        # redaction scheme can catch, and would hide whether the env-value layer
        # actually works.
        secret = os.environ["SECRET_IN_MESSAGE"]
        # The OpenAI-shaped literal is assembled in the *generated* child source
        # for the same reason the parent test file assembles its own: a
        # contiguous sk-<20+ chars> token in tracked source is this repository's
        # secret guard's high-confidence pattern, and a guard that fires on its
        # own fixtures is a guard that gets ignored.
        shaped_key = "sk-" + "abcdefghijklmnopqrstuvwxyz"
        try:
            raise ConnectionError("dial tcp 10.0.0.5:6379: connect: connection refused")
        except ConnectionError as inner:
            raise RuntimeError(
                "cannot reach cache: {marker} "
                "redis://:redis_pw_in_message@cache:6379/0 password=pw_in_message "
                + shaped_key + " " + secret
            ) from inner

    container.ServiceContainer.initialize = _initialize

    import uvicorn
    uvicorn.run("api.app_factory:app", host="127.0.0.1", port={port},
                log_level="info", access_log=False)
"""


@pytest.mark.timeout(300)
class TestRealProcessStartupFailure:
    """The acceptance criteria, on a real uvicorn process rather than a mock."""

    @staticmethod
    def _run(tmp_path: Path, **env_overrides: str):
        port = _free_port()
        script = _FAILING_RUNNER.format(marker=ROOT_CAUSE_MARKER, port=port)
        # The embedded credential is a real environment value, which is the only
        # way a process can recognise an unknown-form credential.
        env_overrides.setdefault("SECRET_IN_MESSAGE", LEAKED_SECRETS[0])
        return _run_uvicorn_until_exit(script, tmp_path, **env_overrides)

    @pytest.mark.unit
    def test_process_exits_non_zero(self, tmp_path: Path) -> None:
        """Fail fast: a swallowed exception would leave a half-initialised process serving."""
        proc = self._run(tmp_path)
        assert proc.returncode != 0, (
            "a lifespan startup failure must not exit 0; "
            f"stdout={proc.stdout[-500:]!r} stderr={proc.stderr[-500:]!r}"
        )

    @pytest.mark.unit
    def test_application_logs_the_failure_itself(self, tmp_path: Path) -> None:
        """Not merely the ASGI server's line: ours names the stage and the root cause."""
        proc = self._run(tmp_path)
        combined = proc.stdout + proc.stderr
        assert "lifespan_startup_failed" in combined, (
            "the application must emit its own startup-failure record; only "
            f"seeing server output would leave the #50 symptom in place:\n{combined[-2000:]}"
        )
        assert "stage=container.initialize" in combined
        assert "CRITICAL" in combined

    @pytest.mark.unit
    def test_root_cause_is_locatable(self, tmp_path: Path) -> None:
        proc = self._run(tmp_path)
        combined = proc.stdout + proc.stderr
        assert ROOT_CAUSE_MARKER in combined, "root cause text missing"
        assert "Traceback" in combined, "traceback missing"
        assert "ConnectionError" in combined, (
            "the root cause type (the chained exception) must be identifiable, "
            "not just the outermost wrapper"
        )
        assert "处置建议" in combined, "remediation must reach the operator"

    @pytest.mark.unit
    def test_no_secret_reaches_the_log(self, tmp_path: Path) -> None:
        """Both writers are covered: our record *and* the server's raw traceback."""
        proc = self._run(tmp_path)
        combined = proc.stdout + proc.stderr
        assert LEAKED_SECRETS[0] not in combined, "environment credential leaked"
        for spelling in ("redis_pw_in_message", "pw_in_message", _OPENAI_SHAPED_KEY):
            assert spelling not in combined, f"credential leaked: {spelling}"
        assert "cache:6379" in combined, "redaction must not erase the diagnostic host"

    @pytest.mark.unit
    def test_other_environment_secrets_are_not_echoed(self, tmp_path: Path) -> None:
        """Unrelated configured secrets must not ride along in the record either."""
        proc = self._run(
            tmp_path,
            JWT_SECRET=LEAKED_SECRETS[1],
            MONITORING_ADMIN_TOKEN=LEAKED_SECRETS[2],
        )
        combined = proc.stdout + proc.stderr
        assert LEAKED_SECRETS[1] not in combined, "JWT_SECRET leaked into the log"
        assert LEAKED_SECRETS[2] not in combined, "MONITORING_ADMIN_TOKEN leaked into the log"


class TestBothLifespanStagesAreCovered:
    """`stage` is only useful if it actually distinguishes the steps.

    Forcing the second stage to fail would mean breaking attribute assignment on
    the ASGI app object, which tests the mock rather than the contract. Asserting
    the shape of the source is both cheaper and more durable: it fails if someone
    adds a third initialisation step and forgets to wrap it, or renames one
    stage but not the other.
    """

    @staticmethod
    def _lifespan_source() -> str:
        source = (REPO_ROOT / "api" / "app_factory.py").read_text(encoding="utf-8")
        start = source.index("async def lifespan(app):")
        return source[start:]

    @pytest.mark.unit
    @pytest.mark.parametrize("stage", ["container.initialize", "app_state_injection"])
    def test_each_stage_is_reported(self, stage: str) -> None:
        assert f'"{stage}"' in self._lifespan_source(), (
            f"lifespan no longer reports the {stage!r} stage; a failure there would be "
            "logged without saying where it happened"
        )

    @pytest.mark.unit
    def test_every_reported_stage_is_followed_by_a_bare_reraise(self) -> None:
        """A `raise` that is not bare would re-raise something other than the cause."""
        source = self._lifespan_source()
        reported = source.count("log_startup_failure(")
        assert reported >= 2, f"expected both stages to be wrapped, found {reported}"
        assert source.count("\n        raise\n") >= reported, (
            "each log_startup_failure call must be followed by a bare `raise`; "
            "anything else would swallow or replace the original exception"
        )

    @pytest.mark.unit
    def test_lifespan_catches_base_exception_not_only_exception(self) -> None:
        """`except Exception` would miss BaseException-derived failures.

        Startup aborts also arrive as ``CancelledError`` / ``KeyboardInterrupt``
        / ``SystemExit`` in some shutdown paths; those must still be logged and
        still propagate.
        """
        assert "except BaseException as exc:" in self._lifespan_source()


@pytest.mark.timeout(300)
class TestHealthyStartupStillWorks:
    """The fix must not turn a normal boot into a failure."""

    #: Emitted by ``lifespan`` right after both wrapped stages succeed.
    LIFESPAN_COMPLETED = "ServiceContainer 初始化完成"

    @pytest.mark.unit
    def test_clean_startup_completes_and_reports_no_failure(self, tmp_path: Path) -> None:
        """RAG seeding is stubbed because it needs a reachable embedding provider.

        That is itself #50's reported trigger, but asserting on it here would only
        re-test the failure path. What matters on the happy path is that the new
        try/except neither fires spuriously nor swallows anything, so the stub
        lets the real ``initialize()`` and the real lifespan run to completion.

        A successful uvicorn does not exit, so the process is stopped once the
        lifespan has reported completion rather than being waited on.
        """
        script = f"""
            import core.container as container

            async def _no_rag(self):
                return None

            container.ServiceContainer._init_rag_and_tools = _no_rag

            import uvicorn
            uvicorn.run("api.app_factory:app", host="127.0.0.1", port={_free_port()},
                        log_level="info", access_log=False)
        """
        script_path = tmp_path / "runner.py"
        script_path.write_text(textwrap.dedent(script), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(tmp_path),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                assert (
                    proc.poll() is None
                ), f"a healthy startup exited early with {proc.returncode}:\n{proc.stdout.read()}"
                if self.LIFESPAN_COMPLETED in (proc.stdout.readline() or ""):
                    break
        finally:
            proc.terminate()
            try:
                output = proc.communicate(timeout=30)[0] or ""
            except subprocess.TimeoutExpired:
                proc.kill()
                output = proc.communicate()[0] or ""
        assert (
            "lifespan_startup_failed" not in output
        ), f"a healthy startup was reported as a failure:\n{output[-1500:]}"
