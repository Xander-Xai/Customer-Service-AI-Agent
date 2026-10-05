"""Regression tests for the ``ADMIN_PASSWORD`` Compose contract (Issue #47).

Root cause these tests pin
--------------------------
``auth/service.py::init_default_admin`` reads the bootstrap admin password
*only* from ``os.getenv("ADMIN_PASSWORD")`` (note: it deliberately does not go
through ``core/config.py`` — there is no such setting there) and raises
``ValueError`` when the variable is absent **and** the ``users`` table has no
``admin`` row yet.

That call sits at **module import time** in ``api/app_factory.py``
(``api/app_factory.py:32``), not inside the lifespan. So a missing variable
kills the process before it serves a single request — from the operator's side,
an ``app`` container that crash-loops on a fresh database.

The Compose stack used to pass ``ADMIN_PASSWORD`` to *no* service, so no value
written in ``.env`` could ever reach the container: the documented production
path (``make prod`` / README 方式一/方式二) was unstartable. It is now wired with
fail-fast interpolation, matching the existing ``POSTGRES_PASSWORD`` /
``GRAFANA_PASSWORD`` treatment.

What is asserted here
---------------------
Two layers, because they fail for different reasons:

1. **Contract shape** (always runs, Docker-free) — the Compose files declare
   the variable for the services that need it, use fail-fast ``:?`` rather than a
   silent ``:-`` default, and the operator-facing docs declare it too.
2. **Behaviour** (subprocess) — the claims layer 1 encodes are actually true:
   ``docker compose config`` fails with a *named* error when the variable is
   missing and succeeds when it is present; ``init_default_admin`` really raises
   on a fresh database without it and succeeds with it; and the worker import
   chain really never reaches ``api.app_factory``, which is why the worker is
   deliberately excluded rather than merely overlooked.

Layer 2's Docker-backed cases skip where the Compose CLI is absent. They are
pure config interpolation (``docker compose config``) and need no daemon.
Container runtime behaviour is NOT asserted here and remains NOT_MEASURED —
see the PR's Known boundaries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
BASE_COMPOSE = COMPOSE_DIR / "docker-compose.yml"
PROD_COMPOSE = COMPOSE_DIR / "docker-compose.prod.yml"
CANARY_COMPOSE = COMPOSE_DIR / "docker-compose.canary.yml"
SCALE_COMPOSE = COMPOSE_DIR / "docker-compose.scale.yml"

ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"
QUICK_LAUNCH = REPO_ROOT / "docs" / "checklists" / "quick-launch-checklist.md"
GEN_PROD_ENV = REPO_ROOT / "scripts" / "generate_prod_env.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _service_env(path: Path, service: str) -> dict[str, str]:
    """Return ``service.environment`` as a ``{NAME: raw_value}`` mapping.

    Compose accepts both the ``KEY=value`` list form and the mapping form. The
    stack under test uses the list form, but normalising both keeps these tests
    from being coupled to formatting.
    """
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]
    assert service in services, f"{path.name} lost the {service!r} service"
    environment = services[service].get("environment") or {}

    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}

    resolved: dict[str, str] = {}
    for item in environment:
        key, _, value = str(item).partition("=")
        resolved[key.strip()] = value.strip()
    return resolved


def _compose_config_env(
    extra: list[str], files: list[Path] | None = None
) -> subprocess.CompletedProcess:
    """Run ``docker compose config`` against the real stack, hermetically.

    The environment is built from scratch (no inherited ``ADMIN_PASSWORD``) so
    the "missing variable" case cannot be masked by the developer's own shell or
    by a ``.env`` sitting in the tree. ``--env-file /dev/null`` stops Compose from
    auto-loading a project ``.env``; the sibling ``:?`` requirements are satisfied
    with throwaway probe values so only ``ADMIN_PASSWORD`` is under test.

    ``extra`` is a list of ``KEY=VALUE`` strings so a caller can express the
    "set but empty" case (``ADMIN_PASSWORD=``), which a dict argument cannot.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "POSTGRES_PASSWORD": "probe_not_a_real_secret",
        "GRAFANA_PASSWORD": "probe_not_a_real_secret",
    }
    env.update(dict(item.split("=", 1) for item in extra))
    args = ["docker", "compose", "--project-directory", str(COMPOSE_DIR)]
    for f in files or [BASE_COMPOSE, PROD_COMPOSE]:
        args += ["-f", str(f)]
    args += ["--env-file", "/dev/null", "config"]
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=180)


