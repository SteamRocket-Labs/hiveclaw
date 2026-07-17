"""Connector ACL mirror helpers.

Connectors may enforce permissions remotely, but prompt injection must also
fail closed inside Hive when a result carries explicit ACL metadata. This module
implements the local mirror used before connector content enters the model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import uuid
from typing import Any

from app.runtime.decision_ledger import build_authorization_decision_entry

CONNECTOR_SOURCE_ITEMS_METADATA_KEY = "connector_source_items"
GENERATED_SOURCE_PERMISSION_CHECKS_METADATA_KEY = "generated_source_permission_checks"
_MAX_REGISTERED_SOURCE_ITEMS = 200

_GOVERNED_SOURCE_PREFIXES = (
    "feishu://",
    "drive://",
    "google-drive://",
    "office://",
    "onlyoffice://",
    "slack://",
    "gmail://",
    "email://",
    "openviking://",
    "viking://",
)
_GOVERNED_SOURCE_RE = re.compile(
    r"\b(?:feishu|drive|google-drive|office|onlyoffice|slack|gmail|email|openviking|viking)://[^\s\]\)\"'<>]+",
    re.IGNORECASE,
)
_ARG_SOURCE_DENY_ACL = {"deny_by_default": True}
_VERIFIED_ACL_METADATA_VALUE = "connector_verified"
_UNVERIFIED_ACL_METADATA_VALUE = "connector_unverified"
_PROTECTED_SHINGLE_SIZE = 5
_MAX_PROTECTED_SIGNATURES = 64
_PROTECTED_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class GeneratedSourcePermissionCheck:
    allowed: bool
    allowed_sources: list[str]
    forbidden_sources: list[str]
    authorization_decision_entry: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorPromptFilterResult:
    payload: Any
    allowed_sources: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()


def _string(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value or "").strip()


def _string_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {item.strip() for item in values.split(",") if item.strip()}
    if isinstance(values, (list, tuple, set)):
        return {_string(item) for item in values if _string(item)}
    return {_string(values)} if _string(values) else set()


def _acl_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("acl", "access", "permissions", "visibility"):
        payload = item.get(key)
        if isinstance(payload, dict):
            return payload
    return None


def _source_id(item: dict[str, Any]) -> str:
    for key in ("source", "source_uri", "uri", "url", "id"):
        value = _string(item.get(key))
        if value:
            return value
    return ""


def _canonical_source_item(item: dict[str, Any], *, origin: str | None = None) -> dict[str, Any] | None:
    source = _source_id(item)
    acl = _acl_payload(item)
    if not source and acl is None:
        return None
    canonical: dict[str, Any] = {"source": source}
    for key in ("source_uri", "uri", "url", "id", "path"):
        value = _string(item.get(key))
        if value:
            canonical[key] = value
    for key in ("acl", "access", "permissions", "visibility"):
        payload = item.get(key)
        if isinstance(payload, dict):
            canonical[key] = dict(payload)
            break
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        canonical["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "acl_authority",
                "accessing_agent_id",
                "source_type",
                "connector",
                "tenant_id",
                "tenant_ids",
                "account_id",
                "account_ids",
                "resource_type",
            }
        }
    content_digest = _string(item.get("content_digest"))
    if content_digest:
        canonical["content_digest"] = content_digest
    snippet_signatures = _string_set(item.get("protected_snippet_signatures"))
    if snippet_signatures:
        canonical["protected_snippet_signatures"] = sorted(snippet_signatures)
    if origin:
        canonical["origin"] = origin
    return canonical


def _requires_acl_metadata(item: dict[str, Any]) -> bool:
    source = _source_id(item).lower()
    return any(source.startswith(prefix) for prefix in _GOVERNED_SOURCE_PREFIXES)


def _authoritative_acl_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    acl = _acl_payload(item)
    if not isinstance(acl, dict) or acl.get("deny_by_default") is True:
        return None
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata.get("acl_authority") == _UNVERIFIED_ACL_METADATA_VALUE:
        return None
    if not _has_authoritative_acl_subject(acl):
        return None
    return acl


def _list_strings(values: Any) -> list[str]:
    return sorted(_string_set(values))


def _extend_acl_list(acl: dict[str, Any], key: str, values: Any) -> None:
    normalized = _list_strings(values)
    if normalized:
        acl[key] = normalized


def _has_authoritative_acl_subject(acl: dict[str, Any]) -> bool:
    if bool(acl.get("public")):
        return True
    if str(acl.get("scope") or "").lower() in {"public", "tenant", "company"}:
        return True
    for key in (
        "principal_ids",
        "principals",
        "user_ids",
        "users",
        "group_ids",
        "groups",
        "department_ids",
        "departments",
    ):
        if _string_set(acl.get(key)):
            return True
    return False


def _normalized_words(text: str) -> list[str]:
    return [match.group(0).lower() for match in _PROTECTED_WORD_RE.finditer(text or "")]


def _signature(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _protected_snippet_signatures(text: str) -> list[str]:
    words = _normalized_words(text)
    if not words:
        return []
    values: list[str] = []
    if len(words) < _PROTECTED_SHINGLE_SIZE:
        normalized = " ".join(words)
        if len(normalized) >= 16:
            values.append(_signature(normalized))
        return values
    seen: set[str] = set()
    for index in range(0, len(words) - _PROTECTED_SHINGLE_SIZE + 1):
        shingle = " ".join(words[index : index + _PROTECTED_SHINGLE_SIZE])
        digest = _signature(shingle)
        if digest in seen:
            continue
        seen.add(digest)
        values.append(digest)
        if len(values) >= _MAX_PROTECTED_SIGNATURES:
            break
    return values


def _content_digest(text: str) -> str | None:
    words = _normalized_words(text)
    if not words:
        return None
    return _signature(" ".join(words))


def _rendered_signature_set(text: str) -> set[str]:
    return set(_protected_snippet_signatures(text))


def authoritative_connector_source_item(
    *,
    source: str,
    connector: str,
    resource_type: str,
    tenant_id: uuid.UUID | str | None = None,
    tenant_ids: list[uuid.UUID | str] | tuple[uuid.UUID | str, ...] | set[uuid.UUID | str] | None = None,
    current_user_id: uuid.UUID | str | None = None,
    user_ids: list[uuid.UUID | str] | tuple[uuid.UUID | str, ...] | set[uuid.UUID | str] | None = None,
    department_ids: list[uuid.UUID | str] | tuple[uuid.UUID | str, ...] | set[uuid.UUID | str] | None = None,
    group_ids: list[uuid.UUID | str] | tuple[uuid.UUID | str, ...] | set[uuid.UUID | str] | None = None,
    agent_id: uuid.UUID | str | None = None,
    scope: str | None = None,
    protected_text: str | None = None,
) -> dict[str, Any]:
    """Build source ACL metadata for a connector result successfully read by Hive."""

    acl: dict[str, Any] = {}
    tenant_values = set(_list_strings(tenant_ids))
    if tenant_id:
        tenant_values.add(_string(tenant_id))
    _extend_acl_list(acl, "tenant_ids", tenant_values)
    user_values = set(_list_strings(user_ids))
    if current_user_id:
        user_values.add(_string(current_user_id))
    _extend_acl_list(acl, "user_ids", user_values)
    _extend_acl_list(acl, "department_ids", department_ids)
    _extend_acl_list(acl, "group_ids", group_ids)
    if scope:
        acl["scope"] = _string(scope)

    verified = _has_authoritative_acl_subject(acl)
    if not verified:
        acl = dict(_ARG_SOURCE_DENY_ACL)
    metadata: dict[str, Any] = {
        "connector": connector,
        "resource_type": resource_type,
        "acl_authority": _VERIFIED_ACL_METADATA_VALUE if verified else _UNVERIFIED_ACL_METADATA_VALUE,
    }
    if agent_id:
        metadata["accessing_agent_id"] = _string(agent_id)
    item: dict[str, Any] = {
        "source": _string(source),
        "acl": acl,
        "metadata": metadata,
    }
    if protected_text:
        digest = _content_digest(protected_text)
        if digest:
            item["content_digest"] = digest
        signatures = _protected_snippet_signatures(protected_text)
        if signatures:
            item["protected_snippet_signatures"] = signatures
    return item


def with_connector_source_items(text: Any, source_items: list[dict[str, Any]]) -> Any:
    """Attach connector source ACL metadata while preserving the text result."""

    if not source_items:
        return text
    from app.tools.result_envelope import ToolContentEnvelope

    if isinstance(text, ToolContentEnvelope):
        metadata = dict(text.metadata or {})
        existing = metadata.get(CONNECTOR_SOURCE_ITEMS_METADATA_KEY)
        merged: list[dict[str, Any]] = []
        source_indexes: dict[str, int] = {}
        for item in [*(existing if isinstance(existing, list) else []), *source_items]:
            if not isinstance(item, dict):
                continue
            source = _source_id(item)
            key = source.lower()
            if not key:
                continue
            index = source_indexes.get(key)
            if index is None:
                source_indexes[key] = len(merged)
                merged.append(item)
                continue
            if _authoritative_acl_payload(merged[index]) is None and _authoritative_acl_payload(item) is not None:
                merged[index] = item
        metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY] = merged
        return replace(text, metadata=metadata)

    return ToolContentEnvelope(text=str(text), metadata={CONNECTOR_SOURCE_ITEMS_METADATA_KEY: source_items})


def extract_connector_source_items(
    payload: Any, *, origin: str | None = None, max_items: int = 50
) -> list[dict[str, Any]]:
    """Extract governed connector source descriptors from structured or text payloads.

    The returned items intentionally omit large content fields. They carry only
    source identifiers and ACL metadata needed for prompt-entry and generated-output
    permission checks.
    """

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        if len(items) >= max_items:
            return
        canonical = _canonical_source_item(item, origin=origin)
        if canonical is None:
            return
        source = _source_id(canonical)
        if not source:
            return
        # Only governed connector sources are tracked here; legacy internal
        # memory stays on the existing memory visibility path.
        if not _requires_acl_metadata(canonical) and _acl_payload(canonical) is None:
            return
        key = source.lower()
        if key in seen:
            return
        seen.add(key)
        items.append(canonical)

    def visit(value: Any, depth: int = 0) -> None:
        if len(items) >= max_items or depth > 5:
            return
        metadata = getattr(value, "metadata", None)
        if isinstance(metadata, dict):
            visit(metadata, depth + 1)
            return
        if isinstance(value, dict):
            add(value)
            for child_key, child_value in value.items():
                if child_key in {"content", "text", "body", "markdown"} and not isinstance(
                    child_value, (dict, list, tuple)
                ):
                    continue
                visit(child_value, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, depth + 1)
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    visit(json.loads(stripped), depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            for match in _GOVERNED_SOURCE_RE.findall(value):
                add({"source": match})

    visit(payload)
    return items


def register_connector_source_items(
    session_context: Any | None,
    source_items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    origin: str | None = None,
) -> int:
    """Register connector source descriptors on a SessionContext for final checks."""

    if session_context is None or not source_items:
        return 0
    metadata = getattr(session_context, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session_context.metadata = metadata

    existing = metadata.setdefault(CONNECTOR_SOURCE_ITEMS_METADATA_KEY, [])
    if not isinstance(existing, list):
        existing = []
        metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY] = existing
    seen = {_source_id(item).lower() for item in existing if isinstance(item, dict) and _source_id(item)}
    index_by_source = {
        _source_id(item).lower(): index
        for index, item in enumerate(existing)
        if isinstance(item, dict) and _source_id(item)
    }
    added = 0
    for item in source_items:
        if not isinstance(item, dict):
            continue
        canonical = _canonical_source_item(item, origin=origin)
        if canonical is None:
            continue
        source = _source_id(canonical)
        if not source:
            continue
        key = source.lower()
        if key in seen:
            existing_index = index_by_source.get(key)
            existing_item = existing[existing_index] if existing_index is not None else None
            if (
                isinstance(existing_item, dict)
                and _authoritative_acl_payload(canonical) is not None
                and _authoritative_acl_payload(existing_item) is None
            ):
                existing[existing_index] = canonical
            continue
        existing.append(canonical)
        seen.add(key)
        index_by_source[key] = len(existing) - 1
        added += 1
    if len(existing) > _MAX_REGISTERED_SOURCE_ITEMS:
        del existing[:-_MAX_REGISTERED_SOURCE_ITEMS]
    return added


def register_connector_source_payload(session_context: Any | None, payload: Any, *, origin: str | None = None) -> int:
    return register_connector_source_items(
        session_context,
        extract_connector_source_items(payload, origin=origin),
        origin=origin,
    )


def source_items_from_tool_call(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Derive governed connector sources from tool call arguments.

    Tool handlers do not all return structured source metadata yet. Argument-
    derived sources therefore fail closed (`deny_by_default`) until a real
    result/connector payload supplies authoritative ACL metadata for the same
    source.
    """

    if not isinstance(arguments, dict):
        return []
    normalized_tool = str(tool_name or "").strip().lower()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(source: str, *, connector: str, resource_type: str) -> None:
        value = _string(source)
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        items.append(
            {
                "source": value,
                "acl": dict(_ARG_SOURCE_DENY_ACL),
                "metadata": {"connector": connector, "resource_type": resource_type},
                "origin": origin or f"tool_args:{normalized_tool}",
            }
        )

    def first_arg(*keys: str) -> str:
        for key in keys:
            value = _string(arguments.get(key))
            if value:
                return value
        return ""

    def feishu_path_token(value: str, *kinds: str) -> str:
        if not value or not kinds:
            return value
        kind_pattern = "|".join(re.escape(kind) for kind in kinds)
        match = re.search(rf"/(?:{kind_pattern})/([^/?#]+)", value)
        return match.group(1) if match else value

    if normalized_tool.startswith("feishu_"):
        raw_url = first_arg("url", "spreadsheet_url", "document_url")
        if raw_url:
            add(raw_url, connector="feishu", resource_type="url")

        if normalized_tool == "feishu_wiki_list":
            node_token = first_arg("node_token")
            space_id = first_arg("space_id")
            space_match = re.search(r"/wiki/space/([^/?#]+)", space_id or node_token)
            node_match = re.search(r"/wiki/(?!space/)([^/?#]+)", node_token)
            if space_match:
                space_id = space_match.group(1)
                node_token = ""
            elif node_match:
                node_token = node_match.group(1)
            if node_token:
                add(f"feishu://wiki/node/{node_token}", connector="feishu", resource_type="wiki_node")
            elif space_id:
                add(f"feishu://wiki/space/{space_id}", connector="feishu", resource_type="wiki_space")
        elif normalized_tool == "feishu_doc_read":
            token = feishu_path_token(
                first_arg("document_token", "doc_token", "doc_id", "token"),
                "wiki",
                "docx",
                "doc",
            )
            if token:
                add(f"feishu://doc/{token}", connector="feishu", resource_type="doc")
        elif normalized_tool == "feishu_drive_file_read":
            token = feishu_path_token(
                first_arg("file_token", "file_id", "token"),
                "file",
                "docx",
                "doc",
                "sheets",
                "spreadsheets",
                "base",
                "bitable",
            )
            if token:
                add(f"feishu://drive/{token}", connector="feishu", resource_type="drive_file")
        elif normalized_tool in {"feishu_sheet_info", "feishu_sheet_read"}:
            token = feishu_path_token(
                first_arg("spreadsheet_token", "spreadsheet_url", "token"),
                "sheets",
                "spreadsheets",
            )
            if token:
                add(f"feishu://sheet/{token}", connector="feishu", resource_type="sheet")
        elif normalized_tool in {
            "feishu_base_table_list",
            "feishu_base_record_list",
            "feishu_base_field_list",
        }:
            token = feishu_path_token(
                first_arg("base_token", "app_token", "token"),
                "base",
                "bitable",
            )
            table_id = first_arg("table_id")
            if token and table_id:
                add(f"feishu://base/{token}/{table_id}", connector="feishu", resource_type="base_table")
            if token:
                add(f"feishu://base/{token}", connector="feishu", resource_type="base")

    if normalized_tool.startswith("office_document_"):
        path = first_arg("path")
        if path:
            add(f"office://workspace/{path.lstrip('/')}", connector="office", resource_type="document")
        output_path = first_arg("output_path")
        if output_path:
            add(f"office://workspace/{output_path.lstrip('/')}", connector="office", resource_type="document")

    return items


