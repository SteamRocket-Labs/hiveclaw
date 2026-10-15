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
from urllib.parse import parse_qsl, urlsplit, urlunsplit


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
    server_name: str = ""
    server_url: str = ""
    tool_name: str = ""
    trust_status: str = "pending_review"
    trust_tier: str = "external_unreviewed"
    legacy_fingerprint: str = ""
    activation: dict[str, Any] | None = None


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


_ACTIVATION_FIELDS = ("transport", "headers", "command", "args", "scopes")


def mcp_activation_binding(
    *,
    package: str | None = None,
    configuration: dict[str, Any] | None = None,
    permissions: Any = None,
) -> dict[str, Any]:
    """Exact activation facts. Empty fields are omitted so a legacy fingerprint stays valid."""

    activation: dict[str, Any] = {}
    if package:
        activation["package"] = str(package)
    config_source = configuration if isinstance(configuration, dict) else {}
    configuration_binding: dict[str, Any] = {}
    for key in _ACTIVATION_FIELDS:
        value = config_source.get(key)
        if value not in (None, "", [], {}):
            configuration_binding[key] = value
    secret = config_source.get("api_key") or config_source.get("credential")
    if secret:
        configuration_binding["credential_sha256"] = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
    elif config_source.get("credential_sha256"):
        configuration_binding["credential_sha256"] = str(config_source["credential_sha256"])
    if configuration_binding:
        activation["configuration"] = configuration_binding
    if isinstance(permissions, (list, tuple)) and permissions:
        activation["permissions"] = sorted({str(item) for item in permissions})
    return activation


def _legacy_fingerprint_payload(
    server_name: str,
    server_url: str,
    tool_name: str,
    description: str,
    schema: Any,
) -> dict[str, Any]:
    return {
        "server_name": str(server_name or ""),
        "server_url": str(server_url or ""),
        "tool_name": str(tool_name or ""),
        "description": description,
        "schema": schema,
    }


