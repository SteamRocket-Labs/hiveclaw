"""Machine-readable interoperability descriptors for Hive agents."""

from __future__ import annotations

from typing import Any


def _base_url(base_url: str | None) -> str:
    return (base_url or "").rstrip("/")


def _string_id(value: Any) -> str | None:
    return str(value) if value is not None else None


def build_a2a_agent_card(agent: object, *, base_url: str | None = None) -> dict[str, Any]:
    """Build an A2A-style Agent Card for a Hive digital employee.

    The card only advertises endpoints that exist today. Hive does not yet
    expose a public A2A JSON-RPC task endpoint, so that capability is reported
    explicitly as not exposed instead of implied.
    """
    base = _base_url(base_url)
    agent_id = _string_id(getattr(agent, "id", None))
    description = getattr(agent, "role_description", None) or getattr(agent, "bio", None) or ""
    card_url = f"{base}/api/v1/agents/{agent_id}/a2a-card" if base and agent_id else ""

    return {
        "protocol": "a2a",
        "protocol_version": "2025-04",
        "name": getattr(agent, "name", "") or "Hive Agent",
        "description": description,
        "url": card_url,
        "provider": {
            "name": "Hive",
            "url": base,
        },
        "authentication": {
            "schemes": [
                {
                    "type": "bearer",
                    "in": "header",
                    "name": "Authorization",
                    "for": "hive_control_plane",
                },
            ],
            "oauth_delegation": {
                "status": "not_exposed",
                "reason": "Hive exposes tenant OIDC login and per-agent sponsor identity, but not a public A2A OAuth delegation grant.",
            },
        },
        "capabilities": {
            "agent_to_agent_messages": True,
            "state_transition_history": True,
            "streaming": False,
            "push_notifications": False,
            "json_rpc_tasks": {"status": "not_exposed"},
            "collaboration_groups": {
                "status": "internal",
                "policy": "same_owner_or_public_or_active_a2a_collaboration_group",
            },
        },
        "endpoints": {
            "card": card_url,
            "messages_inbox": f"{base}/api/v1/messages/inbox" if base else "",
            "a2a_collaborators": f"{base}/api/v1/agents/{agent_id}/a2a/collaborators" if base and agent_id else "",
            "local_agent_channel": f"{base}/api/v1/local-agents/sessions" if base else "",
        },
        "skills": [
            {
                "id": "hive.agent.messages",
                "name": "Agent-to-agent messages",
                "description": "Hive persists agent-to-agent messages in tenant-scoped ChatSession/ChatMessage rows.",
            },
            {
                "id": "hive.local_agent.channel",
                "name": "Hive Connect local agent channel",
                "description": "Hive Connect local agents use authenticated user-level channel sessions.",
            },
        ],
        "hive": {
            "agent_id": agent_id,
            "tenant_id": _string_id(getattr(agent, "tenant_id", None)),
            "participant_id": _string_id(getattr(agent, "participant_id", None)),
            "sponsor_user_id": _string_id(getattr(agent, "sponsor_user_id", None)),
            "security_zone": getattr(agent, "security_zone", None),
            "agent_class": getattr(agent, "agent_class", None),
            "lifecycle_status": getattr(agent, "status", None),
            "deleted": getattr(agent, "deleted_at", None) is not None,
        },
    }


def build_interoperability_profile(*, base_url: str | None = None) -> dict[str, Any]:
    """Build Hive's platform-level interoperability profile."""
    base = _base_url(base_url)
    return {
        "platform": "hive",
        "base_url": base,
        "standards": {
            "mcp": {
                "status": "implemented",
                "transport": ["streamable_http", "sse"],
                "tool_discovery": "deferred_tool_search",
                "token_passthrough": "forbidden",
                "server_url_userinfo": "forbidden",
                "api_key_query": "extracted_to_authorization_header",
                "oauth_authorization_client": {
                    "status": "implemented",
                    "flow": "authorization_code",
                    "pkce": "S256",
                    "token_storage": "server_side_encrypted",
                    "agent_token_exposure": "forbidden",
                    "start_endpoint": "/api/v1/enterprise/mcp/{server_id}/oauth/start",
                    "callback_endpoint": "/api/v1/enterprise/mcp/oauth/callback",
                    "honesty_boundary": "Unit verified; live verification depends on a real OAuth MCP server configuration.",
                },
                "oauth_resource_server": {
                    "status": "not_exposed",
                    "missing": [
                        "RFC9728 metadata discovery",
                        "RFC8707 resource indicators",
                        "resource audience enforcement",
                    ],
                },
            },
            "a2a": {
                "status": "partial",
                "agent_card_endpoint": "/api/v1/agents/{agent_id}/a2a-card",
                "agent_to_agent_messages": "session_backed_internal_runtime",
                "collaboration_policy": "same_owner_or_public_or_active_a2a_collaboration_group",
                "collaborator_projection_endpoint": "/api/v1/agents/{agent_id}/a2a/collaborators",
                "json_rpc_tasks": {
                    "status": "not_exposed",
                    "reason": "Hive currently exposes durable internal agent messaging, not the public A2A task JSON-RPC surface.",
                },
            },
            "oauth_delegation": {
                "status": "not_exposed",
                "reason": "Tenant OIDC login exists; cross-app agent delegation grant is not exposed as a public interoperability endpoint.",
            },
        },
    }
