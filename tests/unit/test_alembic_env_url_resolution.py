"""Regression tests for ``alembic/env.py`` database-URL resolution.

Why these exist: ``run_migrations_online()`` used the module-level
``db.database.engine`` and never called ``_resolve_database_url()``. Combined
with ``core/config.py`` running ``load_dotenv(override=True)`` — which lets the
repo's ``.env`` overwrite a process-level ``DATABASE_URL`` — the practical effect
was that ``alembic upgrade head`` could only ever run against whatever the local
``.env`` happened to say (usually SQLite). That is exactly how "TIMESTAMPTZ /
concurrency correctness verified on SQLite" happens while production runs
PostgreSQL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / "alembic" / "env.py"


def _load_env_module():
    """Import alembic/env.py as a module without running migrations.

    ``alembic.env`` executes ``run_migrations_*`` at import time when an Alembic
    context is configured, so it is loaded as a plain module object and only the
    pure URL-resolution helper is exercised.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_alembic_env_under_test", ENV_PATH)
    module = importlib.util.module_from_spec(spec)
    # Stub the alembic context so importing env.py does not require a live
    # Alembic run; ``run_migrations_*`` is never called by these tests.
    class _FakeConfig:
        config_file_name = None  # skips fileConfig()

    class _BeginTransaction:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    class _Ctx:
        config = _FakeConfig()

        def is_offline_mode(self):
            return False

        def configure(self, **_kwargs):
            return None

        def begin_transaction(self):
            return _BeginTransaction()

        def run_migrations(self):
            return None

    sys.modules.setdefault("alembic", type(sys)("alembic"))
    sys.modules["alembic"].context = _Ctx()  # type: ignore[attr-defined]
    sys.modules["alembic"].command = type(sys)("command")  # type: ignore[attr-defined]
    sys.modules["alembic"].__version__ = "1.0"
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("alembic", None)
    return module


@pytest.fixture(scope="module")
def env_module():
    return _load_env_module()


def test_alembic_database_url_wins_over_dotenv_clobbered_database_url(
    env_module, monkeypatch
):
    """``ALEMBIC_DATABASE_URL`` is the escape hatch that survives dotenv.

    ``load_dotenv(override=True)`` in ``core/config.py`` overwrites
    ``DATABASE_URL`` from the repo ``.env``, so a process-level
    ``DATABASE_URL=postgresql://...`` cannot be trusted to reach Alembic.
    ``ALEMBIC_DATABASE_URL`` is not declared in any ``.env`` template and must
    therefore be honoured verbatim.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/csai.db")
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/verify"
    )
    assert env_module._resolve_database_url() == "postgresql://postgres:postgres@localhost:5432/verify"


def test_database_url_used_when_no_alembic_specific_override(env_module, monkeypatch):
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert env_module._resolve_database_url() == "postgresql://u:p@h:5432/db"


def test_blank_values_fall_through_to_the_default_engine(env_module, monkeypatch):
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", "   ")
    monkeypatch.setenv("DATABASE_URL", "")
    resolved = env_module._resolve_database_url()
    # Falls back to whatever db.database.engine resolved to (SQLite in dev).
    assert resolved == str(env_module._default_engine.url)


def test_online_migrations_honor_the_resolved_url(env_module, monkeypatch):
    """The online path must not bypass URL resolution.

    A structural assertion, not a behavioural one: it fails if someone restores
    the old ``connectable = _default_engine`` body, which would silently ignore
    ``ALEMBIC_DATABASE_URL`` / ``DATABASE_URL`` again.
    """
    import inspect

    source = inspect.getsource(env_module.run_migrations_online)
    assert "_resolve_database_url()" in source, (
        "run_migrations_online must resolve the target through "
        "_resolve_database_url(); using the module-level engine directly makes "
        "DATABASE_URL/ALEMBIC_DATABASE_URL dead config"
    )
    assert "create_engine(" in source, (
        "a URL that differs from the default engine needs its own engine, "
        "otherwise the resolved URL is still ignored"
    )


def test_alembic_url_is_not_declared_in_env_templates():
    """If ``.env`` ever templates ALEMBIC_DATABASE_URL it becomes clobberable."""
    for rel in (".env.example", ".env.test", ".env.dev", ".env.prod"):
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            assert not line.startswith("ALEMBIC_DATABASE_URL"), (
                f"{rel} must not declare ALEMBIC_DATABASE_URL: dotenv override would "
                f"defeat the override precedence the Alembic contract depends on"
            )


def test_default_engine_and_resolved_url_agree_without_any_env(monkeypatch, env_module):
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert env_module._resolve_database_url() == str(env_module._default_engine.url)


def test_no_stray_dangling_env_reference():
    """``os`` must remain imported in env.py (the resolver reads it)."""
    text = ENV_PATH.read_text(encoding="utf-8")
    assert "import os" in text
    assert os.sep  # module object still usable; guards against a bad refactor


def test_inline_dotenv_comment_is_not_mistaken_for_a_url(env_module, monkeypatch):
    """``.env.example`` ships ``DATABASE_URL=   # 为空使用 SQLite`` .

    python-dotenv keeps the inline comment as the value, and
    ``load_dotenv(override=True)`` installs it into ``os.environ``. Handing that
    to ``create_engine`` raises ``ArgumentError: Could not parse SQLAlchemy URL``.
    Such a value must be treated as unset.
    """
    monkeypatch.setenv("DATABASE_URL", "# 为空使用 SQLite（开发环境）")
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    assert env_module._resolve_database_url() == str(env_module._default_engine.url)


@pytest.mark.parametrize(
    "junk",
    ["", "   ", "# comment", "sqlite", "localhost:5432/db", "///nope"],
)
def test_non_url_values_fall_back_to_the_default_engine(env_module, monkeypatch, junk: str):
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", junk)
    assert env_module._resolve_database_url() == str(env_module._default_engine.url)


@pytest.mark.parametrize(
    "good",
    [
        "postgresql://postgres:postgres@localhost:5432/db",
        "postgres://u:p@h:5432/db",
        "sqlite:///data/csai.db",
        "sqlite+aiosqlite:///data/csai.db",
    ],
)
def test_real_urls_are_passed_through_verbatim(env_module, monkeypatch, good: str):
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", good)
    assert env_module._resolve_database_url() == good


def test_quoted_urls_are_unwrapped(env_module, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", '"postgresql://u:p@h:5432/db"')
    assert env_module._resolve_database_url() == "postgresql://u:p@h:5432/db"


def test_invalid_database_url_from_env_does_not_break_the_alembic_cli(env_module, monkeypatch):
    """The import-time path must survive a broken DATABASE_URL.

    This is the regression the old body could not have caught: the old code
    ignored the URL entirely, so the latent ``create_engine('<comment>')`` crash
    only appears once the resolver is actually wired in.
    """
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "# 为空使用 SQLite（开发环境）")
    url = env_module._resolve_database_url()
    # Must be a string create_engine can accept.
    from sqlalchemy.engine import make_url

    make_url(url)
