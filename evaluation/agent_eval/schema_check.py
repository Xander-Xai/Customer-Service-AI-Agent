"""工具参数的 JSON Schema **子集**校验器。

为什么不直接用 ``jsonschema``
---------------------------
不是为了少一个依赖，而是为了让"不可判定"这件事**显式化**。本仓库的工具 schema
只用到一个小子集；遇到子集之外的关键字时，正确的做法是让调用方知道"这个 schema 我
判不了"（抛 :class:`SchemaError`），而不是**悄悄跳过校验**并报告"参数合法"。

后者是评测系统里最阴险的一类撒谎：schema 判不了 -> 不报错 -> 通过率 100%，
而实际上一条都没验。

支持的关键字：``type`` / ``properties`` / ``required`` /
``additionalProperties`` / ``enum`` / ``items``，外加一组**注释性**关键字
（``title`` / ``description`` / ``default`` / ``examples`` …，它们只影响文档展示）。
其余一律 :class:`SchemaError` —— 包括 ``pattern`` / ``minLength`` /
``oneOf`` 这类**带语义**但本子集不支持的关键字。
"""

from __future__ import annotations

from typing import Any

#: 参与**校验**的关键字。
_ANNOTATION_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "enum", "items"}
)

#: 纯注释性关键字：只影响文档展示，不影响取值是否合法。
#:
#: 必须显式列出而不是"忽略一切未知关键字" —— 后者会让新增的、**带语义**的关键字
#: （``pattern`` / ``minLength`` / ``format`` / ``oneOf`` …）被静默跳过，
#: 于是报告"参数合法"而实际上一条都没验。把 `description` 归到这一类，
#: 正是为了让「合法但没校验」与「确实校验过」这两件事保持可区分。
_ANNOTATION_ONLY_KEYS = frozenset(
    {"title", "description", "default", "examples", "$comment", "deprecated"}
)

_SUPPORTED_KEYS = _ANNOTATION_KEYS | _ANNOTATION_ONLY_KEYS

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class SchemaError(ValueError):
    """schema 本身无法被本子集校验器处理（而不是"参数不合法"）。"""


def unsupported_keywords(schema: Any) -> list[str]:
    """返回 schema（含其 properties/items）中本子集无法判定的关键字。"""
    found: set[str] = set()
    if isinstance(schema, dict):
        found |= set(schema) - _SUPPORTED_KEYS
        for key in ("properties",):
            props = schema.get(key)
            if isinstance(props, dict):
                for sub in props.values():
                    found |= set(unsupported_keywords(sub))
        items = schema.get("items")
        if items is not None:
            found |= set(unsupported_keywords(items))
    return sorted(found)


def _check_type(value: Any, expected: Any, path: str) -> list[str]:
    if expected is None:
        return []
    if isinstance(expected, list):
        return [
            f"{path}: unsupported json-schema type {t!r}" for t in expected if t not in _TYPE_CHECKS
        ]
    if expected not in _TYPE_CHECKS:
        return [
            f"{path}: unsupported json-schema type {expected!r}: expected type {expected}, got {type(value).__name__}"
        ]
    if not _TYPE_CHECKS[expected](value):
        return [f"{path}: expected type {expected}, got {type(value).__name__}"]
    return []


def validate_arguments(arguments: Any, schema: dict[str, Any]) -> list[str]:
    """返回错误列表（空 = 通过）。schema 不可判定时抛 :class:`SchemaError`。"""
    bad_keywords = unsupported_keywords(schema)
    if bad_keywords:
        raise SchemaError(f"schema uses keywords outside the supported subset: {bad_keywords}")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SchemaError("tool parameter schema must be an object")
    if not isinstance(arguments, dict):
        return [f": arguments must be an object, got {type(arguments).__name__}"]

    errors: list[str] = []
    for key, prop_schema in (schema.get("properties") or {}).items():
        if key not in arguments:
            continue
        value = arguments[key]
        errors += _check_type(value, prop_schema.get("type"), f".{key}")
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            errors.append(f".{key}: value not in enum {prop_schema['enum']}")
        if prop_schema.get("type") == "array" and isinstance(value, list):
            item_schema = prop_schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    errors += _check_type(item, item_schema.get("type"), f".{key}[{index}]")

    for key in schema.get("required") or []:
        if key not in arguments:
            errors.append(f".{key}: required property missing")

    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in (schema.get("properties") or {}):
                errors.append(f".{key}: property not declared and additionalProperties=false")

    return errors


__all__ = ["SchemaError", "unsupported_keywords", "validate_arguments"]
