"""Tool config resolution service — per-tenant tool configuration isolation.

Merge hierarchy:
  Tool.config (platform default) < TenantToolConfig.config (tenant override) < AgentTool.config (agent override)

Every runtime tool config read MUST go through resolve_tool_config() to
ensure tenant isolation. Direct reads from Tool.config leak API keys
across tenants.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_scoped_session
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool

logger = logging.getLogger(__name__)

# Sentinel returned to clients in place of a stored secret value. The API must
# never echo a stored API key back — a freshly created tenant that inherits a
# platform-default builtin tool would otherwise receive the previous tenant's
# key verbatim (cross-tenant secret leak).
MASKED_SECRET_SENTINEL = "__HIVE_SECRET_SET__"


def _schema_fields_by_key(config_schema: dict | None) -> dict[str, dict]:
    if not isinstance(config_schema, dict):
        return {}
    fields = config_schema.get("fields")
    if not isinstance(fields, list):
        return {}
    return {str(field["key"]): field for field in fields if isinstance(field, dict) and field.get("key")}


def _secret_field_keys(config_schema: dict | None) -> set[str]:
    """Return config keys whose schema field type is ``password`` (i.e. secrets)."""
    return {key for key, field in _schema_fields_by_key(config_schema).items() if field.get("type") == "password"}


def _split_multiline_secret_rows(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n,]+", value) if part.strip()]
    return []


def _join_multiline_secret_rows(rows: list[str]) -> str:
    return "\n".join(row for row in rows if row)


def mask_tool_config_secrets(config: dict | None, config_schema: dict | None) -> dict:
    """Replace non-empty secret values with :data:`MASKED_SECRET_SENTINEL`.

    Empty secrets stay empty so the UI can still tell "unset" from "set".
    Non-secret fields pass through unchanged.
    """
    if not isinstance(config, dict):
        return {}
    masked = dict(config)
    fields_by_key = _schema_fields_by_key(config_schema)
    for key in _secret_field_keys(config_schema):
        value = masked.get(key)
        if not value:
            continue
        if fields_by_key.get(key, {}).get("multiline") is True:
            rows = _split_multiline_secret_rows(_decrypt_secret(value))
            if rows:
                masked[key] = _join_multiline_secret_rows([MASKED_SECRET_SENTINEL] * len(rows))
        elif isinstance(value, str) and value:
            masked[key] = MASKED_SECRET_SENTINEL
    return masked


def is_safe_agent_tool_setting(key: str, value: object, config_schema: dict | None, *, platform_tool: bool) -> bool:
    """Only platform-declared non-credential scalars may be shown or handed over."""
    field = _schema_fields_by_key(config_schema).get(key)
    return bool(platform_tool and field and field.get("type") in {"select", "number", "boolean"}
        and isinstance(value, (str, int, float, bool, type(None))) and value != MASKED_SECRET_SENTINEL)


def mask_agent_tool_config_secrets(config: dict | None, config_schema: dict | None, *, platform_tool: bool = True) -> dict:
    """Mask each unsafe non-empty setting as a whole, including containers."""
    return {key: value if value in (None, "", {}, []) or is_safe_agent_tool_setting(key, value, config_schema, platform_tool=platform_tool)
        else MASKED_SECRET_SENTINEL for key, value in config.items()} if isinstance(config, dict) else {}


def clear_agent_tool_config_secrets(config: dict | None, config_schema: dict | None, *, platform_tool: bool = True,
    company_config: dict | None = None) -> dict:
    """Keep company copies and declared safe scalars; empty personal secrets."""
    company = company_config or {}
    return {key: value if is_safe_agent_tool_setting(key, value, config_schema, platform_tool=platform_tool)
        or (key in company and _decrypt_secret(value) == company[key]) else ""
        for key, value in config.items()} if isinstance(config, dict) else {}


def merge_tool_config_secrets(
    incoming: dict | None,
    stored: dict | None,
    config_schema: dict | None,
) -> dict:
    """Restore stored secret values for fields the client left masked.

    Write-back companion to :func:`mask_tool_config_secrets`. For each secret
    field: a sentinel value or an absent key keeps the stored value (the user
    did not edit it); an empty string clears it; any other value updates it.
    Non-secret fields pass through from ``incoming`` unchanged.
    """
    incoming = dict(incoming) if isinstance(incoming, dict) else {}
    stored = stored if isinstance(stored, dict) else {}
    incoming = {key: value for key, value in merge_agent_tool_config_secrets(incoming, stored, config_schema).items()
        if key in incoming}
    fields_by_key = _schema_fields_by_key(config_schema)
    for key in _secret_field_keys(config_schema):
        if key not in incoming or incoming.get(key) == MASKED_SECRET_SENTINEL:
            if key in stored:
                incoming[key] = stored[key]
            else:
                incoming.pop(key, None)
            continue
        if fields_by_key.get(key, {}).get("multiline") is True:
            value = incoming.get(key)
            if value == "":
                continue
            stored_rows = _split_multiline_secret_rows(_decrypt_secret(stored.get(key)))
            merged_rows: list[str] = []
            for index, row in enumerate(_split_multiline_secret_rows(value)):
                if row == MASKED_SECRET_SENTINEL:
                    if index < len(stored_rows):
                        merged_rows.append(stored_rows[index])
                else:
                    merged_rows.append(row)
            if merged_rows:
                incoming[key] = _join_multiline_secret_rows(merged_rows)
            else:
                incoming.pop(key, None)
    return incoming


def merge_agent_tool_config_secrets(
    incoming: dict | None,
    stored: dict | None,
    config_schema: dict | None,
    *,
    platform_tool: bool = True,
) -> dict:
    """A sentinel keeps current storage; every other submitted value is new input."""
    merged = dict(stored) if isinstance(stored, dict) else {}
    for key, value in (incoming or {}).items():
        if isinstance(value, (dict, list)) and _contains_mask(value):
            from fastapi import HTTPException

            raise HTTPException(400, "Masked nested settings must be entered again before saving.")
        if value == MASKED_SECRET_SENTINEL:
            continue
        merged[key] = value
    return merged


def _contains_mask(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_mask(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mask(item) for item in value)
    return value == MASKED_SECRET_SENTINEL


_ENCRYPTED_SECRET_PREFIX = "enc:v1:"


def _encrypt_secret(value: str) -> str:
    """Encrypt one secret value, tagging the ciphertext so reads can tell it
    from legacy plaintext. A no-op provider (no ``SECRETS_MASTER_KEY``) returns
    the value unchanged; we then store plaintext WITHOUT the tag, preserving
    current behavior with zero migration risk.
    """
    if not isinstance(value, str) or not value or value.startswith(_ENCRYPTED_SECRET_PREFIX):
        return value
    from app.services.secrets_provider import get_secrets_provider

    ciphertext = get_secrets_provider().encrypt(value)
    if ciphertext == value:  # no-op provider (dev / no master key)
        return value
    return _ENCRYPTED_SECRET_PREFIX + ciphertext


def _decrypt_secret(value: str) -> str:
    """Inverse of :func:`_encrypt_secret`. Untagged (legacy plaintext) values are
    returned as-is and never fed to the provider — so a configured master key
    does not blow up on pre-encryption rows.
    """
    if not isinstance(value, str) or not value.startswith(_ENCRYPTED_SECRET_PREFIX):
        return value
    from app.services.secrets_provider import get_secrets_provider

    return get_secrets_provider().decrypt(value[len(_ENCRYPTED_SECRET_PREFIX) :])


def encrypt_tool_config_secrets(config: dict | None, config_schema: dict | None) -> dict:
    """Encrypt secret (password-typed) fields before persisting tool config."""
    if not isinstance(config, dict):
        return {}
    out = dict(config)
    for key in _secret_field_keys(config_schema):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = _encrypt_secret(value)
    return out


def decrypt_tool_config_secrets(config: dict | None, config_schema: dict | None) -> dict:
    """Decrypt secret fields after reading tool config for runtime use."""
    if not isinstance(config, dict):
        return {}
    out = dict(config)
    for key in _secret_field_keys(config_schema):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = _decrypt_secret(value)
    return out


async def resolve_tool_config(
    tool_name: str,
    tenant_id: uuid.UUID | None = None,
    *,
    agent_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Resolve effective tool config with full tenant isolation.

    If tenant_id is not provided but agent_id is, resolves tenant_id from agent.
    Merges: Tool.config → TenantToolConfig.config → AgentTool.config (if agent_id given).
    """
    if db is None:
        # No caller session: open a GUC-pinned one so the Tool/Agent reads survive
        # under enforced (non-owner) RLS. tenant_id may be None here (only agent_id
        # given) — resolve the owning tenant first so the GUC is pinned before the
        # agents read below, which is itself RLS-policied. The caller-`db` path is
        # already inside a governed session and is left untouched (Note K).
        effective_tenant = tenant_id
        if not effective_tenant and agent_id:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            effective_tenant = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(effective_tenant) as scoped_db:
            return await resolve_tool_config(tool_name, tenant_id, agent_id=agent_id, db=scoped_db)

    # 1. Get base tool
    result = await db.execute(select(Tool).where(Tool.name == tool_name, Tool.tenant_id.is_(None)).limit(1))
    tool = result.scalar_one_or_none()
    if not tool:
        # Try tenant-scoped tool
        if tenant_id:
            result = await db.execute(select(Tool).where(Tool.name == tool_name, Tool.tenant_id == tenant_id).limit(1))
            tool = result.scalar_one_or_none()
        if not tool:
            return {}

    base_config = decrypt_tool_config_secrets(tool.config, tool.config_schema)

    # 2. Resolve tenant_id from agent if needed
    effective_tenant_id = tenant_id
    if not effective_tenant_id and agent_id:
        from app.models.agent import Agent

        agent_r = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        effective_tenant_id = agent_r.scalar_one_or_none()

    # 3. Merge tenant-level override
    if effective_tenant_id:
        ttc_r = await db.execute(
            select(TenantToolConfig).where(
                TenantToolConfig.tenant_id == effective_tenant_id,
                TenantToolConfig.tool_id == tool.id,
            )
        )
        ttc = ttc_r.scalar_one_or_none()
        if ttc and ttc.config:
            base_config.update(decrypt_tool_config_secrets(ttc.config, tool.config_schema))

    # These providers historically read environment defaults in their handlers.
    # Resolve them before employee overrides so endpoint changes also withhold them.
    fallback = {"firecrawl_search": "FIRECRAWL_API_KEY", "firecrawl_fetch": "FIRECRAWL_API_KEY",
        "exa_search": "EXA_API_KEY", "exa_fetch": "EXA_API_KEY", "tavily_search": "TAVILY_API_KEY",
        "tavily_extract": "TAVILY_API_KEY", "xcrawl_scrape": "XCRAWL_API_KEY"}.get(tool_name)
    if fallback and not (base_config.get("api_key") or base_config.get(fallback.lower())):
        from app.config import get_settings

        base_config["api_key"] = getattr(get_settings(), fallback)
        if fallback == "TAVILY_API_KEY":
            from app.models.system_settings import SystemSetting

            setting = await db.scalar(select(SystemSetting).where(SystemSetting.key == "tavily_api_key"))
            if setting and setting.value.get("api_key"):
                base_config["api_key"] = setting.value["api_key"]

    # 4. Merge agent-level override (if requested)
    if agent_id:
        at_r = await db.execute(
            select(AgentTool.config).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id == tool.id,
            )
        )
        agent_config = at_r.scalar_one_or_none()
        if agent_config:
            agent_config = decrypt_tool_config_secrets(agent_config, tool.config_schema)
            fields = _schema_fields_by_key(tool.config_schema)
            endpoint_keys = {key for key in fields.keys() | base_config.keys() | agent_config.keys()
                if key in {"url", "uri"} or key.endswith(("_url", "_host"))
                or fields.get(key, {}).get("type") in {"url", "uri"} or fields.get(key, {}).get("format") in {"uri", "url"}}
            endpoint_keys.update({"base_url", "mcp_url", "email_provider", "smtp_port", "imap_port",
                "smtp_ssl", "imap_ssl", "smtp_tls", "imap_tls"})
            agent_config = {key: value for key, value in agent_config.items() if key not in endpoint_keys or value not in ("", None)}
            if any(key in agent_config and agent_config[key] != base_config.get(key) for key in endpoint_keys):
                inherited_secrets = {key for key, value in base_config.items() if key not in endpoint_keys
                    and not is_safe_agent_tool_setting(key, value, tool.config_schema,
                        platform_tool=tool.type == "builtin" and tool.tenant_id is None)}
                agent_config = {key: value for key, value in agent_config.items() if key not in inherited_secrets or value != base_config.get(key)}
                base_config = {key: value for key, value in base_config.items() if key not in inherited_secrets}
            base_config.update(agent_config)

    return base_config


