"""Connector ACL mirror helpers.

Connectors may enforce permissions remotely, but prompt injection must also
fail closed inside Hive when a result carries explicit ACL metadata. This module
implements the local mirror used before connector content enters the model.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

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
)


@dataclass(frozen=True, slots=True)
class GeneratedSourcePermissionCheck:
    allowed: bool
    allowed_sources: list[str]
    forbidden_sources: list[str]


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


def _requires_acl_metadata(item: dict[str, Any]) -> bool:
    source = _source_id(item).lower()
    return any(source.startswith(prefix) for prefix in _GOVERNED_SOURCE_PREFIXES)


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

    if bool(acl.get("public")) or str(acl.get("scope") or "").lower() in {"public", "tenant", "company"}:
        return True

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
        "agent_ids",
        "agents",
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
    allowed_sources: list[str] = []
    forbidden_sources: list[str] = []
    for item in source_items:
        if not isinstance(item, dict):
            continue
        source = _source_id(item)
        if not source or source not in rendered:
            continue
        if connector_item_visible(item, tenant_id=tenant_id, current_user_id=current_user_id, agent_id=agent_id):
            if source not in allowed_sources:
                allowed_sources.append(source)
        elif source not in forbidden_sources:
            forbidden_sources.append(source)
    return GeneratedSourcePermissionCheck(
        allowed=not forbidden_sources,
        allowed_sources=allowed_sources,
        forbidden_sources=forbidden_sources,
    )
