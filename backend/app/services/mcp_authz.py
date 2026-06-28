"""MCP authorization guardrails.

Hive accepts server-scoped MCP credentials, but never forwards user/OAuth
tokens or URL userinfo to arbitrary MCP resource servers.
"""

from __future__ import annotations

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
