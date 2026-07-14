"""MCP metadata trust boundary.

Remote MCP prose and JSON-Schema annotations are evidence, never model
instructions. This functional core keeps the raw material for administrator
review while producing a small, provider-neutral runtime description and a
mechanically constrained schema.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


MAX_RAW_DESCRIPTION_CHARS = 20_000
MAX_RAW_SCHEMA_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_PROPERTIES = 128
MAX_CANONICAL_DESCRIPTION_CHARS = 500

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROPERTY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ALLOWED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_COMBINATORS = {"oneOf", "anyOf", "allOf"}
_SCALAR_CONSTRAINTS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "multipleOf",
}
_TEXT_CONSTRAINTS = {"format", "pattern"}
_UNTRUSTED_ANNOTATIONS = {"description", "title", "$comment", "examples", "example", "default", "deprecated"}

_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prompt_injection", re.compile(r"\b(ignore|disregard|override)\b.{0,60}\b(instruction|prompt|policy)", re.I)),
    ("system_prompt_request", re.compile(r"\b(system|developer)\s+(prompt|message|instruction)", re.I)),
    ("credential_request", re.compile(r"\b(secret|credential|api[_ -]?key|access[_ -]?token|password)\b", re.I)),
    (
        "exfiltration_request",
        re.compile(r"\b(exfiltrate|upload|send|forward)\b.{0,60}\b(secret|memory|context|token)", re.I),
    ),
)


class MCPMetadataValidationError(ValueError):
    """Raised when untrusted MCP metadata cannot be represented safely."""


@dataclass(frozen=True)
class MCPMetadataCandidate:
    raw_description: str
    raw_schema: dict[str, Any]
    canonical_description: str
    canonical_schema: dict[str, Any]
    fingerprint: str
    risk_flags: tuple[str, ...]
    trust_status: str = "pending_review"
    trust_tier: str = "external_unreviewed"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _schema_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.append(str(key))
            result.extend(_schema_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_schema_strings(item))
        return result
    return []


def _risk_flags(raw_description: str, raw_schema: dict[str, Any]) -> tuple[str, ...]:
    corpus = "\n".join([raw_description, *_schema_strings(raw_schema)])
    return tuple(name for name, pattern in _RISK_PATTERNS if pattern.search(corpus))


def _safe_endpoint_host(server_url: str) -> str:
    host = (urlsplit(str(server_url or "")).hostname or "unidentified-endpoint").lower()
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
        return "unidentified-endpoint"
    return host


def _neutral_description(*, tool_name: str, server_url: str) -> str:
    return (
        f"External MCP operation '{tool_name}' provided by endpoint host "
        f"'{_safe_endpoint_host(server_url)}'. Remote metadata is untrusted and omitted. "
        "Use only with reviewed arguments and normal Hive governance."
    )


def _validate_enum(values: Any) -> list[Any]:
    if not isinstance(values, list) or len(values) > 100:
        raise MCPMetadataValidationError("MCP schema enum must be a list with at most 100 values")
    result: list[Any] = []
    for value in values:
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise MCPMetadataValidationError("MCP schema enum contains a non-scalar value")
        if isinstance(value, str):
            if len(value) > 128 or _risk_flags(value, {}):
                raise MCPMetadataValidationError("MCP schema enum contains unsafe text")
        result.append(value)
    return result


def _sanitize_schema_node(value: Any, *, depth: int, root: bool = False) -> dict[str, Any]:
    if depth > MAX_SCHEMA_DEPTH:
        raise MCPMetadataValidationError("MCP schema exceeds maximum depth")
    if not isinstance(value, dict):
        raise MCPMetadataValidationError("MCP schema nodes must be objects")
    if "$ref" in value or "$dynamicRef" in value or "$recursiveRef" in value:
        raise MCPMetadataValidationError("MCP schema remote references are forbidden")

    raw_type = value.get("type", "object" if root else None)
    if root and raw_type != "object":
        raise MCPMetadataValidationError("MCP schema root must be an object")
    if raw_type is not None and raw_type not in _ALLOWED_TYPES:
        raise MCPMetadataValidationError(f"Unsupported MCP schema type: {raw_type}")

    result: dict[str, Any] = {}
    if raw_type is not None:
        result["type"] = raw_type

    properties = value.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or len(properties) > MAX_SCHEMA_PROPERTIES:
            raise MCPMetadataValidationError("MCP schema properties must be a bounded object")
        clean_properties: dict[str, Any] = {}
        for name, child in properties.items():
            if not _PROPERTY_NAME_RE.fullmatch(str(name)):
                raise MCPMetadataValidationError(f"Unsafe MCP schema property name: {name!r}")
            clean_properties[str(name)] = _sanitize_schema_node(child, depth=depth + 1)
        result["properties"] = clean_properties

    required = value.get("required")
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise MCPMetadataValidationError("MCP schema required must be a string list")
        known = set((properties or {}).keys())
        if any(item not in known for item in required):
            raise MCPMetadataValidationError("MCP schema required references an unknown property")
        result["required"] = list(dict.fromkeys(required))

    if "items" in value:
        result["items"] = _sanitize_schema_node(value["items"], depth=depth + 1)
    for keyword in _COMBINATORS:
        if keyword not in value:
            continue
        options = value[keyword]
        if not isinstance(options, list) or not 1 <= len(options) <= 20:
            raise MCPMetadataValidationError(f"MCP schema {keyword} must be a bounded list")
        result[keyword] = [_sanitize_schema_node(option, depth=depth + 1) for option in options]
    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        if isinstance(additional, bool):
            result["additionalProperties"] = additional
        elif isinstance(additional, dict):
            result["additionalProperties"] = _sanitize_schema_node(additional, depth=depth + 1)
        else:
            raise MCPMetadataValidationError("MCP schema additionalProperties must be boolean or object")
    if "enum" in value:
        result["enum"] = _validate_enum(value["enum"])
    if "const" in value:
        result["const"] = _validate_enum([value["const"]])[0]
    for keyword in _SCALAR_CONSTRAINTS:
        if keyword in value and isinstance(value[keyword], (int, float)) and not isinstance(value[keyword], bool):
            result[keyword] = value[keyword]
    for keyword in _TEXT_CONSTRAINTS:
        if keyword in value:
            candidate = str(value[keyword])
            if len(candidate) > 256 or _risk_flags(candidate, {}):
                raise MCPMetadataValidationError(f"MCP schema {keyword} contains unsafe text")
            result[keyword] = candidate

    # Annotations are deliberately discarded, not escaped: escaping markup
    # would not remove natural-language prompt injection.
    _ = _UNTRUSTED_ANNOTATIONS.intersection(value)
    return result


def sanitize_mcp_schema(raw_schema: Any) -> dict[str, Any]:
    if raw_schema in (None, {}):
        raw_schema = {"type": "object", "properties": {}}
    if not isinstance(raw_schema, dict):
        raise MCPMetadataValidationError("MCP schema root must be an object")
    encoded = _canonical_json(raw_schema).encode("utf-8")
    if len(encoded) > MAX_RAW_SCHEMA_BYTES:
        raise MCPMetadataValidationError("MCP schema exceeds maximum size")
    return _sanitize_schema_node(raw_schema, depth=0, root=True)


def prepare_mcp_metadata_candidate(
    *,
    server_name: str,
    server_url: str,
    tool_name: str,
    raw_description: Any,
    raw_schema: Any,
) -> MCPMetadataCandidate:
    normalized_tool_name = str(tool_name or "").strip()
    if not _TOOL_NAME_RE.fullmatch(normalized_tool_name):
        raise MCPMetadataValidationError("Unsafe MCP tool name")
    description = str(raw_description or "").strip()
    if len(description) > MAX_RAW_DESCRIPTION_CHARS:
        raise MCPMetadataValidationError("MCP description exceeds maximum size")
    schema = copy.deepcopy(raw_schema if isinstance(raw_schema, dict) else raw_schema)
    canonical_schema = sanitize_mcp_schema(schema)
    fingerprint_payload = {
        "server_name": str(server_name or ""),
        "server_url": str(server_url or ""),
        "tool_name": normalized_tool_name,
        "description": description,
        "schema": schema,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    return MCPMetadataCandidate(
        raw_description=description,
        raw_schema=copy.deepcopy(schema),
        canonical_description=_neutral_description(tool_name=normalized_tool_name, server_url=server_url),
        canonical_schema=canonical_schema,
        fingerprint=fingerprint,
        risk_flags=_risk_flags(description, schema),
    )


def quarantine_invalid_mcp_metadata(
    tool: Any,
    *,
    server_name: str,
    server_url: str,
    tool_name: str,
    raw_description: Any,
    raw_schema: Any,
    validation_error: str,
) -> None:
    """Revoke runtime access when refreshed metadata cannot pass validation.

    Invalid metadata must also invalidate a previous administrator approval.
    Bounded raw evidence remains available to the administrator, while the
    model-facing surface is replaced with a neutral description and empty
    object schema.
    """

    description = str(raw_description or "")
    schema_copy = (
        copy.deepcopy(raw_schema) if isinstance(raw_schema, dict) else {"_invalid_type": type(raw_schema).__name__}
    )
    encoded_schema = _canonical_json(schema_copy).encode("utf-8")
    stored_schema: dict[str, Any]
    if len(encoded_schema) <= MAX_RAW_SCHEMA_BYTES:
        stored_schema = schema_copy
    else:
        stored_schema = {
            "_quarantined": "schema_too_large",
            "sha256": hashlib.sha256(encoded_schema).hexdigest(),
            "bytes": len(encoded_schema),
        }
    fingerprint_payload = {
        "server_name": str(server_name or ""),
        "server_url": str(server_url or ""),
        "tool_name": str(tool_name or ""),
        "description": description,
        "schema": schema_copy,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    flags = list(_risk_flags(description, schema_copy))
    flags.extend(["invalid_metadata", f"validation:{validation_error}"])

    tool.mcp_raw_description = description[:MAX_RAW_DESCRIPTION_CHARS]
    tool.mcp_raw_schema = stored_schema
    tool.mcp_metadata_fingerprint = fingerprint
    tool.mcp_metadata_risk_flags = list(dict.fromkeys(flags))
    tool.description = _neutral_description(tool_name=str(tool_name or "unknown"), server_url=server_url)
    tool.parameters_schema = {"type": "object", "properties": {}}
    tool.mcp_trust_status = "invalid_metadata"
    tool.mcp_trust_tier = "external_invalid"
    tool.mcp_reviewed_fingerprint = None
    tool.mcp_reviewed_by = None
    tool.mcp_reviewed_at = None
    tool.enabled = False


def apply_mcp_metadata_candidate(tool: Any, candidate: MCPMetadataCandidate) -> bool:
    current_fingerprint = str(getattr(tool, "mcp_metadata_fingerprint", None) or "")
    current_status = str(getattr(tool, "mcp_trust_status", None) or "")
    changed = current_fingerprint != candidate.fingerprint or current_status not in {
        "pending_review",
        "approved",
        "rejected",
    }
    tool.mcp_raw_description = candidate.raw_description
    tool.mcp_raw_schema = copy.deepcopy(candidate.raw_schema)
    tool.mcp_metadata_fingerprint = candidate.fingerprint
    tool.mcp_metadata_risk_flags = list(candidate.risk_flags)
    tool.description = candidate.canonical_description
    tool.parameters_schema = copy.deepcopy(candidate.canonical_schema)
    if changed:
        tool.mcp_trust_status = candidate.trust_status
        tool.mcp_trust_tier = candidate.trust_tier
        tool.mcp_reviewed_fingerprint = None
        tool.mcp_reviewed_by = None
        tool.mcp_reviewed_at = None
        tool.enabled = False
    return changed


def _validate_canonical_description(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > MAX_CANONICAL_DESCRIPTION_CHARS:
        raise MCPMetadataValidationError("Canonical MCP description must be 1-500 characters")
    if _risk_flags(normalized, {}):
        raise MCPMetadataValidationError("unsafe canonical description")
    return normalized


def review_mcp_metadata(
    tool: Any,
    *,
    decision: str,
    reviewer_id: Any,
    expected_fingerprint: str,
    canonical_description: str | None = None,
    now: datetime | None = None,
) -> None:
    fingerprint = str(getattr(tool, "mcp_metadata_fingerprint", None) or "")
    if not fingerprint or fingerprint != str(expected_fingerprint or ""):
        raise MCPMetadataValidationError("MCP metadata fingerprint changed; reload before reviewing")
    if decision not in {"approve", "reject"}:
        raise MCPMetadataValidationError("MCP metadata decision must be approve or reject")
    approved_description = None
    approved_tier = None
    if decision == "approve":
        if canonical_description is not None and canonical_description.strip():
            approved_description = _validate_canonical_description(canonical_description)
            approved_tier = "admin_approved"
        else:
            approved_description = _validate_canonical_description(str(getattr(tool, "description", "")))
            approved_tier = "platform_generated_reviewed"
    reviewed_at = now or datetime.now(timezone.utc)
    tool.mcp_reviewed_by = reviewer_id
    tool.mcp_reviewed_at = reviewed_at
    tool.mcp_reviewed_fingerprint = fingerprint
    if decision == "reject":
        tool.mcp_trust_status = "rejected"
        tool.mcp_trust_tier = "admin_rejected"
        tool.enabled = False
        return
    tool.description = approved_description
    tool.mcp_trust_tier = approved_tier
    tool.mcp_trust_status = "approved"
    tool.enabled = True


def is_mcp_metadata_runtime_approved(tool: Any) -> bool:
    if getattr(tool, "type", None) != "mcp":
        return True
    fingerprint = str(getattr(tool, "mcp_metadata_fingerprint", None) or "")
    return bool(
        getattr(tool, "enabled", False)
        and getattr(tool, "mcp_trust_status", None) == "approved"
        and fingerprint
        and fingerprint == str(getattr(tool, "mcp_reviewed_fingerprint", None) or "")
    )