def _has_compose_cli() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.returncode == 0


requires_compose_cli = pytest.mark.skipif(
    not _has_compose_cli(),
    reason="docker compose CLI not available; contract-shape assertions still cover the wiring",
)


# ---------------------------------------------------------------------------
# A. contract shape (Docker-free, always runs)
# ---------------------------------------------------------------------------


class TestAppServiceDeclaresFailFastAdminPassword:
    """`app` is the service that crash-looped; it must receive the variable."""

    @pytest.mark.unit
    def test_app_declares_admin_password(self) -> None:
        assert "ADMIN_PASSWORD" in _service_env(BASE_COMPOSE, "app"), (
            "compose app service must pass ADMIN_PASSWORD: init_default_admin() "
            "reads it at import time of api/app_factory.py and raises otherwise"
        )

    @pytest.mark.unit
    def test_admin_password_uses_fail_fast_not_a_silent_default(self) -> None:
        """A ``:-`` default would hand the bootstrap admin a guessable password."""
        value = _service_env(BASE_COMPOSE, "app")["ADMIN_PASSWORD"]
        assert ":?" in value, (
            "ADMIN_PASSWORD must use the `:?` fail-fast form so a missing value "
            f"aborts `docker compose config` with an explicit message; got: {value!r}"
        )
        assert ":-" not in value, (
            "ADMIN_PASSWORD must not fall back to a default value; that would "
            f"reintroduce a guessable bootstrap password: {value!r}"
        )

    @pytest.mark.unit
    def test_admin_password_is_interpolated_from_operator_config(self) -> None:
        """The value must come from .env, not be hardcoded in the Compose file."""
        value = _service_env(BASE_COMPOSE, "app")["ADMIN_PASSWORD"]
        assert "${ADMIN_PASSWORD:" in value, (
            "ADMIN_PASSWORD must be interpolated from operator config (.env), "
            f"not hardcoded in the Compose file; got: {value!r}"
        )

    @pytest.mark.unit
    def test_fail_fast_message_names_the_variable(self) -> None:
        """The operator must be told *which* variable to set, not just that one is missing."""
        value = _service_env(BASE_COMPOSE, "app")["ADMIN_PASSWORD"]
        assert "ADMIN_PASSWORD" in value, f"fail-fast hint must name the variable; got: {value!r}"


class TestCanaryServiceDeclaresAdminPassword:
    """`canary` is a *separate* service, so it does not inherit `app`'s environment.

    It builds the same image and therefore runs the same import-time
    ``init_default_admin()``; leaving it unwired would keep ``make canary``
    crash-looping on a fresh database for the identical reason.
    """

    @pytest.mark.unit
    def test_canary_declares_fail_fast_admin_password(self) -> None:
        env = _service_env(CANARY_COMPOSE, "canary")
        assert "ADMIN_PASSWORD" in env, (
            "canary shares the app image and runs api.app_factory:app, so it "
            "needs the same ADMIN_PASSWORD contract as app"
        )
        assert ":?" in env["ADMIN_PASSWORD"], (
            f"canary ADMIN_PASSWORD must be fail-fast; got: {env['ADMIN_PASSWORD']!r}"
        )
        assert ":-" not in env["ADMIN_PASSWORD"], (
            f"canary ADMIN_PASSWORD must not default; got: {env['ADMIN_PASSWORD']!r}"
        )


class TestOverrideFilesDoNotDropTheContract:
    """Overlay files merge `environment` per key, so they must not blank it out."""

    @pytest.mark.unit
    def test_prod_and_scale_overlays_do_not_unset_admin_password(self) -> None:
        for overlay, service in ((PROD_COMPOSE, "app"), (SCALE_COMPOSE, "app")):
            env = _service_env(overlay, service)
            assert "ADMIN_PASSWORD" not in env, (
                f"{overlay.name} re-declares {service}.environment with ADMIN_PASSWORD; "
                "a bare override would shadow the base fail-fast value"
            )


