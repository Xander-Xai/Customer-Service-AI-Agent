"""Fresh-DB migration gate runner.

Creates a **blank** PostgreSQL database, runs the full Alembic chain
(`alembic upgrade head`) against it, verifies the schema, then drops it.
No `alembic stamp`, no `Base.metadata.create_all()`.

The application calls ``load_dotenv(override=True)``, so a normal invocation
would read the repo ``.env`` instead of the target DB. This runner neutralizes
dotenv *before* importing the app/alembic so the process environment is
authoritative (same seam used by the Celery test worker).

Usage::

    FRESH_DB_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres \
        python -m tests.integration.fresh_db_migrate

Prints a JSON summary on success and exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_TABLES = {"users", "chat_histories", "audit_logs", "feedbacks", "prompt_versions"}
RUNTIME_TABLES = {"agent_runs", "agent_dead_letters", "tool_side_effects"}


def _target_url(admin_url: str, db_name: str) -> str:
    parts = urlsplit(admin_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", "", ""))


def _admin_conn(admin_url: str):
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    conn = psycopg2.connect(admin_url)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def main() -> int:
    admin_url = os.environ.get("FRESH_DB_ADMIN_URL", "").strip()
    if not admin_url:
        print(json.dumps({"ok": False, "error": "FRESH_DB_ADMIN_URL not set"}))
        return 2

    db_name = os.environ.get("FRESH_DB_NAME", "").strip() or f"csai_fresh_{uuid.uuid4().hex[:10]}"
    target_url = _target_url(admin_url, db_name)

    # 1. Create blank database.
    admin = _admin_conn(admin_url)
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    cur.execute(f'CREATE DATABASE "{db_name}"')
    cur.close()
    admin.close()

    result: dict[str, object] = {"ok": False, "db_name": db_name}
    try:
        # 2. Neutralize dotenv so DATABASE_URL from the process env is used.
        #    Seed non-DB baseline config from the repo .env (API_KEY etc.) so the
        #    app config validates, but never let .env override process env.
        import dotenv
        from dotenv import dotenv_values

        env_path = _REPO_ROOT / ".env"
        if env_path.exists():
            for key, value in dotenv_values(env_path).items():
                if value is not None:
                    os.environ.setdefault(key, value)
        dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
        os.environ["DATABASE_URL"] = target_url

        sys.path.insert(0, str(_REPO_ROOT))

        # 3. Run the real migration chain (no stamp, no create_all).
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")

        # 4. Verify schema.
        from sqlalchemy import create_engine, text

        engine = create_engine(target_url)
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                )
            }
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()

        missing_core = sorted(CORE_TABLES - tables)
        missing_runtime = sorted(RUNTIME_TABLES - tables)
        result.update(
            {
                "ok": not missing_core and not missing_runtime,
                "alembic_head": head,
                "tables": sorted(tables),
                "missing_core": missing_core,
                "missing_runtime": missing_runtime,
            }
        )
        return 0 if result["ok"] else 1
    except Exception as e:  # pragma: no cover - surfaced via JSON
        result["error"] = f"{type(e).__name__}: {e}"
        return 1
    finally:
        # 5. Cleanup (dispose engine first so DROP DATABASE is not blocked).
        try:
            from db.database import engine as _app_engine

            _app_engine.dispose()
        except Exception:
            pass
        try:
            admin = _admin_conn(admin_url)
            cur = admin.cursor()
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.close()
            admin.close()
        except Exception:
            pass
        print(json.dumps(result))


if __name__ == "__main__":
    raise SystemExit(main())
