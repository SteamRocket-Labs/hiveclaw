"""Resolve exact protected credentials from tenant-scoped runtime authority."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any
import uuid

from sqlalchemy import cast, null, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.services.exact_secret_boundary import (
    ExactSecretBoundary,
    ExactSecretPayloadRedaction,
    boundary_from_channel_config,
    boundary_from_reply_target,
)


class RuntimeIngressSecretBoundaryUnavailable(RuntimeError):
    """The authoritative credential inventory could not be loaded."""


@dataclass(slots=True)
class IngressSecretInventory:
    """Carry one requester's inventory from ingress to inline dispatch."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    boundary: ExactSecretBoundary | None = dataclass_field(default=None, init=False, repr=False)


_PASTE_POLICY_EXEMPT_SOURCES = frozenset({
    "feishu", "lark", "dingtalk", "wecom", "slack", "telegram", "channel",
    "plan_mode_handoff", "runtime_result_integration", "hr_agent_handoff",
    "trigger_reflection", "system_plan_run", "loop_same_session",
    "goal_continuation", "agent_session_mailbox",
    "discord", "microsoft_teams", "wechat_personal", "a2a", "task_notification",
})


async def load_exact_secret_boundary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> ExactSecretBoundary:
    """Load exact secrets that the current Agent must never echo or persist."""
    return await load_requester_exact_secret_boundary(
        db, tenant_id=tenant_id, agent_id=agent_id, requester_user_id=None,
    )


async def load_requester_exact_secret_boundary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    requester_user_id: uuid.UUID | None,
) -> ExactSecretBoundary:
    """Read the tenant and requester credentials in the caller's tenant-pinned transaction."""
    from app.models.channel_config import ChannelConfig
    from app.models.custom_api_account import CustomApiAccount
    from app.models.email_account import EmailAccount
    from app.models.llm import LLMModel
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.models.tenant_tool_config import TenantToolConfig
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tool_config_storage import is_agent_tool_credential_key
    from app.services.tool_config_service import MASKED_SECRET_SENTINEL, decrypt_tool_config_secrets

    tools = select(Tool).where(
        Tool.enabled.is_(True), or_(Tool.tenant_id.is_(None), Tool.tenant_id == tenant_id),
    )
    tool_ids = tools.with_only_columns(Tool.id)
    channels = select(ChannelConfig).where(ChannelConfig.tenant_id == tenant_id)
    servers = select(MCPServer).where(MCPServer.tenant_id == tenant_id)
    if agent_id is not None:
        channels = channels.where(ChannelConfig.agent_id == agent_id)
        assigned_ids = select(AgentMCPServerAssignment.mcp_server_id).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.agent_id == agent_id,
            AgentMCPServerAssignment.enabled.is_(True),
        )
        servers = servers.where(MCPServer.id.in_(assigned_ids))

    queries = {
        LLMModel: select(LLMModel).where(
            or_(LLMModel.tenant_id.is_(None), LLMModel.tenant_id == tenant_id), LLMModel.enabled.is_(True),
        ),
        ChannelConfig: channels,
        TenantChannelConfig: select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == tenant_id, TenantChannelConfig.is_active.is_(True),
        ),
        Tool: tools,
        TenantToolConfig: select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id.in_(tool_ids),
            TenantToolConfig.enabled.is_(True),
        ),
        MCPServer: servers,
    }
    if agent_id is not None:
        queries[AgentTool] = select(AgentTool).where(
            AgentTool.tenant_id == tenant_id,
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id.in_(tool_ids),
            AgentTool.enabled.is_(True),
        )
    account_types = (
        (CustomApiAccount, "custom-api-account", ("api_key", "bearer_token", "basic_password")),
        (EmailAccount, "email-account", ("auth_code",)),
    )
    personal_server = aliased(MCPServer)
    if requester_user_id is not None:
        for account_type, _, _ in account_types:
            queries[account_type] = select(account_type).where(
                account_type.tenant_id == tenant_id, account_type.owner_user_id == requester_user_id,
            )
        queries[personal_server] = select(personal_server).where(
            personal_server.tenant_id == tenant_id,
            personal_server.owner_user_id == requester_user_id,
            personal_server.parent_server_id.is_not(None),
        )
    rows = await _read_inventory_rows(db, queries)

    pairs: list[tuple[str, str]] = []
    for model in rows[LLMModel]:
        if value := str(model.api_key or ""):
            pairs.append((f"llm-model://{model.id}/api_key", value))

    tenant_overrides = {row.tool_id: dict(row.config or {}) for row in rows[TenantToolConfig]}
    agent_overrides = {row.tool_id: dict(row.config or {}) for row in rows.get(AgentTool, ())}
    for tool in rows[Tool]:
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
    pairs.extend(_mcp_secret_pairs(rows[MCPServer]))

    channel_boundaries = [
        boundary_from_channel_config(config, agent_id=config.agent_id, channel=str(config.channel_type))
        for config in rows[ChannelConfig]
    ]
    channel_boundaries.extend(
        boundary_from_channel_config(config, agent_id=f"tenant-{tenant_id}", channel=str(config.channel_type))
        for config in rows[TenantChannelConfig]
    )

    private_pairs: list[tuple[str, str]] = []
    for account_type, source_kind, secret_keys in account_types:
        for account in rows.get(account_type, ()):
            private_pairs.extend(
                (f"{source_kind}://{account.id}/{key}", value)
                for key in secret_keys
                if isinstance(value := (account.config or {}).get(key), str) and value
            )
    private_pairs.extend(_mcp_secret_pairs(rows.get(personal_server, ())))
    return ExactSecretBoundary.combine(
        ExactSecretBoundary.from_pairs(pairs), *channel_boundaries, ExactSecretBoundary.from_pairs(private_pairs),
    )


