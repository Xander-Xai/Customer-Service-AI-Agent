"""Data-minimized A2A experiment contract, independent of SDK or application state.

The only values crossing the Agent boundary are fixed synthetic topic identifiers.
No customer messages, memory, PII, order IDs or ERP results are ever forwarded.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

ALLOWED_TOPICS = frozenset({"returns_demo", "shipping_demo", "invoice_demo"})
SYNTHETIC_FAQ = {
    "returns_demo": (
        "SYNTHETIC EXAMPLE ONLY: A demo return inquiry is routed to a human policy owner. "
        "Never infer a real refund eligibility or initiate a refund from this example."
    ),
    "shipping_demo": (
        "SYNTHETIC EXAMPLE ONLY: A demo delivery inquiry requires the authorized "
        "order system. This sample supplies no shipment or customer facts."
    ),
    "invoice_demo": (
        "SYNTHETIC EXAMPLE ONLY: A demo invoice inquiry requires verification by "
        "the authorized billing team. This sample supplies no tax or billing facts."
    ),
}


class A2AIntegrationDisabled(RuntimeError):
    """Configuration disallows the experimental remote agent."""


class A2AContractError(ValueError):
    """Unsafe, unsupported or ill-formed A2A experiment request."""


def require_demo_enabled() -> None:
    if os.getenv("A2A_FAQ_EXPERIMENT_ENABLED", "").lower() != "true":
        raise A2AIntegrationDisabled("A2A experiment is disabled")
    if os.getenv("APP_ENV", "").lower() not in {"development", "local", "test", "testing"}:
        raise A2AIntegrationDisabled(
            "A2A experiment is allowed only in explicit local/test environments"
        )


def require_token() -> str:
    secret = os.getenv("A2A_FAQ_SHARED_TOKEN", "")
    if not isinstance(secret, str) or not 32 <= len(secret) <= 256 or not secret.isascii():
        raise A2AIntegrationDisabled("Explicit high-entropy A2A service token required")
    return secret


def validate_topic(topic: str) -> str:
    if not isinstance(topic, str) or topic not in ALLOWED_TOPICS:
        raise A2AContractError("Only fixed synthetic topic identifiers are allowed")
    return topic


def validate_loopback_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or len(base_url) > 180:
        raise A2AContractError("Invalid configured A2A endpoint")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise A2AContractError("A2A demo endpoint must be an explicit loopback HTTP host:port")
    return f"http://{parsed.hostname}:{parsed.port}"


def validate_private_https_base_url(base_url: str) -> str:
    """Operator-pinned DNS identity, never a model-supplied URL."""
    if not isinstance(base_url, str) or len(base_url) > 180:
        raise A2AContractError("Invalid configured private peer endpoint")
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise A2AContractError("Invalid configured private peer endpoint") from None
    labels = host.split(".")
    if (
        parsed.scheme != "https"
        or len(labels) < 2
        or all(label.isdigit() for label in labels)
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        or host.endswith(".localhost")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc != (f"{host}:{port}" if port is not None else host)
        or port == 0
    ):
        raise A2AContractError("Private A2A endpoint requires an exact HTTPS DNS name")
    return f"https://{parsed.netloc}"


def synthetic_policy_answer(topic: str) -> str:
    return SYNTHETIC_FAQ[validate_topic(topic)]
