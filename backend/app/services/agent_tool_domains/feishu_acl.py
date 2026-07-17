"""Shared Feishu connector authority helpers for successful read results."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from app.services.connector_acl import (
    authoritative_connector_source_item,
    extract_connector_source_items,
    with_connector_source_items,
)

FeishuSource = tuple[str, str]


def with_feishu_read_authority(
    result: Any,
    *,
    sources: Iterable[FeishuSource],
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
) -> Any:
    """Attach exact runtime authority to a successful Feishu read result."""

    rendered = str(result)
    source_items = [
        authoritative_connector_source_item(
            source=source,
            connector="feishu",
            resource_type=resource_type,
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
            protected_text=rendered,
        )
        for source, resource_type in sources
        if str(source or "").strip()
    ]
    return with_connector_source_items(result, source_items)


def with_feishu_alias_for_verified_result(
    result: Any,
    *,
    source: str,
    resource_type: str,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
) -> Any:
    """Authorize a URL alias only when the routed connector read proved access."""

    existing = extract_connector_source_items(result)
    if not any(
        item.get("metadata", {}).get("acl_authority") == "connector_verified"
        and item.get("metadata", {}).get("connector") == "feishu"
        for item in existing
        if isinstance(item, dict)
    ):
        return result
    return with_feishu_read_authority(
        result,
        sources=[(source, resource_type)],
        agent_id=agent_id,
        tenant_id=tenant_id,
        current_user_id=current_user_id,
    )


def preserve_feishu_fallback_authority(rendered_fallback: Any, fallback_result: Any) -> Any:
    """Carry successful fallback source receipts through a rendered fallback envelope."""

    return with_connector_source_items(
        rendered_fallback,
        extract_connector_source_items(fallback_result),
    )