async def get_or_create_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_id: uuid.UUID,
) -> TenantToolConfig:
    """Get existing or create new TenantToolConfig record."""
    result = await db.execute(
        select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id == tool_id,
        )
    )
    ttc = result.scalar_one_or_none()
    if ttc:
        return ttc

    ttc = TenantToolConfig(
        tenant_id=tenant_id,
        tool_id=tool_id,
        config={},
        enabled=True,
    )
    db.add(ttc)
    await db.flush()
    return ttc


async def update_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_id: uuid.UUID,
    *,
    config: dict | None = None,
    enabled: bool | None = None,
    config_schema: dict | None = None,
) -> TenantToolConfig:
    """Create or update tenant-level tool config.

    When ``config_schema`` is given, masked secret fields in ``config`` are
    reconciled against the currently stored values so a UI round-trip that
    echoes the :data:`MASKED_SECRET_SENTINEL` never overwrites a real key.
    """
    ttc = await get_or_create_tenant_tool_config(db, tenant_id, tool_id)
    if config is not None:
        config = merge_tool_config_secrets(config, ttc.config, config_schema)
        config = encrypt_tool_config_secrets(config, config_schema)
        ttc.config = config
    if enabled is not None:
        ttc.enabled = enabled
    await db.flush()
    return ttc


