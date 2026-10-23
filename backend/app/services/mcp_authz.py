"""MCP authorization guardrails.

Hive accepts server-scoped MCP credentials, but never forwards user/OAuth
tokens or URL userinfo to arbitrary MCP resource servers.
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

PASSTHROUGH_TOKEN_KEYS = {
    "access_token",
    "authorization",
    "bearer",
    "bearer_token",
    "id_token",
    "jwt",
    "oauth_token",
    "refresh_token",
    "session_token",
    "user_token",
}
API_KEY_QUERY_KEYS = {"apikey", "api_key"}
LOCAL_ONLY_TRANSPORTS = {"stdio", "ws", "websocket", "wss", "sdk", "ipc", "local"}
CLOUD_CORE_TRANSPORTS = {"http", "https", "sse", "streamable", "streamable_http", "http_sse"}


class MCPAuthzError(ValueError):
    """Raised when MCP auth material violates Hive's authz policy."""


class MCPStoredCredentialRequired(MCPAuthzError):
    """A stored MCP credential has no current scoped assignment."""


class MCPCompanyCredentialAuthorityDenied(MCPAuthzError):
    """The current principal cannot issue company credential authority."""


MCP_COMPANY_CREDENTIAL_SCOPE = "_hive_mcp_company_credential_v1"


def _company_api_key(tool) -> str | None:
    from app.services.tool_config_service import decrypt_tool_config_secrets, _ENCRYPTED_SECRET_PREFIX

    config = decrypt_tool_config_secrets(tool.config or {}, getattr(tool, "config_schema", None))
    key = config.get("api_key")
    if not key:
        return None
    if not isinstance(key, str) or key.startswith(_ENCRYPTED_SECRET_PREFIX):
        raise MCPStoredCredentialRequired("Company MCP credential storage is unavailable; restore its configuration keyring.")
    return key


def mcp_company_credential_binding(tool, tenant_id) -> dict:
    """Comparison facts only; authority comes from an administrator-issued receipt."""
    key = _company_api_key(tool)
    if not key:
        raise MCPStoredCredentialRequired("No company MCP credential is configured.")
    endpoint, _ = split_mcp_server_url_and_api_key(tool.mcp_server_url or "")
    return {"tenant_id": str(tenant_id), "tool_id": str(tool.id),
        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "credential_sha256": hashlib.sha256(key.encode()).hexdigest()}


async def require_mcp_company_admin(db, *, tenant_id, actor_id) -> None:
    """Recheck the live principal and selected company at the write boundary."""
    from sqlalchemy import select
    from app.models.tenant import Tenant
    from app.database import get_current_tenant_id
    from app.services.runtime_user_identity import load_runtime_user

    company = await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True))
    actor = await load_runtime_user(db, actor_id, lock=True)
    if (actor is None or actor.role not in {"platform_admin", "org_admin"}
        or (actor.role == "org_admin" and actor.tenant_id != tenant_id)
        or str(get_current_tenant_id()) != str(tenant_id) or company is None or not company.is_active):
        raise MCPCompanyCredentialAuthorityDenied("A current administrator in an active selected company is required.")


async def record_mcp_company_credential(db, *, tool, actor_id, source: str) -> None:
    """Called only by the authenticated administrator configuration boundary."""
    from sqlalchemy import select
    from app.models.audit import AuditLog
    from app.models.tenant_tool_config import TenantToolConfig

    tenant_id = tool.tenant_id
    if tenant_id is None or source not in {"setting", "confirmation"}:
        raise MCPAuthzError("An owned company MCP tool and explicit administrator action are required.")
    record = (await db.execute(select(TenantToolConfig).where(
        TenantToolConfig.tenant_id == tenant_id, TenantToolConfig.tool_id == tool.id))).scalar_one_or_none()
    await require_mcp_company_admin(db, tenant_id=tenant_id, actor_id=actor_id)
    config = dict(record.config or {}) if record else {}
    if not (tool.config or {}).get("api_key"):
        config.pop(MCP_COMPANY_CREDENTIAL_SCOPE, None)
        if record:
            record.config = config
        return
    binding = mcp_company_credential_binding(tool, tenant_id)
    old = config.get(MCP_COMPANY_CREDENTIAL_SCOPE)
    if record and isinstance(old, dict) and old.get("binding") == binding:
        return
    authorization_id = str(uuid.uuid4())
    config[MCP_COMPANY_CREDENTIAL_SCOPE] = {"authorization_id": authorization_id,
        "authorized_by": str(actor_id), "binding": binding}
    if record is None:
        record = TenantToolConfig(tenant_id=tenant_id, tool_id=tool.id, enabled=True)
        db.add(record)
    record.config = config
    db.add(AuditLog(tenant_id=tenant_id, user_id=actor_id, action="mcp.company_credential.authorized",
        details={"tool_id": str(tool.id), "authorization_id": authorization_id, "source": source,
            "status": "company_scope_authorized; external_execution_not_asserted"}))


