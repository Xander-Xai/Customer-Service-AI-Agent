"""Regression tests for ADMIN_PASSWORD wiring in the production Compose stack.

Background
----------
``auth/service.py::init_default_admin`` creates the bootstrap ``admin`` account
only when the row is absent, and it reads the password *solely* from
``ADMIN_PASSWORD``; if that variable is missing it raises ``ValueError``. That
call runs at **module import time** in ``api/app_factory.py`` (not inside the
lifespan), so a missing variable aborts process start -- the API container
crash-loops on a fresh database instead of degrading.

The Compose stack used to never pass ``ADMIN_PASSWORD`` into any service, so an
operator could not satisfy that requirement at all, and ``.env.example`` /
README did not document the variable either.

Scope of these tests (deliberately Docker-free, per repo convention)
------------------------------------------------------------------
They pin the *contract* of the Compose file and the operator-facing docs:

- ``app`` must receive ``ADMIN_PASSWORD`` interpolated from operator config
- the interpolation must be fail-fast (``:?``), so a missing value fails
  ``docker compose config`` with an explicit message instead of crash-looping
- ``worker`` must **not** receive it: it never imports ``api.app_factory``, so
  injecting the secret would only widen its blast radius for no functional gain
- the docs operators read must declare the variable

They do not assert container runtime behaviour; that is covered by running
``docker compose config`` / bringing the stack up manually.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"
QUICK_LAUNCH = REPO_ROOT / "docs" / "checklists" / "quick-launch-checklist.md"


def _service_env(service_name: str) -> dict[str, str]:
    """Return ``service.environment`` as a ``{NAME: raw_value}`` mapping.

    Compose accepts both the ``KEY=value`` list form and the mapping form; the
    stack under test uses the list form, but normalising both keeps the test
    from being coupled to formatting.
    """
    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    services = yaml.safe_load(raw)["services"]
    assert service_name in services, f"compose file lost the {service_name!r} service"
    environment = services[service_name].get("environment") or {}

    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}

    resolved: dict[str, str] = {}
    for item in environment:
        key, _, value = str(item).partition("=")
        resolved[key.strip()] = value.strip()
    return resolved


class TestAppReceivesAdminPassword:
    def test_app_declares_admin_password(self) -> None:
        assert "ADMIN_PASSWORD" in _service_env("app"), (
            "compose app service must pass ADMIN_PASSWORD: init_default_admin() "
            "reads it at import time of api/app_factory.py and raises otherwise"
        )

    def test_app_admin_password_is_fail_fast_not_silent_default(self) -> None:
        """A ``:-`` default would silently hand the admin account a known password."""
        value = _service_env("app")["ADMIN_PASSWORD"]
        assert ":?" in value, (
            "ADMIN_PASSWORD must use the `:?` fail-fast form so a missing value "
            f"aborts `docker compose config` with an explicit message; got: {value!r}"
        )
        assert ":-" not in value, (
            "ADMIN_PASSWORD must not fall back to a default value; that would "
            f"reintroduce a guessable bootstrap password: {value!r}"
        )

    def test_app_admin_password_is_interpolated_from_operator_config(self) -> None:
        value = _service_env("app")["ADMIN_PASSWORD"]
        assert re.search(r"\$\{ADMIN_PASSWORD:", value), (
            "ADMIN_PASSWORD must be interpolated from operator config (.env), "
            f"not hardcoded in the Compose file; got: {value!r}"
        )


class TestWorkerDoesNotReceiveAdminPassword:
    """The worker shares the image but not the responsibility.

    ``runtime/celery_app.py`` -> ``runtime.tasks`` -> ``runtime.executor`` never
    imports ``api.app_factory``, so the worker never runs
    ``init_default_admin``. Injecting the bootstrap password into it would hand
    a privileged secret to a process that has no use for it.
    """

    def test_worker_does_not_declare_admin_password(self) -> None:
        assert "ADMIN_PASSWORD" not in _service_env("worker"), (
            "worker must not receive ADMIN_PASSWORD: it never imports "
            "api.app_factory, so the secret is unnecessary exposure"
        )

    def test_worker_service_still_exists_and_declares_its_role(self) -> None:
        """Guard against 'fixing' this by deleting the worker block."""
        assert _service_env("worker").get("SERVICE_ROLE") == "worker"


class TestOperatorDocsDeclareAdminPassword:
    @pytest.mark.parametrize(
        "doc",
        [ENV_EXAMPLE, README, QUICK_LAUNCH],
        ids=lambda p: p.name,
    )
    def test_doc_documents_admin_password(self, doc: Path) -> None:
        assert "ADMIN_PASSWORD" in doc.read_text(encoding="utf-8"), (
            f"{doc.relative_to(REPO_ROOT)} must document ADMIN_PASSWORD; it is "
            "required for the app container to bootstrap the admin account"
        )

    def test_env_example_uses_a_placeholder_not_a_real_secret(self) -> None:
        line = next(
            line
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.startswith("ADMIN_PASSWORD=")
        )
        _, _, value = line.partition("=")
        assert "your" in value.lower() or not value.strip(), (
            f".env.example is committed and must not carry a real password: {line!r}"
        )