def record_generated_source_permission_check(
    session_context: Any | None,
    check: GeneratedSourcePermissionCheck,
) -> None:
    if session_context is None:
        return
    metadata = getattr(session_context, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session_context.metadata = metadata
    checks = metadata.setdefault(GENERATED_SOURCE_PERMISSION_CHECKS_METADATA_KEY, [])
    if not isinstance(checks, list):
        checks = []
        metadata[GENERATED_SOURCE_PERMISSION_CHECKS_METADATA_KEY] = checks
    checks.append(
        {
            "allowed": check.allowed,
            "allowed_sources": list(check.allowed_sources),
            "forbidden_sources": list(check.forbidden_sources),
            "forbidden_source_count": len(check.forbidden_sources),
            "authorization_decision_entry": check.authorization_decision_entry,
        }
    )
    if len(checks) > 50:
        del checks[:-50]


def _principal_ids(
    *,
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None,
) -> set[str]:
    principals: set[str] = set()
    if tenant_id:
        value = _string(tenant_id)
        principals.update({value, f"tenant:{value}"})
    if current_user_id:
        value = _string(current_user_id)
        principals.update({value, f"user:{value}"})
    if agent_id:
        value = _string(agent_id)
        principals.update({value, f"agent:{value}"})
    return principals


def connector_item_visible(
    item: dict[str, Any],
    *,
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
) -> bool:
    """Return whether one connector result may enter prompt context.

    Internal legacy items without connector source metadata remain visible for
    compatibility. Governed connector items (Feishu/Drive/Office/etc.) require
    ACL metadata and then fail closed unless tenant and principal match.
    """

    acl = _acl_payload(item)
    if acl is None:
        return not _requires_acl_metadata(item)

    tenant = _string(tenant_id)
    if not tenant:
        return False

    allowed_tenants = _string_set(
        acl.get("tenant_ids") or acl.get("tenants") or acl.get("account_ids") or acl.get("accounts")
    )
    denied_tenants = _string_set(acl.get("deny_tenant_ids") or acl.get("denied_tenants"))
    if tenant in denied_tenants or f"tenant:{tenant}" in denied_tenants:
        return False
    if allowed_tenants and tenant not in allowed_tenants and f"tenant:{tenant}" not in allowed_tenants:
        return False

    scope = str(acl.get("scope") or "").lower()
    if bool(acl.get("public")) or scope == "public":
        return True
    if scope in {"tenant", "company"}:
        return bool(allowed_tenants)

    principals = _principal_ids(tenant_id=tenant_id, current_user_id=current_user_id, agent_id=agent_id)
    denied = _string_set(acl.get("deny_principal_ids") or acl.get("denied_principals"))
    if principals & denied:
        return False

    allowed = set()
    for key in (
        "principal_ids",
        "principals",
        "user_ids",
        "users",
        "group_ids",
        "groups",
        "department_ids",
        "departments",
    ):
        allowed |= _string_set(acl.get(key))

    if not allowed:
        return False
    return bool(principals & allowed)


def filter_connector_results_for_prompt(
    items: list[dict[str, Any]],
    *,
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and connector_item_visible(item, tenant_id=tenant_id, current_user_id=current_user_id, agent_id=agent_id)
    ]


def filter_connector_payload_for_prompt(
    payload: Any,
    *,
    source_items: list[dict[str, Any]],
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
) -> ConnectorPromptFilterResult:
    """Remove unauthorized connector payloads before they enter model context.

    Structured lists preserve authorized siblings. Opaque text/envelopes are
    replaced by a typed denial because their fragments cannot be attributed
    safely. The original connector remains the evidence authority.
    """

    allowed_sources: list[str] = []
    forbidden_sources: list[str] = []
    visible_by_source: dict[str, bool] = {}
    for item in source_items:
        if not isinstance(item, dict):
            continue
        source = _source_id(item)
        if not source:
            continue
        visible = connector_item_visible(
            item,
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
        )
        visible_by_source[source.lower()] = visible
        target = allowed_sources if visible else forbidden_sources
        if source not in target:
            target.append(source)

    if not forbidden_sources:
        return ConnectorPromptFilterResult(payload=payload, allowed_sources=tuple(allowed_sources))

    denied_marker = json.dumps(
        {
            "status": "source_permission_denied",
            "forbidden_source_count": len(forbidden_sources),
            "retryable": False,
        },
        sort_keys=True,
    )
    omitted = object()

    def visit(value: Any) -> Any:
        from app.tools.result_envelope import ToolContentEnvelope

        if isinstance(value, ToolContentEnvelope):
            return ToolContentEnvelope(
                text=denied_marker,
                metadata={
                    "source_permission": {
                        "status": "denied",
                        "forbidden_source_count": len(forbidden_sources),
                    }
                },
            )
        if isinstance(value, dict):
            source = _source_id(value)
            if source and visible_by_source.get(source.lower()) is False:
                return omitted
            filtered: dict[str, Any] = {}
            for key, child in value.items():
                filtered_child = visit(child)
                if filtered_child is not omitted:
                    filtered[key] = filtered_child
            return filtered
        if isinstance(value, list):
            return [child for item in value if (child := visit(item)) is not omitted]
        if isinstance(value, tuple):
            return tuple(child for item in value if (child := visit(item)) is not omitted)
        if isinstance(value, str):
            # Opaque connector text cannot prove which bytes belong to which
            # source. Do not let it cross the prompt ingress boundary.
            return denied_marker
        return value

    filtered = visit(payload)
    if filtered is omitted or filtered in ({}, [], ()):
        filtered = denied_marker
    return ConnectorPromptFilterResult(
        payload=filtered,
        allowed_sources=tuple(allowed_sources),
        forbidden_sources=tuple(forbidden_sources),
    )


def validate_generated_source_permissions(
    text: str,
    *,
    source_items: list[dict[str, Any]],
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
) -> GeneratedSourcePermissionCheck:
    """Check that generated text does not cite or reveal forbidden connector sources."""

    rendered = str(text or "")
    rendered_signatures = _rendered_signature_set(rendered)
    allowed_sources: list[str] = []
    forbidden_sources: list[str] = []
    for item in source_items:
        if not isinstance(item, dict):
            continue
        source = _source_id(item)
        if not source:
            continue
        source_mentioned = source in rendered
        snippet_signatures = _string_set(item.get("protected_snippet_signatures"))
        protected_snippet_mentioned = bool(snippet_signatures and rendered_signatures & snippet_signatures)
        if not source_mentioned and not protected_snippet_mentioned:
            continue
        if connector_item_visible(item, tenant_id=tenant_id, current_user_id=current_user_id, agent_id=agent_id):
            if source not in allowed_sources:
                allowed_sources.append(source)
        elif source not in forbidden_sources:
            forbidden_sources.append(source)
    resource = (
        forbidden_sources[0] if forbidden_sources else (allowed_sources[0] if allowed_sources else "generated_source")
    )
    authorization_decision_entry = build_authorization_decision_entry(
        resource=resource,
        action="render_generated_connector_source",
        principal=current_user_id,
        company=tenant_id,
        policy="generated_source_acl",
        result="allowed" if not forbidden_sources else "blocked",
        reason="allowed_connector_source" if not forbidden_sources else "forbidden_connector_source",
        model_visible_message=(
            "Generated response referenced only accessible governed connector sources."
            if not forbidden_sources
            else "Generated response referenced an inaccessible governed connector source."
        ),
        source="generated_source_acl",
    )
    return GeneratedSourcePermissionCheck(
        allowed=not forbidden_sources,
        allowed_sources=allowed_sources,
        forbidden_sources=forbidden_sources,
        authorization_decision_entry=authorization_decision_entry,
    )


def redact_forbidden_generated_source_fragments(
    text: str,
    *,
    source_items: list[dict[str, Any]],
    tenant_id: uuid.UUID | str | None,
    current_user_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None = None,
) -> tuple[str, GeneratedSourcePermissionCheck]:
    """Apply an exact final failsafe while preserving unrelated model prose."""

    check = validate_generated_source_permissions(
        text,
        source_items=source_items,
        tenant_id=tenant_id,
        current_user_id=current_user_id,
        agent_id=agent_id,
    )
    if check.allowed:
        return text, check

    forbidden = set(check.forbidden_sources)
    redacted = str(text or "")
    for source in forbidden:
        redacted = redacted.replace(source, "[REDACTED_FORBIDDEN_SOURCE]")

    protected_signatures: set[str] = set()
    for item in source_items:
        if isinstance(item, dict) and _source_id(item) in forbidden:
            protected_signatures.update(_string_set(item.get("protected_snippet_signatures")))
    if protected_signatures:
        lines: list[str] = []
        for line in redacted.splitlines(keepends=True):
            if _rendered_signature_set(line) & protected_signatures:
                newline = "\n" if line.endswith("\n") else ""
                lines.append("[REDACTED_FORBIDDEN_SOURCE_FRAGMENT]" + newline)
            else:
                lines.append(line)
        redacted = "".join(lines)
    if not redacted.strip():
        redacted = "[REDACTED_FORBIDDEN_SOURCE_FRAGMENT]"
    return redacted, check