def _fingerprint_for(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def prepare_mcp_metadata_candidate(
    *,
    server_name: str,
    server_url: str,
    tool_name: str,
    raw_description: Any,
    raw_schema: Any,
    package: str | None = None,
    configuration: dict[str, Any] | None = None,
    permissions: Any = None,
) -> MCPMetadataCandidate:
    normalized_tool_name = str(tool_name or "").strip()
    if not _TOOL_NAME_RE.fullmatch(normalized_tool_name):
        raise MCPMetadataValidationError("Unsafe MCP tool name")
    description = str(raw_description or "").strip()
    if len(description) > MAX_RAW_DESCRIPTION_CHARS:
        raise MCPMetadataValidationError("MCP description exceeds maximum size")
    schema = copy.deepcopy(raw_schema if isinstance(raw_schema, dict) else raw_schema)
    canonical_schema = sanitize_mcp_schema(schema)
    legacy_payload = _legacy_fingerprint_payload(server_name, server_url, normalized_tool_name, description, schema)
    activation = mcp_activation_binding(package=package, configuration=configuration, permissions=permissions)
    payload = dict(legacy_payload)
    if activation:
        payload["activation"] = activation
    return MCPMetadataCandidate(
        raw_description=description,
        raw_schema=copy.deepcopy(schema),
        canonical_description=_neutral_description(tool_name=normalized_tool_name, server_url=server_url),
        canonical_schema=canonical_schema,
        fingerprint=_fingerprint_for(payload),
        risk_flags=_risk_flags(description, schema),
        server_name=str(server_name or ""),
        server_url=str(server_url or ""),
        tool_name=normalized_tool_name,
        legacy_fingerprint=_fingerprint_for(legacy_payload),
        activation=activation or None,
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


def _config_with_activation(tool: Any, activation: dict[str, Any] | None) -> dict[str, Any]:
    current = getattr(tool, "config", None)
    config = dict(current) if isinstance(current, dict) else {}
    if not activation:
        config.pop("mcp_activation", None)
        return config
    config["mcp_activation"] = copy.deepcopy(activation)
    if activation.get("package"):
        config["package"] = activation["package"]
    configuration = activation.get("configuration") if isinstance(activation.get("configuration"), dict) else {}
    for key in _ACTIVATION_FIELDS:
        if key in configuration:
            config[key] = copy.deepcopy(configuration[key])
    if activation.get("permissions"):
        config["permissions"] = list(activation["permissions"])
    return config


def apply_mcp_metadata_candidate(tool: Any, candidate: MCPMetadataCandidate) -> bool:
    current_fingerprint = str(getattr(tool, "mcp_metadata_fingerprint", None) or "")
    current_status = str(getattr(tool, "mcp_trust_status", None) or "")
    changed = current_fingerprint != candidate.fingerprint or current_status not in {
        "pending_review",
        "approved",
        "rejected",
    }
    tool.mcp_server_name = candidate.server_name
    tool.mcp_server_url = candidate.server_url
    tool.mcp_tool_name = candidate.tool_name
    tool.mcp_raw_description = candidate.raw_description
    tool.mcp_raw_schema = copy.deepcopy(candidate.raw_schema)
    tool.mcp_metadata_risk_flags = list(candidate.risk_flags)
    tool.description = candidate.canonical_description
    tool.parameters_schema = copy.deepcopy(candidate.canonical_schema)
    tool.config = _config_with_activation(tool, candidate.activation)
    tool.mcp_metadata_fingerprint = candidate.fingerprint
    if changed:
        tool.mcp_trust_status = candidate.trust_status
        tool.mcp_trust_tier = candidate.trust_tier
        tool.mcp_reviewed_fingerprint = None
        tool.mcp_reviewed_by = None
        tool.mcp_reviewed_at = None
        tool.enabled = False
    return changed


def live_mcp_activation(tool: Any) -> dict[str, Any]:
    config = getattr(tool, "config", None)
    config = config if isinstance(config, dict) else {}
    permissions = config.get("permissions")
    return mcp_activation_binding(
        package=config.get("package") or config.get("qualified_name"),
        configuration=config,
        permissions=permissions,
    )


def _live_legacy_fingerprint(tool: Any) -> str:
    description = str(getattr(tool, "mcp_raw_description", None) or "").strip()
    schema = getattr(tool, "mcp_raw_schema", None)
    if schema is None:
        schema = {}
    return _fingerprint_for(
        _legacy_fingerprint_payload(
            str(getattr(tool, "mcp_server_name", None) or ""),
            str(getattr(tool, "mcp_server_url", None) or ""),
            str(getattr(tool, "mcp_tool_name", None) or "").strip(),
            description,
            schema,
        )
    )


def current_mcp_fingerprint(tool: Any) -> str:
    """Fingerprint of the metadata and activation facts currently on the tool."""

    payload = _legacy_fingerprint_payload(
        str(getattr(tool, "mcp_server_name", None) or ""),
        str(getattr(tool, "mcp_server_url", None) or ""),
        str(getattr(tool, "mcp_tool_name", None) or "").strip(),
        str(getattr(tool, "mcp_raw_description", None) or "").strip(),
        getattr(tool, "mcp_raw_schema", None) or {},
    )
    activation = live_mcp_activation(tool)
    if activation:
        payload["activation"] = activation
    return _fingerprint_for(payload)


_SAFE_TRANSPORTS = frozenset({"http", "https", "sse", "streamable", "streamable_http", "http_sse", "stdio"})
_SAFE_QUERY_NAMES = frozenset({"mode", "transport"})
_SAFE_HEADER_NAMES = frozenset({"accept", "accept-language", "content-type", "user-agent"})
_SAFE_HEADER_VALUES = frozenset({"*/*", "application/json", "application/xml", "text/event-stream", "text/plain"})
_FLAG_RE = re.compile(r"--?[A-Za-z][A-Za-z0-9-]{0,40}")
_SCOPE_RE = re.compile(r"[A-Za-z*][A-Za-z0-9_.*:-]{0,24}")
_COMMAND_RE = re.compile(r"[A-Za-z0-9_./+-]{1,128}")
_PACKAGE_RE = re.compile(r"@?[A-Za-z0-9_][A-Za-z0-9_./+-]{0,127}")


def _public_endpoint(url: str) -> tuple[str, bool]:
    """Scheme, host, and path, plus known transport query pairs. Nothing else."""

    raw = str(url or "").strip()
    if not raw:
        return "", False
    parts = urlsplit(raw)
    credential = bool(parts.username or parts.password or parts.fragment)
    kept: list[str] = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        normalized = value.strip().lower().replace("-", "_")
        if name.strip().lower() in _SAFE_QUERY_NAMES and normalized in _SAFE_TRANSPORTS:
            kept.append(f"{name.strip().lower()}={normalized}")
        else:
            credential = True
    host = parts.hostname or ""
    if not parts.scheme or not host:
        return "", bool(credential or "://" in raw or "@" in raw)
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "&".join(kept), "")), credential