class TestWorkerDoesNotReceiveAdminPassword:
    """The worker shares the image but not the responsibility.

    ``runtime/celery_app.py`` -> ``runtime.tasks`` -> ``runtime.executor`` never
    imports ``api.app_factory``, so the worker never runs ``init_default_admin``.
    Injecting a high-privilege bootstrap password into a process with no use for
    it would only widen the blast radius.
    """

    @pytest.mark.unit
    def test_worker_does_not_declare_admin_password(self) -> None:
        assert "ADMIN_PASSWORD" not in _service_env(BASE_COMPOSE, "worker"), (
            "worker must not receive ADMIN_PASSWORD: it never imports "
            "api.app_factory, so the secret is unnecessary exposure"
        )

    @pytest.mark.unit
    def test_worker_still_declares_its_role(self) -> None:
        """Guards against 'fixing' this by deleting the worker block."""
        assert _service_env(BASE_COMPOSE, "worker").get("SERVICE_ROLE") == "worker"


class TestOperatorDocsDeclareAdminPassword:
    """The docs an operator actually reads must declare the variable."""

    @pytest.mark.unit
    @pytest.mark.parametrize("doc", [ENV_EXAMPLE, README, QUICK_LAUNCH], ids=lambda p: p.name)
    def test_doc_documents_admin_password(self, doc: Path) -> None:
        assert "ADMIN_PASSWORD" in doc.read_text(encoding="utf-8"), (
            f"{doc.relative_to(REPO_ROOT)} must document ADMIN_PASSWORD; without it "
            "the app container cannot bootstrap the admin account"
        )

    @pytest.mark.unit
    def test_env_example_declares_a_placeholder_not_a_real_secret(self) -> None:
        lines = [
            line
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.startswith("ADMIN_PASSWORD=")
        ]
        assert lines, ".env.example must declare ADMIN_PASSWORD with a placeholder"
        _, _, value = lines[0].partition("=")
        assert "your" in value.lower() or not value.strip(), (
            f".env.example is committed and must not carry a real password: {lines[0]!r}"
        )


