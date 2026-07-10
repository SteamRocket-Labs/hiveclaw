"""Runtime validateInput gate for tool arguments."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _tool_parameter_schemas() -> dict[str, dict[str, Any]]:
    from app.tools.collector import collect_tools
    from app.tools.decorator import get_all_registered_tools

    metas = get_all_registered_tools()
    if not metas:
        collect_tools()
        metas = get_all_registered_tools()
    return {name: dict(meta.parameters or {"type": "object", "properties": {}}) for name, (meta, _fn) in metas.items()}


def validate_tool_arguments(tool_name: str, arguments: Any) -> list[str]:
    """Validate tool arguments against the registered JSON-schema subset.

    The platform already publishes JSON schemas to the model. This function is
    the matching runtime admission gate after hooks can rewrite arguments.
    """
    schema = _tool_parameter_schemas().get(str(tool_name or ""))
    if not isinstance(schema, dict):
        return []
    return _validate_schema(arguments, schema, path="$")


def _validate_schema(value: Any, schema: dict[str, Any], *, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    if "anyOf" in schema:
        variants = [variant for variant in schema.get("anyOf", []) if isinstance(variant, dict)]
        if variants and not any(not _validate_schema(value, variant, path=path) for variant in variants):
            errors.append(f"{path} does not match any allowed schema variant")

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path} must be one of {enum_values!r}")

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        errors.append(f"{path} must be {expected_type}")
        return errors

    if (
        schema.get("type") == "object"
        or isinstance(schema.get("properties"), dict)
        or isinstance(schema.get("required"), list)
    ):
        if not isinstance(value, dict):
            errors.append(f"{path} must be object")
            return errors
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key} is not allowed")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(_validate_schema(value[key], child_schema, path=f"{path}.{key}"))

    if schema.get("type") == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_schema(item, item_schema, path=f"{path}[{index}]"))

    return errors


def _matches_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(value, item) for item in expected_type)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