async def resolve_tool_config_for_tenant_display(
    db: AsyncSession,
    tool: Tool,
    tenant_id: uuid.UUID | None,
) -> tuple[dict, bool]:
    """Resolve effective config and enabled status for frontend display.

    Returns (merged_config, effective_enabled).
    """
    base_config = dict(tool.config or {})
    effective_enabled = tool.enabled

    if not tenant_id:
        return base_config, effective_enabled

    result = await db.execute(
        select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id == tool.id,
        )
    )
    ttc = result.scalar_one_or_none()
    if ttc:
        if ttc.config:
            base_config.update(ttc.config)
        effective_enabled = bool(tool.enabled) and bool(ttc.enabled) if getattr(tool, "type", None) == "mcp" else ttc.enabled

    return base_config, effective_enabled


def clear_secret_fields(config: dict | None, config_schema: dict | None) -> dict:
    """Blank out every secret (password-typed) field — tenant offboarding scrub."""
    if not isinstance(config, dict):
        return {}
    out = dict(config)
    for key in _secret_field_keys(config_schema):
        if out.get(key):
            out[key] = ""
    return out


async def scrub_tenant_tool_secrets(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Clear secret fields from a tenant's tool-config overrides on offboarding.

    Returns the number of ``TenantToolConfig`` rows mutated. Soft-deleting a
    tenant must not leave its API keys (even encrypted) sitting in the database.
    """
    rows = await db.execute(
        select(TenantToolConfig, Tool.config_schema)
        .join(Tool, Tool.id == TenantToolConfig.tool_id)
        .where(TenantToolConfig.tenant_id == tenant_id)
    )
    cleared = 0
    for ttc, schema in rows.all():
        new_config = clear_secret_fields(ttc.config, schema)
        if new_config != (ttc.config or {}):
            ttc.config = new_config
            cleared += 1
    return cleared