async def _read_inventory_rows(db: AsyncSession, queries: dict) -> dict:
    # Keep each source's normal ORM load so the caller can reuse its instances.
    # Typed NULLs pad the other sources without losing column decryption.
    columns = [tuple(query.selected_columns) for query in queries.values()]
    slots = [tuple(col.label(f"source_{index}_{col.key}") for col in group) for index, group in enumerate(columns)]
    branches = [
        query.with_only_columns(*(
            col if owner == source else cast(null(), col.type).label(col.name)
            for owner, group in enumerate(slots) for col in group
        ), maintain_column_froms=True)
        for source, query in enumerate(queries.values())
    ]
    packed = union_all(*branches).subquery()
    sources = [aliased(model, packed) for model in queries]
    statement = select(*sources)
    rows = {model: [] for model in queries}
    for row in await db.execute(statement):
        for group, value in zip(rows.values(), row, strict=True):
            if value is not None:
                group.append(value)
    return rows


def _mcp_secret_pairs(servers) -> list[tuple[str, str]]:
    from app.services.mcp_oauth import decrypt_token_set, decrypt_value

    pairs: list[tuple[str, str]] = []
    for server in servers:
        oauth = dict((server.config_json or {}).get("oauth") or {})
        token = decrypt_token_set(oauth.get("token"))
        if token is not None:
            pairs.extend(
                (f"mcp-server://{server.id}/oauth/{key}", value)
                for key in ("access_token", "refresh_token") if (value := getattr(token, key))
            )
        pending = dict(oauth.get("pending") or {})
        for path, config, key in (
            ("client_secret", oauth, "client_secret"),
            ("pkce_verifier", pending, "verifier"),
            ("pending_client_secret", pending, "client_secret"),
        ):
            if value := decrypt_value(config.get(key)):
                pairs.append((f"mcp-server://{server.id}/oauth/{path}", value))
    return pairs


async def load_runtime_ingress_secret_boundary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    requester_user_id: uuid.UUID | None,
    reply_target: dict[str, Any] | None = None,
    inventory: IngressSecretInventory | None = None,
) -> ExactSecretBoundary:
    """Load the exact tenant inventory plus the current typed reply target."""

    try:
        same_requester = inventory is not None and (
            inventory.tenant_id, inventory.agent_id, inventory.requester_user_id
        ) == (tenant_id, agent_id, requester_user_id)
        tenant_boundary = inventory.boundary if same_requester else None
        if tenant_boundary is None:
            tenant_boundary = await load_requester_exact_secret_boundary(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                requester_user_id=requester_user_id,
            )
            if same_requester:
                inventory.boundary = tenant_boundary
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
    requester_user_id: uuid.UUID | None,
    payload: Any,
    reply_target: dict[str, Any] | None = None,
    source_channel: str | None = None,
    source: str | None = None,
    inventory: IngressSecretInventory | None = None,
) -> ExactSecretPayloadRedaction:
    """Protect durable ingress, including the opt-in Web paste policy."""

    from app.services.privacy_layer import PrivacyLayer

    if not PrivacyLayer.has_member_text(payload):
        return ExactSecretPayloadRedaction(value=payload)
    web_paste = source_channel == "web" and source not in _PASTE_POLICY_EXEMPT_SOURCES
    pasted_count = 0
    if web_paste:
        pasted = PrivacyLayer.redact_pasted_credentials(payload)
        payload = pasted.value
        pasted_count = pasted.redacted_count

    if not isinstance(db, AsyncSession):
        # Production entry points are typed to AsyncSession. Lightweight unit
        # doubles cannot prove tenant pinning or credential-store authority,
        # so they exercise wiring with an injected redactor instead.
        redaction = ExactSecretPayloadRedaction(value=payload)
    else:
        boundary = await load_runtime_ingress_secret_boundary(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            requester_user_id=requester_user_id,
            reply_target=reply_target,
            inventory=inventory,
        )
        redaction = boundary.redact_payload_with_evidence(payload)
    if web_paste and (pasted_count or redaction.redacted_count) and isinstance(redaction.value, dict):
        redaction.value.setdefault("extra_metadata", {})["pasted_credential_notice"] = True
    return redaction


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