def _public_args(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if not isinstance(value, list):
        return "set"
    return [item if isinstance(item, str) and _FLAG_RE.fullmatch(item) else "set" for item in value]


def _public_headers(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if not isinstance(value, dict):
        return "set"
    shown: dict[str, str] = {}
    for key, item in value.items():
        name = str(key)
        if name.lower() in _SAFE_HEADER_NAMES and isinstance(item, str) and item.lower() in _SAFE_HEADER_VALUES:
            shown[name] = item
        else:
            shown[name] = "set"
    return shown


def _public_scope(item: Any) -> str:
    if not isinstance(item, str) or not item:
        return "set"
    if "://" in item:
        cleaned, _has_credential = _public_endpoint(item)
        return cleaned or "set"
    return item if _SCOPE_RE.fullmatch(item) else "set"


def _public_scopes(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if not isinstance(value, list):
        return "set"
    return [_public_scope(item) for item in value]


def _public_transport(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("-", "_")
    return normalized if normalized in _SAFE_TRANSPORTS else "set"


def _public_command(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" in text:
        cleaned, _has_credential = _public_endpoint(text)
        return cleaned or "set"
    if _COMMAND_RE.fullmatch(text) and (len(text) <= 24 or "/" in text):
        return text
    return "set"


def _public_package(package: str) -> tuple[str, bool]:
    if "://" in package:
        shown, credential = _public_endpoint(package)
        return (shown or "set"), bool(credential or not shown)
    if _PACKAGE_RE.fullmatch(package) and (
        "." in package or "/" in package or package.startswith("@") or len(package) <= 24
    ):
        return package, False
    return "set", True


def _public_configuration(configuration: Any, *, credential: bool) -> dict[str, Any]:
    source = configuration if isinstance(configuration, dict) else {}
    shown: dict[str, Any] = {}
    transport = _public_transport(source.get("transport"))
    if transport:
        shown["transport"] = transport
    command = _public_command(source.get("command"))
    if command:
        shown["command"] = command
    args = _public_args(source.get("args"))
    if args is not None:
        shown["args"] = args
    scopes = _public_scopes(source.get("scopes"))
    if scopes is not None:
        shown["scopes"] = scopes
    headers = _public_headers(source.get("headers"))
    if headers is not None:
        shown["headers"] = headers
    if credential or source.get("credential_sha256"):
        shown["credential"] = "set"
    return shown


def public_mcp_review_facts(tool: Any) -> dict[str, Any]:
    """Return the current review candidate and the masked facts bound to it.

    ``fingerprint`` is the only digest the administrator can submit. It is not
    the historical ``reviewed_fingerprint``, which stays the last approval.
    """

    endpoint, endpoint_credential = _public_endpoint(str(getattr(tool, "mcp_server_url", None) or ""))
    activation = live_mcp_activation(tool)
    public: dict[str, Any] = {}
    if endpoint:
        public["endpoint"] = endpoint
    package = activation.get("package")
    if isinstance(package, str) and package:
        shown_package, package_credential = _public_package(package)
        endpoint_credential = endpoint_credential or package_credential
        if shown_package:
            public["package"] = shown_package
    configuration = _public_configuration(activation.get("configuration"), credential=endpoint_credential)
    if configuration:
        public["configuration"] = configuration
    permissions = activation.get("permissions")
    if isinstance(permissions, list) and permissions:
        public["permissions"] = [str(item) for item in permissions]
    reviewed = getattr(tool, "mcp_reviewed_fingerprint", None)
    return {
        "fingerprint": current_mcp_fingerprint(tool),
        "reviewed_fingerprint": str(reviewed) if reviewed else None,
        "activation": public,
    }


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
    live = current_mcp_fingerprint(tool)
    if not live or live != str(expected_fingerprint or ""):
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
    activation = live_mcp_activation(tool)
    tool.config = _config_with_activation(tool, activation or None)
    tool.mcp_metadata_fingerprint = live
    tool.mcp_reviewed_by = reviewer_id
    tool.mcp_reviewed_at = reviewed_at
    tool.mcp_reviewed_fingerprint = live
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
    stored = str(getattr(tool, "mcp_metadata_fingerprint", None) or "")
    reviewed = str(getattr(tool, "mcp_reviewed_fingerprint", None) or "")
    if not (
        getattr(tool, "enabled", False) and getattr(tool, "mcp_trust_status", None) == "approved" and stored == reviewed
    ):
        return False
    if not stored or stored != current_mcp_fingerprint(tool):
        return False
    config = getattr(tool, "config", None)
    sealed = config.get("mcp_activation") if isinstance(config, dict) else None
    activation = live_mcp_activation(tool)
    if not activation:
        return not isinstance(sealed, dict) or not sealed
    return isinstance(sealed, dict) and sealed == activation
