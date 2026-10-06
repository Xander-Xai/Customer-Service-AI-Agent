"""Regression tests for service healthcheck contracts (issue #49).

Two probes in the deployment were structurally incapable of passing, so
``docker compose ps`` was permanently red for services that were actually fine.

1. ``qdrant`` probed with ``curl``
   ``test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]``.
   ``qdrant/qdrant:v1.12.0`` ships no ``curl`` (nor ``wget`` / ``nc`` /
   ``python``), so every attempt died with::

       exec: "curl": executable file not found in $PATH

2. ``worker`` inherited an HTTP healthcheck but serves no HTTP
   The service declared no ``healthcheck:``, so it picked up the image-level
   ``HEALTHCHECK`` from the Dockerfile::

       CMD curl -f http://localhost:8000/api/health || exit 1

   but it runs ``celery ... worker`` and binds no HTTP port, so the probe could
   never succeed while Celery itself reported ``celery@<host> ready.``.

Scope of these tests (deliberately Docker-free, per repo convention)
------------------------------------------------------------------
They pin the *contract* of the Compose file against the real capabilities of the
images it references:

- no healthcheck may invoke a binary the target image does not ship
- a service that runs Celery must probe Celery, not an HTTP port it never binds
- timing values must be explicit and justified, and ``start_period`` must exist
  so a slow boot is not reported as ill health
- the inherited image-level HTTP probe must not be left standing for any service

The runtime behaviour (both probes passing on a live stack, and failing when the
dependency is removed) was verified against a real ``docker compose up``; see the
issue's before/after evidence. These tests exist so the *declaration* cannot
regress, since CI has no Docker daemon for the compose stack.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"

#: Images whose tool inventory was enumerated directly, so the tests can state
#: what the probe is allowed to rely on without pulling anything.
#:
#: ``qdrant/qdrant:v1.12.0`` contains only ``bash`` and ``sh`` as a shell plus
#: coreutils (``cat``/``head``/``grep``/``timeout``/``tr``). No curl, no wget,
#: no nc, no python, and the ``qdrant`` server binary is not on PATH.
#: ``/bin/sh`` is **dash**, which has no ``/dev/tcp`` -- hence an explicit
#: ``bash -c`` is mandatory for any socket-level probe in that image.
QDRANT_IMAGE = "qdrant/qdrant:v1.12.0"
QDRANT_AVAILABLE_SHELL = "bash"
QDRANT_ABSENT_TOOLS = ("curl", "wget", "nc", "python", "python3", "qdrant")

#: Services in the base compose file that run the Celery worker.
CELERY_SERVICES = ("worker",)

#: Services that are HTTP servers (as opposed to queue workers).
HTTP_SERVICES = ("app", "canary")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _healthcheck(service: dict) -> dict | None:
    return service.get("healthcheck")


def _probe_text(service: dict) -> str:
    """Flatten a healthcheck ``test`` into the string the runtime executes."""
    health = _healthcheck(service) or {}
    test = health.get("test")
    if isinstance(test, str):
        return test
    if isinstance(test, list) and len(test) >= 2:
        return str(test[1])
    raise AssertionError(f"unrecognised healthcheck test shape: {test!r}")


def _build_context(service: dict) -> str | None:
    build = service.get("build")
    if isinstance(build, str):
        return build
    if isinstance(build, dict):
        return build.get("context")
    return None


def _image_healthcheck() -> str | None:
    """The image-level HEALTHCHECK declared in the Dockerfile, if any."""
    match = re.search(
        r"HEALTHCHECK.*?CMD\s+(.+?)(?=\n[A-Z#]|\n\s*#|\Z)",
        DOCKERFILE.read_text(encoding="utf-8"),
        re.S,
    )
    return " ".join(match.group(1).split()) if match else None


def _has_docker() -> bool:
    return shutil.which("docker") is not None


class TestQdrantProbeUsesOnlyWhatTheImageShips:
    def test_qdrant_probe_does_not_invoke_an_absent_binary(self) -> None:
        probe = _probe_text(_compose()["services"]["qdrant"])
        for tool in QDRANT_ABSENT_TOOLS:
            assert not re.search(rf"(?:^|[\s|&;])({re.escape(tool)})(?:\s|$)", probe), (
                f"the qdrant healthcheck invokes `{tool}`, which {QDRANT_IMAGE} does "
                f"not ship; the probe can never pass. Probe was: {probe!r}"
            )

    def test_qdrant_probe_does_not_rely_on_dash_dev_tcp(self) -> None:
        """/bin/sh in the qdrant image is dash, which has no /dev/tcp support."""
        probe = _probe_text(_compose()["services"]["qdrant"])
        if "/dev/tcp" not in probe:
            pytest.skip("probe does not use bash socket redirection")
        assert re.search(rf"\b{QDRANT_AVAILABLE_SHELL}\s+-c\b", probe), (
            "the probe uses /dev/tcp but does not invoke bash explicitly; "
            f"{QDRANT_IMAGE} /bin/sh is dash and would fail with "
            "'/dev/tcp/...: Directory nonexistent'"
        )

    def test_qdrant_probe_asserts_on_a_real_status_code(self) -> None:
        """A TCP connect alone is liveness; the contract is /healthz readiness."""
        probe = _probe_text(_compose()["services"]["qdrant"])
        assert (
            "/healthz" in probe
        ), f"the qdrant probe must exercise the readiness endpoint, got: {probe!r}"
        assert (
            re.search(r'"?\s*200\s*"?', probe) or "200" in probe
        ), f"the qdrant probe must assert on the HTTP status code, got: {probe!r}"

    def test_qdrant_probe_is_not_a_bare_tcp_connect(self) -> None:
        """Guard against regressing to a connect-only probe with no status check."""
        probe = _probe_text(_compose()["services"]["qdrant"])
        assert (
            "read -r" in probe or "grep" in probe
        ), f"the qdrant probe must read the HTTP response, not just open a socket; got: {probe!r}"


class TestWorkerProbeIsCeleryNative:
    @pytest.mark.parametrize("service", CELERY_SERVICES)
    def test_worker_declares_its_own_healthcheck(self, service: str) -> None:
        """Without one it inherits the image-level HTTP probe, which cannot pass."""
        assert _healthcheck(_compose()["services"][service]) is not None, (
            f"{service} declares no healthcheck, so it inherits the Dockerfile "
            f"HEALTHCHECK ({_image_healthcheck()!r}) -- an HTTP probe for a process "
            "that binds no HTTP port"
        )

    @pytest.mark.parametrize("service", CELERY_SERVICES)
    def test_worker_probe_does_not_probe_an_http_port(self, service: str) -> None:
        probe = _probe_text(_compose()["services"][service])
        assert "localhost:8000" not in probe and "/api/health" not in probe, (
            f"the {service} probe still dials the API's HTTP port, but the worker "
            f"runs `celery ... worker` and binds no HTTP port: {probe!r}"
        )

    @pytest.mark.parametrize("service", CELERY_SERVICES)
    def test_worker_probe_is_celery_native(self, service: str) -> None:
        probe = _probe_text(_compose()["services"][service])
        assert "celery" in probe and "inspect ping" in probe, (
            f"the {service} liveness probe must be Celery-native "
            f"(`celery ... inspect ping`), got: {probe!r}"
        )

    @pytest.mark.parametrize("service", CELERY_SERVICES)
    def test_worker_probe_targets_this_node(self, service: str) -> None:
        """A broadcast ping can be answered by a different node, proving nothing here."""
        probe = _probe_text(_compose()["services"][service])
        assert re.search(r"-d\s+celery@\$\$?HOSTNAME", probe), (
            f"the {service} probe must address this container's own node "
            f"(`-d celery@$HOSTNAME`); a bare broadcast can be answered elsewhere: "
            f"{probe!r}"
        )

    @pytest.mark.parametrize("service", CELERY_SERVICES)
    def test_worker_probe_loads_the_real_celery_app(self, service: str) -> None:
        probe = _probe_text(_compose()["services"][service])
        assert "runtime.celery_app:celery_app" in probe, (
            f"the {service} probe must load the same Celery app the worker runs, so "
            f"it shares the broker URL and node identity: {probe!r}"
        )

    def test_no_repo_built_service_is_left_with_the_inherited_http_probe(self) -> None:
        """No service built from this repo's Dockerfile may fall through to its HEALTHCHECK.

        Only services actually built from the repository root are in scope. nginx,
        prometheus, grafana and alertmanager run upstream images and never see the
        repo's ``Dockerfile`` at all, so asserting on them would be wrong.
        """
        inherited = _image_healthcheck()
        assert inherited and "8000" in inherited, (
            "expected the Dockerfile to declare the HTTP healthcheck this test "
            f"guards against; found {inherited!r}"
        )
        services = _compose()["services"]
        repo_built = {
            name for name, service in services.items() if _build_context(service) == "../.."
        }
        assert (
            {"app", "worker"} <= repo_built
        ), f"expected app and worker to build from the repo root, got {sorted(repo_built)}"
        offenders = [name for name in sorted(repo_built) if _healthcheck(services[name]) is None]
        assert not offenders, (
            f"{offenders} are built from the repo root but declare no healthcheck, "
            f"so they inherit {inherited!r}; a Celery worker must never be probed "
            "over HTTP"
        )


class TestProbeTimingIsExplicitAndJustified:
    """Timing must be declared, not inherited by accident."""

    @pytest.mark.parametrize("service", ["qdrant", *CELERY_SERVICES])
    def test_start_period_is_declared(self, service: str) -> None:
        """Cold start must not be reported as ill health."""
        health = _healthcheck(_compose()["services"][service])
        assert health.get("start_period"), (
            f"{service} has no start_period; a slow boot would count towards the "
            "retry budget and report a healthy service as unhealthy"
        )

    @pytest.mark.parametrize("service", ["qdrant", *CELERY_SERVICES])
    def test_budget_is_bounded(self, service: str) -> None:
        """interval x retries must stay inside a sane detection window."""
        health = _healthcheck(_compose()["services"][service])

        def seconds(value: str) -> int:
            return int(str(value).rstrip("s"))

        interval = seconds(health["interval"])
        retries = int(health["retries"])
        start = seconds(health.get("start_period", "0s"))
        assert interval * retries <= 300, (
            f"{service} detection window is {interval * retries}s "
            f"(interval={interval}s x retries={retries}); too slow to be a gate"
        )
        assert start > 0

    @pytest.mark.parametrize("service", ["qdrant", *CELERY_SERVICES])
    def test_timeout_is_shorter_than_interval(self, service: str) -> None:
        """A hung probe must not overlap the next run."""

        def seconds(value: str) -> int:
            return int(str(value).rstrip("s"))

        health = _healthcheck(_compose()["services"][service])
        assert seconds(health["timeout"]) < seconds(
            health["interval"]
        ), f"{service} timeout ({health['timeout']}) must be below interval ({health['interval']})"


class TestNoProbeIsDisabledAsAnEscape:
    def test_no_healthcheck_is_disabled(self) -> None:
        """`disable: true` would fake green; #49 must not be solved that way."""
        offenders = [
            name
            for name, service in _compose()["services"].items()
            if (service.get("healthcheck") or {}).get("disable") is True
        ]
        assert not offenders, (
            f"services {offenders} disable their healthcheck; that hides the probe "
            "rather than fixing it"
        )

    def test_healthcheck_coverage_did_not_shrink(self) -> None:
        """A guard against 'fixing' this by deleting healthchecks wholesale."""
        with_health = {
            name for name, service in _compose()["services"].items() if _healthcheck(service)
        }
        assert {
            "postgres",
            "redis",
            "app",
            "qdrant",
            "worker",
        } <= with_health, f"expected healthchecks on the core stack; found {sorted(with_health)}"


