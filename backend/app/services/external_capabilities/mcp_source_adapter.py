from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from app.services.external_capabilities.trust_gate import stage_external_capability_review
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle
from app.services.mcp_authz import (
    API_KEY_QUERY_KEYS,
    assert_mcp_cloud_transport_allowed,
    assert_no_mcp_token_passthrough,
    split_mcp_server_url_and_api_key,
)

_SENSITIVE_CONFIG_KEYS = {"api_key", "apikey"}
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


async def stage_external_mcp_server_review(
    db,
    tenant_id: uuid.UUID,
    *,
    created_by_user_id: uuid.UUID | None,
    server_id: str | None,
    mcp_url: str | None,
    server_name: str | None,
    config: dict | None,
) -> dict[str, Any]:
    if not server_id and not mcp_url and not config:
        raise ValueError("server_id or mcp_url is required")

    sanitized_url, api_key_from_url = _sanitize_url(mcp_url)
    raw_config = dict(config or {})
    assert_no_mcp_token_passthrough(raw_config)
    assert_mcp_cloud_transport_allowed(sanitized_url, raw_config.get("transport"))
    sanitized_config, has_config_secret = _sanitize_config(raw_config)

    name = _server_name(server_name=server_name, server_id=server_id, mcp_url=sanitized_url)
    if sanitized_url:
        source_uri = sanitized_url
    elif server_id:
        source_uri = f"mcp_registry:{server_id}"
    else:
        source_uri = "mcp_config:inline"
    credential_requirements = []
    if api_key_from_url or has_config_secret:
        credential_requirements.append({"key": "api_key", "sensitive": True, "source": "mcp_config_or_url"})

    component = ExternalCapabilityComponent(
        component_type="mcp_server",
        local_name=name,
        qualified_name=f"mcp:{name}",
        source_path="mcp_server_config",
        content_sha256=_stable_digest(
            {
                "server_id": server_id,
                "mcp_url": sanitized_url,
                "server_name": server_name,
                "config": sanitized_config,
            }
        ),
        runtime_projection={
            "server_id": server_id,
            "server_name": server_name or name,
            "mcp_url": sanitized_url,
            "config": sanitized_config,
        },
    )
    bundle = NormalizedExternalPluginBundle(
        source_format="mcp_server",
        source_uri=source_uri,
        plugin_name=name,
        manifest_sha256=component.content_sha256,
        components=[component],
        credential_requirements=credential_requirements,
    )
    review = await stage_external_capability_review(
        db,
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        bundle=bundle,
    )
    return {
        "status": review["status"],
        "review_id": review["id"],
        "admission_class": review["admission_class"],
        "normalized_name": name,
        "source_uri": source_uri,
        "review": review,
    }


def _sanitize_url(mcp_url: str | None) -> tuple[str | None, str | None]:
    if not mcp_url:
        return None, None
    return split_mcp_server_url_and_api_key(mcp_url)


def _sanitize_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sanitized: dict[str, Any] = {}
    has_secret = False
    for key, value in config.items():
        normalized = str(key).strip().lower()
        if normalized in _SENSITIVE_CONFIG_KEYS or normalized in API_KEY_QUERY_KEYS:
            has_secret = True
            continue
        sanitized[str(key)] = value
    return sanitized, has_secret


def _server_name(*, server_name: str | None, server_id: str | None, mcp_url: str | None) -> str:
    raw = server_name or server_id or mcp_url or "mcp-server"
    slug = _SLUG_RE.sub("-", str(raw).strip().lower()).strip("-_")
    return slug[:80] or "mcp-server"


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
