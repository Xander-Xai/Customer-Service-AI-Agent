"""Celery worker entrypoint for distributed infrastructure tests.

The application calls ``load_dotenv(override=True)``, so a normal Celery worker
would ignore process-env overrides and read the repo ``.env``. This runner
neutralizes dotenv *before* importing the app, letting the test harness pass a
deterministic environment (PostgreSQL URL, short lease/visibility timeouts,
fake runtime provider) to a real Celery worker process.

Usage (from repo root)::

    AGENT_RUN_RUNTIME_PROVIDER=tests.integration.fake_runtime_provider:provide \
    python -m tests.integration.celery_worker_runner
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import dotenv
from dotenv import dotenv_values

# Seed baseline config from the repo .env, but never let it override the
# process environment (parent harness overrides win).
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    for _key, _value in dotenv_values(_ENV_PATH).items():
        if _value is not None:
            os.environ.setdefault(_key, _value)

# Neutralize .env so process environment is authoritative for this worker.
dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]

from runtime.celery_app import celery_app  # noqa: E402


def main() -> None:
    argv = sys.argv[1:] or [
        "worker",
        "--loglevel=warning",
        "-Q",
        os.getenv("AGENT_RUN_QUEUE", "agent_runs"),
        "-c",
        "1",
        "--without-gossip",
        "--without-mingle",
    ]
    celery_app.worker_main(argv=argv)


if __name__ == "__main__":
    main()