class TestDocumentedProdSetupPathCanSupplyTheVariable:
    """`scripts/generate_prod_env.py` is the documented way to build `.env.prod`.

    Since Compose now fails fast without the variable, that generator must be able
    to produce it — otherwise the documented setup path could only ever fail. The
    generator is placeholder-driven and `.env.prod` is operator-owned (gitignored),
    so the assertion is on the mechanism: a ``CHANGE_ME_*`` placeholder for
    ``ADMIN_PASSWORD`` is substituted with a real secret. No script change is needed.
    """

    @pytest.mark.unit
    def test_generator_replaces_admin_password_placeholder(self, tmp_path: Path) -> None:
        """Drive the real script, not a re-implementation of its logic."""
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "generate_prod_env.py").write_text(
            GEN_PROD_ENV.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (tmp_path / ".env.prod").write_text(
            "JWT_SECRET=CHANGE_ME_TO_SECURE_RANDOM_JWT_SECRET\n"
            "ADMIN_PASSWORD=CHANGE_ME_TO_SECURE_PASSWORD\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(script_dir / "generate_prod_env.py")],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        generated = (tmp_path / ".env.prod.generated").read_text(encoding="utf-8")
        line = next(row for row in generated.splitlines() if row.startswith("ADMIN_PASSWORD="))
        _, _, value = line.partition("=")
        assert value.strip(), "ADMIN_PASSWORD must be generated, not left empty"
        assert "CHANGE_ME" not in value, (
            f"the CHANGE_ME placeholder must be substituted; got: {line!r}"
        )
        assert value.strip() != "CHANGE_ME_TO_SECURE_PASSWORD"


# ---------------------------------------------------------------------------
# B. behaviour: `docker compose config` (no daemon needed; pure interpolation)
# ---------------------------------------------------------------------------


@requires_compose_cli
class TestComposeConfigEnforcesTheContract:
    """The acceptance behaviour: missing variable -> explicit failure, correct variable -> success."""

    @pytest.mark.unit
    def test_config_fails_when_admin_password_is_unset(self) -> None:
        result = _compose_config_env(extra=[])
        assert result.returncode != 0, (
            "compose config must fail when ADMIN_PASSWORD is unset; instead it "
            f"succeeded, which means the app container would crash-loop:\n{result.stdout}"
        )
        combined = result.stdout + result.stderr
        assert "ADMIN_PASSWORD" in combined, (
            "the failure must name ADMIN_PASSWORD so the operator knows what to set; "
            f"got:\n{combined}"
        )

    @pytest.mark.unit
    def test_config_fails_when_admin_password_is_empty(self) -> None:
        """An empty value is not a value: `:?` also rejects it.

        Without this, `ADMIN_PASSWORD=` in .env would pass interpolation and put
        an empty password on the bootstrap admin account.
        """
        result = _compose_config_env(extra=["ADMIN_PASSWORD="])
        assert result.returncode != 0, (
            "an empty ADMIN_PASSWORD must fail closed, not silently bootstrap an "
            f"empty-password admin:\n{result.stdout}"
        )
        assert "ADMIN_PASSWORD" in result.stdout + result.stderr

    @pytest.mark.unit
    def test_config_succeeds_and_app_receives_the_value(self) -> None:
        result = _compose_config_env(extra=["ADMIN_PASSWORD=probe_admin_secret"])
        assert result.returncode == 0, f"compose config failed:\n{result.stderr}"
        rendered = yaml.safe_load(result.stdout)["services"]["app"]["environment"]
        assert rendered.get("ADMIN_PASSWORD") == "probe_admin_secret", (
            "the operator-supplied value must reach the app service verbatim; "
            f"got: {rendered.get('ADMIN_PASSWORD')!r}"
        )

    @pytest.mark.unit
    def test_worker_does_not_receive_the_value_in_the_rendered_stack(self) -> None:
        result = _compose_config_env(extra=["ADMIN_PASSWORD=probe_admin_secret"])
        assert result.returncode == 0, f"compose config failed:\n{result.stderr}"
        rendered = yaml.safe_load(result.stdout)["services"]["worker"]["environment"]
        assert "ADMIN_PASSWORD" not in rendered, (
            "the rendered worker service must not carry ADMIN_PASSWORD; got: "
            f"{rendered.get('ADMIN_PASSWORD')!r}"
        )

    @pytest.mark.unit
    def test_canary_config_succeeds_only_with_the_variable(self) -> None:
        missing = _compose_config_env(extra=[], files=[BASE_COMPOSE, CANARY_COMPOSE])
        assert missing.returncode != 0
        assert "ADMIN_PASSWORD" in missing.stdout + missing.stderr

        present = _compose_config_env(
            extra=["ADMIN_PASSWORD=probe_admin_secret"], files=[BASE_COMPOSE, CANARY_COMPOSE]
        )
        assert present.returncode == 0, f"compose config failed:\n{present.stderr}"
        rendered = yaml.safe_load(present.stdout)["services"]["canary"]["environment"]
        assert rendered.get("ADMIN_PASSWORD") == "probe_admin_secret"


# ---------------------------------------------------------------------------
# C. behaviour: the runtime facts the contract depends on
# ---------------------------------------------------------------------------


def _run_python(code: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a snippet in a fresh interpreter rooted at a throwaway working dir.

    A fresh ``cwd`` gives the subprocess its own SQLite database, which is what
    makes the "fresh database" precondition observable (an existing ``admin`` row
    short-circuits the read entirely).
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
        # API-role config validation would reject an empty API_KEY; irrelevant
        # here, so disable it rather than weakening any assertion.
        "SERVICE_ROLE": "api",
        "API_KEY_ENABLED": "false",
        "DEV_MODE": "true",
    }
    env.update(env_extra)
    return subprocess.run(
        [os.environ.get("PYTHON", "python3"), "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestFreshDatabaseBootstrapRequiresTheVariable:
    """Reproduces the crash-loop precondition the Compose contract exists to satisfy.

    The snippets mirror ``api/app_factory.py``'s real order (``init_db()`` then
    ``init_default_admin()``); calling ``init_default_admin`` against a database
    that was never created fails earlier with "no such table: users" and would
    test the wrong thing.
    """

    _BOOTSTRAP = "from db.database import init_db; init_db()\nimport auth.service as s\ns.init_default_admin()\n"

    @pytest.mark.unit
    def test_init_default_admin_raises_without_the_variable(self, tmp_path: Path) -> None:
        result = _run_python(self._BOOTSTRAP, tmp_path, {})
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "a fresh database without ADMIN_PASSWORD must fail; if it no longer "
            f"does, the Compose fail-fast contract may no longer be necessary:\n{combined}"
        )
        assert "ADMIN_PASSWORD" in combined, f"failure must name the variable:\n{combined}"

    @pytest.mark.unit
    def test_init_default_admin_succeeds_with_the_variable(self, tmp_path: Path) -> None:
        result = _run_python(
            self._BOOTSTRAP + "print('BOOTSTRAP_OK')\n",
            tmp_path,
            {"ADMIN_PASSWORD": "probe_admin_secret"},
        )
        assert result.returncode == 0, f"bootstrap with the variable failed:\n{result.stderr}"
        assert "BOOTSTRAP_OK" in result.stdout

    @pytest.mark.unit
    def test_existing_admin_short_circuits_the_variable_read(self, tmp_path: Path) -> None:
        """Pins the qualifier the docs rely on: the variable matters only on first bootstrap.

        Once the ``admin`` row exists, ``init_default_admin`` skips the read
        entirely — which is why the failure is intermittent in practice and why the
        docs say "first bootstrap only" rather than "always required".
        """
        first = _run_python(self._BOOTSTRAP, tmp_path, {"ADMIN_PASSWORD": "probe_admin_secret"})
        assert first.returncode == 0, f"first bootstrap failed:\n{first.stderr}"

        second = _run_python(self._BOOTSTRAP + "print('SECOND_OK')\n", tmp_path, {})
        assert second.returncode == 0, (
            "re-running against an existing admin must not require the variable; "
            f"got:\n{second.stdout}{second.stderr}"
        )
        assert "SECOND_OK" in second.stdout

    @pytest.mark.unit
    def test_the_raise_happens_at_app_factory_import_time(self, tmp_path: Path) -> None:
        """Pins *where* the failure lives: import time, not inside the lifespan.

        This is what makes the symptom a crash loop rather than a failed request,
        and it is why Issue #50 (lifespan/uvicorn diagnostics) does not and cannot
        fix this one: nothing ever reaches the lifespan.
        """
        result = _run_python("import api.app_factory", tmp_path, {})
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "ADMIN_PASSWORD" in combined
        assert "app_factory.py" in combined, (
            "the failure must be attributable to api/app_factory.py at import time; "
            f"got:\n{combined}"
        )

    @pytest.mark.unit
    def test_app_factory_import_succeeds_with_the_variable(self, tmp_path: Path) -> None:
        """The positive half of the crash-loop story: the variable makes import work."""
        result = _run_python(
            "import api.app_factory; print('APP_IMPORT_OK')",
            tmp_path,
            {"ADMIN_PASSWORD": "probe_admin_secret"},
        )
        assert result.returncode == 0, (
            f"importing api.app_factory must succeed once ADMIN_PASSWORD is set:\n{result.stderr}"
        )
        assert "APP_IMPORT_OK" in result.stdout


class TestWorkerImportChainNeverReachesInitDefaultAdmin:
    """Empirical backing for excluding the worker, instead of assuming it."""

    @pytest.mark.unit
    def test_worker_chain_does_not_import_app_factory(self, tmp_path: Path) -> None:
        result = _run_python(
            "import sys, runtime.celery_app  # noqa: F401\n"
            "print('APP_FACTORY_IMPORTED', 'api.app_factory' in sys.modules)\n"
            "print('AUTH_SERVICE_IMPORTED', 'auth.service' in sys.modules)",
            tmp_path,
            {"SERVICE_ROLE": "worker"},
        )
        assert result.returncode == 0, f"worker-role import failed:\n{result.stderr}"
        assert "APP_FACTORY_IMPORTED False" in result.stdout, (
            "the worker chain must not import api.app_factory, otherwise the worker "
            f"would need ADMIN_PASSWORD too; got:\n{result.stdout}"
        )
        assert "AUTH_SERVICE_IMPORTED False" in result.stdout, (
            f"the worker chain must not import auth.service; got:\n{result.stdout}"
        )

    @pytest.mark.unit
    def test_worker_import_succeeds_without_the_variable(self, tmp_path: Path) -> None:
        """The exclusion must be load-bearing: worker boots fine without it."""
        result = _run_python(
            "import runtime.celery_app  # noqa: F401", tmp_path, {"SERVICE_ROLE": "worker"}
        )
        assert result.returncode == 0, (
            "the worker must start without ADMIN_PASSWORD; if this now fails, the "
            f"worker exclusion in compose needs revisiting:\n{result.stderr}"
        )
