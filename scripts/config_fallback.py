#!/usr/bin/env python3
"""AST extraction of ``core/config.py`` runtime fallback defaults.

Why not import ``core.config``: importing it executes ``load_dotenv(override=
True)``, so the resulting module attributes are the developer's *effective*
configuration, not the canonical fallback. Documentation guards must compare
against the fallback literals written in the source, independent of any local
``.env``.

Only plain, statically-resolvable defaults are returned (env-helper calls with
constant defaults, bare string/int/bool constants, and
``os.getenv(...).lower() == "true"`` boolean flags). Computed/conditional
defaults are skipped rather than guessed.

Note on ``ENV_KEY -> constant``: several constants (e.g. ``VERSION``) read an
env var whose name differs from the Python constant (``APP_VERSION``).
Callers should look up by the **constant name** they document.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ENV_HELPERS = {
    "os.getenv",
    "getenv",
    "_int_env",
    "_float_env",
    "_str_env",
}


def _call_default(call: ast.Call) -> object | None:
    if call.keywords:
        for kw in call.keywords:
            if kw.arg in {"default", "default_value"}:
                return getattr(kw.value, "value", None)
    if len(call.args) >= 2:
        return getattr(call.args[1], "value", None)
    return None


def _bool_from_compare(node: ast.Compare) -> bool | None:
    """Recognize ``X.lower() == "true"`` / ``X == "1"`` style flags."""
    comparators = [getattr(c, "value", None) for c in node.comparators]
    if not comparators:
        return None
    raw = comparators[0]
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(raw, bool):
        return raw
    return None


def _resolve(node: ast.AST) -> object | None:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Compare):
        return _bool_from_compare(node)
    if isinstance(node, ast.Call):
        func = node.func
        dotted = (
            f"{getattr(func.value, 'id', '')}.{func.attr}"
            if isinstance(func, ast.Attribute)
            else getattr(func, "id", "")
        )
        if dotted not in _ENV_HELPERS:
            return None
        return _call_default(node)
    return None


def extract_fallback_defaults(config_path: Path) -> dict[str, str]:
    """Return ``{CONSTANT_NAME: rendered_fallback}`` from ``core/config.py``."""
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = _resolve(node.value)
        if value is None:
            continue
        out[target.id] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
    return out