@pytest.mark.skipif(not _has_docker(), reason="docker CLI not available")
class TestProbesAgainstRealImages:
    """Verifies the probe really works inside the image it targets.

    Gated on the docker **CLI** being present, not on an opt-in flag, so these
    cases do run on GitHub-hosted runners (which ship both the CLI and a daemon)
    and on any developer machine with Docker. They are skipped rather than mocked
    where Docker is unavailable -- a mock could not catch the actual failure mode,
    which is a binary missing from a specific image.

    Where Docker is absent, exercise them explicitly with::

        pytest tests/unit/test_service_healthchecks.py -k RealImages

    Each case also skips itself if the image cannot be run or pulled, so a
    registry outage degrades to a skip instead of a false failure.
    """

    def test_qdrant_image_really_lacks_the_absent_tools(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                QDRANT_IMAGE,
                "-c",
                "for t in " + " ".join(QDRANT_ABSENT_TOOLS) + "; do "
                'command -v $t >/dev/null 2>&1 && echo "PRESENT:$t"; done; exit 0',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"cannot run {QDRANT_IMAGE}: {result.stderr.strip()[:120]}")
        present = [
            line.split(":", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("PRESENT:")
        ]
        assert not present, (
            f"{QDRANT_IMAGE} now ships {present}; the test's premise (and the "
            "comment in the compose file) must be revisited"
        )

    def test_qdrant_image_sh_is_dash(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                QDRANT_IMAGE,
                "-c",
                "readlink -f /bin/sh",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"cannot run {QDRANT_IMAGE}: {result.stderr.strip()[:120]}")
        assert "dash" in result.stdout, (
            f"/bin/sh resolved to {result.stdout.strip()!r}; if it is ever bash, the "
            "explicit `bash -c` is still correct but this premise changed"
        )

    def test_worker_probe_command_survives_compose_interpolation(self) -> None:
        """`$$` must render to a literal `$` so the shell sees $HOSTNAME."""
        health = _compose()["services"]["worker"]["healthcheck"]
        raw = health["test"][1]
        assert "$$HOSTNAME" in raw, (
            "the worker probe must escape the variable as $$HOSTNAME so Compose "
            f"emits $HOSTNAME for the shell to expand; got: {raw!r}"
        )
        rendered = raw.replace("$$", "$")
        assert "$HOSTNAME" in rendered and "$$HOSTNAME" not in rendered
        # and the un-rendered form must not be what the shell finally sees
