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


def _dotted_call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return f"{getattr(func.value, 'id', '')}.{func.attr}"
    return getattr(func, "id", "")


def _is_env_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted_call_name(node) in _ENV_HELPERS


def _unwrap_env_call(node: ast.AST) -> ast.Call | None:
    """Return the underlying env-helper call, unwrapping string methods.

    ``os.getenv("K", "false").lower()`` is an ``ast.Call`` whose func is the
    ``.lower`` attribute of the env call, so a naive unwrap would step past the
    env call to the ``os`` name. This walks method wrappers (``.lower()`` /
    ``.strip()``) until it reaches the env helper itself.
    """
    while isinstance(node, ast.Call):
        if _is_env_call(node):
            return node
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
            node = node.func.value
            continue
        return None
    return None


_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return None


def _bool_from_compare(node: ast.Compare) -> bool | None:
    """Evaluate a boolean flag from the ``getenv`` fallback, not the literal.

    For ``os.getenv("QDRANT_PREFER_GRPC", "false").lower() == "true"`` the
    canonical fallback is ``false`` (evaluate the *default* against the
    comparator), not ``true`` from the comparison target.
    """
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    env_call = _unwrap_env_call(node.left)
    if env_call is None:
        return None
    default_bool = _as_bool(_call_default(env_call))
    comparator_bool = _as_bool(getattr(node.comparators[0], "value", None))
    if default_bool is None or comparator_bool is None:
        return None
    op = node.ops[0]
    if isinstance(op, ast.Eq):
        return default_bool == comparator_bool
    if isinstance(op, ast.NotEq):
        return default_bool != comparator_bool
    return None


def _resolve(node: ast.AST) -> object | None:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Compare):
        return _bool_from_compare(node)
    if _is_env_call(node):
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
