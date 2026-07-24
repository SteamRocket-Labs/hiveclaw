"""Resolve exact protected credentials from tenant-scoped runtime authority."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.exact_secret_boundary import (
    ExactSecretBoundary,
    ExactSecretPayloadRedaction,
    boundary_from_channel_config,
    boundary_from_reply_target,
)


class RuntimeIngressSecretBoundaryUnavailable(RuntimeError):
    """The authoritative credential inventory could not be loaded."""


async def load_exact_secret_boundary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> ExactSecretBoundary:
    """Load exact secrets that the current Agent must never echo or persist.

    Every selected row is read inside the caller's tenant-pinned transaction.
    Returned values remain inside :class:`ExactSecretBoundary`; only opaque
    source references may leave the boundary in audit evidence.
    """

    from app.models.channel_config import ChannelConfig
    from app.models.llm import LLMModel
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.models.tenant_tool_config import TenantToolConfig
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tool_config_storage import is_agent_tool_credential_key
    from app.services.mcp_oauth import decrypt_token_set, decrypt_value
    from app.services.tool_config_service import (
        MASKED_SECRET_SENTINEL,
        decrypt_tool_config_secrets,
    )

    pairs: list[tuple[str, str]] = []

    models = (
        (
            await db.execute(
                select(LLMModel).where(
                    or_(
                        LLMModel.tenant_id.is_(None),
                        LLMModel.tenant_id == tenant_id,
                    ),
                    LLMModel.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for model in models:
        value = str(model.api_key or "")
        if value:
            pairs.append((f"llm-model://{model.id}/api_key", value))

    channel_boundaries: list[ExactSecretBoundary] = []
    channel_query = select(ChannelConfig).where(ChannelConfig.tenant_id == tenant_id)
    if agent_id is not None:
        channel_query = channel_query.where(ChannelConfig.agent_id == agent_id)
    channels = (await db.execute(channel_query)).scalars().all()
    for config in channels:
        channel_boundaries.append(
            boundary_from_channel_config(
                config,
                agent_id=config.agent_id,
                channel=str(config.channel_type),
            )
        )
    tenant_channels = (
        (
            await db.execute(
                select(TenantChannelConfig).where(
                    TenantChannelConfig.tenant_id == tenant_id,
                    TenantChannelConfig.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for config in tenant_channels:
        channel_boundaries.append(
            boundary_from_channel_config(
                config,
                agent_id=f"tenant-{tenant_id}",
                channel=str(config.channel_type),
            )
        )

    tools = (
        (
            await db.execute(
                select(Tool).where(
                    Tool.enabled.is_(True),
                    or_(Tool.tenant_id.is_(None), Tool.tenant_id == tenant_id),
                )
            )
        )
        .scalars()
        .all()
    )
    tool_ids = [tool.id for tool in tools]
    tenant_overrides: dict[uuid.UUID, dict] = {}
    agent_overrides: dict[uuid.UUID, dict] = {}
    if tool_ids:
        tenant_rows = (
            (
                await db.execute(
                    select(TenantToolConfig).where(
                        TenantToolConfig.tenant_id == tenant_id,
                        TenantToolConfig.tool_id.in_(tool_ids),
                        TenantToolConfig.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        tenant_overrides = {row.tool_id: dict(row.config or {}) for row in tenant_rows}
        if agent_id is not None:
            agent_rows = (
                (
                    await db.execute(
                        select(AgentTool).where(
                            AgentTool.tenant_id == tenant_id,
                            AgentTool.agent_id == agent_id,
                            AgentTool.tool_id.in_(tool_ids),
                            AgentTool.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            agent_overrides = {row.tool_id: dict(row.config or {}) for row in agent_rows}

    for tool in tools:
        merged = dict(tool.config or {})
        merged.update(tenant_overrides.get(tool.id, {}))
        merged.update(agent_overrides.get(tool.id, {}))
        runtime_config = decrypt_tool_config_secrets(merged, tool.config_schema)
        _collect_tool_secret_pairs(
            runtime_config,
            config_schema=tool.config_schema,
            prefix=f"tool-config://{tenant_id}/{agent_id or 'tenant'}/{tool.id}",
            sentinel=MASKED_SECRET_SENTINEL,
            is_credential_key=is_agent_tool_credential_key,
            pairs=pairs,
        )

    server_query = select(MCPServer).where(MCPServer.tenant_id == tenant_id)
    if agent_id is not None:
        assigned_ids = (
            (
                await db.execute(
                    select(AgentMCPServerAssignment.mcp_server_id).where(
                        AgentMCPServerAssignment.tenant_id == tenant_id,
                        AgentMCPServerAssignment.agent_id == agent_id,
                        AgentMCPServerAssignment.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        server_query = server_query.where(MCPServer.id.in_(assigned_ids))
    servers = (await db.execute(server_query)).scalars().all()
    for server in servers:
        oauth = dict((server.config_json or {}).get("oauth") or {})
        token = decrypt_token_set(oauth.get("token"))
        if token is not None:
            pairs.append((f"mcp-server://{server.id}/oauth/access_token", token.access_token))
            if token.refresh_token:
                pairs.append((f"mcp-server://{server.id}/oauth/refresh_token", token.refresh_token))
        client_secret = decrypt_value(oauth.get("client_secret"))
        if client_secret:
            pairs.append((f"mcp-server://{server.id}/oauth/client_secret", client_secret))
        pending = dict(oauth.get("pending") or {})
        verifier = decrypt_value(pending.get("verifier"))
        if verifier:
            pairs.append((f"mcp-server://{server.id}/oauth/pkce_verifier", verifier))
        pending_client_secret = decrypt_value(pending.get("client_secret"))
        if pending_client_secret:
            pairs.append((f"mcp-server://{server.id}/oauth/pending_client_secret", pending_client_secret))

    return ExactSecretBoundary.combine(
        ExactSecretBoundary.from_pairs(pairs),
        *channel_boundaries,
    )


async def load_runtime_ingress_secret_boundary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    reply_target: dict[str, Any] | None = None,
) -> ExactSecretBoundary:
    """Load the exact tenant inventory plus the current typed reply target."""

    try:
        tenant_boundary = await load_exact_secret_boundary(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        return ExactSecretBoundary.combine(
            tenant_boundary,
            boundary_from_reply_target(reply_target),
        )
    except RuntimeIngressSecretBoundaryUnavailable:
        raise
    except Exception as exc:
        raise RuntimeIngressSecretBoundaryUnavailable("runtime ingress credential authority is unavailable") from exc


async def redact_runtime_ingress_payload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    payload: Any,
    reply_target: dict[str, Any] | None = None,
) -> ExactSecretPayloadRedaction:
    """Redact only exact authority-backed values before durable ingress."""

    if not isinstance(db, AsyncSession):
        # Production entry points are typed to AsyncSession. Lightweight unit
        # doubles cannot prove tenant pinning or credential-store authority,
        # so they exercise wiring with an injected redactor instead.
        return ExactSecretPayloadRedaction(value=payload)
    boundary = await load_runtime_ingress_secret_boundary(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        reply_target=reply_target,
    )
    return boundary.redact_payload_with_evidence(payload)


def exact_secret_redaction_receipt(
    redaction: ExactSecretPayloadRedaction,
    *,
    phase: str,
) -> dict[str, Any] | None:
    """Build value-free evidence for an exact ingress redaction."""

    if redaction.redacted_count <= 0:
        return None
    return {
        "schema": "hive.exact_secret_redaction_receipt",
        "schema_version": 1,
        "phase": str(phase),
        "redacted_count": redaction.redacted_count,
        "source_refs": list(redaction.matched_refs),
    }


def _collect_tool_secret_pairs(
    config: dict[str, Any],
    *,
    config_schema: dict | None,
    prefix: str,
    sentinel: str,
    is_credential_key,
    pairs: list[tuple[str, str]],
) -> None:
    fields = config_schema.get("fields") if isinstance(config_schema, dict) else None
    for field in fields if isinstance(fields, list) else ():
        if not isinstance(field, dict) or field.get("type") != "password":
            continue
        key = str(field.get("key") or "")
        value = config.get(key)
        if field.get("multiline") is True and isinstance(value, str):
            for index, item in enumerate(part.strip() for part in value.replace(",", "\n").splitlines()):
                if item and item != sentinel:
                    pairs.append((f"{prefix}/{key}/{index}", item))
        elif isinstance(value, str) and value and value != sentinel:
            pairs.append((f"{prefix}/{key}", value))

    def walk(value: Any, *, path: str, protected: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(
                    child,
                    path=f"{path}/{key}",
                    protected=protected or bool(is_credential_key(key)),
                )
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path=f"{path}/{index}", protected=protected)
            return
        if protected and isinstance(value, str) and value and value != sentinel:
            pairs.append((path, value))

    walk(config, path=prefix)
