"""Explicit industrial candidate boundary; legacy local fixtures remain separate."""

import os
from collections.abc import Mapping


def industrial_enabled() -> bool:
    profile = os.getenv("CSAI_RUNTIME_PROFILE", "").strip().lower()
    if profile not in {"", "industrial"}:
        raise ValueError("Invalid CSAI_RUNTIME_PROFILE")
    return profile == "industrial"


def validate_industrial_profile(settings: Mapping[str, object]) -> list[str]:
    required = {
        "DEV_MODE": False,
        "ERP_MODE": "real",
        "SESSION_STORAGE_BACKEND": "redis",
        "SESSION_ENCRYPTION_REQUIRED": True,
        "SESSION_ENCRYPTION_PLAINTEXT_COMPAT": "reject",
        "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
        "AGENT_RUN_DISPATCH": "celery",
        "AGENT_RUN_THREAD_LOCK_ENABLED": True,
        "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
        "HITL_ENABLED": True,
    }
    return [
        f"Industrial candidate requires {name}={value}"
        for name, value in required.items()
        if settings.get(name) != value
    ]