async def resolve_mcp_api_key(db, *, tool, tenant_id, agent_config: dict, oauth_bearer=None):
    """Resolve a scoped credential without promoting a legacy shared value."""
    from sqlalchemy import and_, or_, select
    from types import SimpleNamespace
    from app.models.tenant_tool_config import TenantToolConfig
    from app.models.tool import Tool

    row = (await db.execute(select(
        TenantToolConfig.config.label("company_config"), TenantToolConfig.enabled.label("company_enabled"),
        Tool.enabled.label("tool_enabled"), Tool.config.label("tool_config"),
        Tool.config_schema.label("config_schema"), Tool.mcp_server_url.label("server_url"),
        Tool.mcp_metadata_fingerprint.label("fingerprint"),
    ).select_from(Tool).outerjoin(TenantToolConfig, and_(
        TenantToolConfig.tenant_id == tenant_id, TenantToolConfig.tool_id == Tool.id,
    )).where(Tool.id == tool.id, or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None))))).one_or_none()
    if row is None or not row.tool_enabled or row.tool_config != tool.config or row.server_url != tool.mcp_server_url or (
        row.fingerprint != tool.mcp_metadata_fingerprint
        or (row.config_schema or {}) != (getattr(tool, "config_schema", None) or {})
    ):
        raise MCPStoredCredentialRequired("MCP configuration changed while resolving its credential; reload and retry.")
    if row.company_enabled is False:
        raise MCPAuthzError("MCP tool is disabled by company policy.")
    if oauth_bearer:
        return oauth_bearer
    if "api_key" in agent_config:
        return agent_config["api_key"]
    if not (tool.config or {}).get("api_key"):
        return None
    expected = mcp_company_credential_binding(tool, tenant_id)
    receipt = (row.company_config or {}).get(MCP_COMPANY_CREDENTIAL_SCOPE)
    valid = row.company_enabled and isinstance(receipt, dict) and receipt.get("binding") == expected
    if valid:
        try:
            uuid.UUID(str(receipt["authorization_id"]))
            uuid.UUID(str(receipt["authorized_by"]))
        except (KeyError, TypeError, ValueError):
            valid = False
    if not valid:
        raise MCPStoredCredentialRequired(
            "This shared MCP credential has no confirmed company scope for the current tool and endpoint. "
            "A company administrator can confirm the existing value against its current fingerprint or re-enter it securely."
        )
    return _company_api_key(SimpleNamespace(config=row.tool_config, config_schema=row.config_schema))


def mcp_url_reference(server_url: str) -> str:
    """Stable legacy connection identity without exposing URL credentials."""
    return "mcp-url-" + hashlib.sha256(server_url.encode("utf-8")).hexdigest()


def resolve_stored_mcp_endpoint(server_url: str, *, api_key: str | None = None) -> tuple[str, str | None]:
    """Use current scoped credentials, never an implicit key in a stored URL."""
    clean_url, embedded_key = split_mcp_server_url_and_api_key(server_url)
    if embedded_key and not api_key:
        raise MCPStoredCredentialRequired(
            "This legacy MCP URL contains an unattributed credential. Configure this employee's "
            "API key, or use an authorized company or OAuth credential before retrying."
        )
    return clean_url, api_key


def _normalize_transport(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def assert_mcp_cloud_transport_allowed(server_url: str | None = None, transport: object = None) -> None:
    """Fail closed for MCP transports that require local runtime ownership.

    Hive cloud core can connect to HTTP/SSE MCP endpoints. Local-resource
    transports (stdio/WebSocket SDK/local IPC) must enter through the Local
    Bridge or the optional coding plugin, not through the cloud MCP client.
    """
    explicit_transport = _normalize_transport(transport)
    parsed = urlparse((server_url or "").strip())
    scheme_transport = _normalize_transport(parsed.scheme)

    candidates = [candidate for candidate in (explicit_transport, scheme_transport) if candidate]
    for candidate in candidates:
        if candidate in LOCAL_ONLY_TRANSPORTS:
            raise MCPAuthzError(
                "MCP local transport "
                f"{candidate!r} requires the local bridge / coding plugin; "
                "cloud core only supports streamable_http and sse MCP endpoints."
            )
        if candidate not in CLOUD_CORE_TRANSPORTS:
            raise MCPAuthzError(
                f"MCP transport {candidate!r} is not supported by cloud core; "
                "use streamable_http/sse or route local transports through the coding plugin."
            )


def split_mcp_server_url_and_api_key(server_url: str, explicit_api_key: str | None = None) -> tuple[str, str | None]:
    """Return a sanitized MCP URL and server-scoped API key.

    `apiKey` / `api_key` query params are accepted for legacy imports but are
    removed from the URL and sent as an Authorization header. OAuth/user token
    query params and URL userinfo are rejected because MCP token passthrough is
    explicitly forbidden.
    """
    parsed = urlparse((server_url or "").strip())
    assert_mcp_cloud_transport_allowed(server_url)
    if parsed.username or parsed.password:
        raise MCPAuthzError("MCP server URL userinfo is forbidden; store credentials separately.")

    query = parse_qs(parsed.query, keep_blank_values=True)
    api_key = explicit_api_key
    kept_query: dict[str, list[str]] = {}
    blocked: list[str] = []

    for key, values in query.items():
        normalized = key.strip().lower()
        if normalized in PASSTHROUGH_TOKEN_KEYS:
            blocked.append(key)
            continue
        if normalized in API_KEY_QUERY_KEYS:
            if not api_key and values:
                api_key = values[0]
            continue
        kept_query[key] = values

    if blocked:
        raise MCPAuthzError(f"MCP token passthrough forbidden in server URL query: {', '.join(sorted(blocked))}")

    sanitized_query = urlencode(kept_query, doseq=True)
    return urlunparse(parsed._replace(query=sanitized_query)).rstrip("/"), api_key


def assert_no_mcp_token_passthrough(config: dict | None) -> None:
    """Reject common user/OAuth token fields before MCP execution."""
    if not config:
        return
    blocked = sorted(str(key) for key in config if str(key).strip().lower() in PASSTHROUGH_TOKEN_KEYS)
    if blocked:
        raise MCPAuthzError(f"MCP token passthrough forbidden in tool config: {', '.join(blocked)}")
