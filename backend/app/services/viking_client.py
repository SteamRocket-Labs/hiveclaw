"""OpenViking HTTP client — thin async wrapper around the OpenViking REST API.

Maps Hive's multi-tenancy onto OpenViking's identity model:
  account_id = tenant_id
  user_id = user_id
  agent_id = agent_id

All methods gracefully degrade to empty results if OpenViking is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient | None:
    """Get or create the HTTP client. Returns None if OpenViking is not configured."""
    global _client
    if not settings.OPENVIKING_URL:
        return None
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.OPENVIKING_URL,
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Authorization": f"Bearer {settings.OPENVIKING_API_KEY}"} if settings.OPENVIKING_API_KEY else {},
        )
    return _client


def _identity_headers(tenant_id: str, user_id: str | None = None, agent_id: str | None = None) -> dict[str, str]:
    """Build OpenViking identity headers from Hive entities."""
    headers = {"X-OpenViking-Account": tenant_id}
    if user_id:
        headers["X-OpenViking-User"] = user_id
    if agent_id:
        headers["X-OpenViking-Agent"] = agent_id
    return headers


async def find(
    query: str,
    target_uri: str = "viking://resources/",
    *,
    tenant_id: str,
    agent_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Semantic search across knowledge base. Returns list of matching context items."""
    client = _get_client()
    if not client:
        return []
    try:
        resp = await client.post(
            "/api/v1/search/find",
            json={"query": query, "target_uri": target_uri, "limit": limit},
            headers=_identity_headers(tenant_id, agent_id=agent_id),
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        logger.warning("OpenViking find failed: %s", e)
        return []


async def add_resource(
    content: str,
    to: str,
    *,
    tenant_id: str,
    agent_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Add a resource to the knowledge base."""
    client = _get_client()
    if not client:
        return {"error": "OpenViking not configured"}
    try:
        resp = await client.post(
            "/api/v1/resources",
            json={"content": content, "to": to, "reason": reason},
            headers=_identity_headers(tenant_id, agent_id=agent_id),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("OpenViking add_resource failed: %s", e)
        return {"error": str(e)}


async def read(uri: str, *, tenant_id: str, agent_id: str | None = None) -> str:
    """Read content from a viking:// URI."""
    client = _get_client()
    if not client:
        return ""
    try:
        resp = await client.get(
            "/api/v1/fs/read",
            params={"uri": uri},
            headers=_identity_headers(tenant_id, agent_id=agent_id),
        )
        resp.raise_for_status()
        return resp.json().get("content", "")
    except Exception as e:
        logger.warning("OpenViking read failed for %s: %s", uri, e)
        return ""


async def abstract(uri: str, *, tenant_id: str) -> str:
    """Get L0 abstract (1-2 sentence summary) of a resource."""
    client = _get_client()
    if not client:
        return ""
    try:
        resp = await client.get(
            "/api/v1/fs/abstract",
            params={"uri": uri},
            headers=_identity_headers(tenant_id),
        )
        resp.raise_for_status()
        return resp.json().get("abstract", "")
    except Exception as e:
        logger.warning("OpenViking abstract failed for %s: %s", uri, e)
        return ""


async def overview(uri: str, *, tenant_id: str) -> str:
    """Get L1 overview (500-1000 char description) of a resource."""
    client = _get_client()
    if not client:
        return ""
    try:
        resp = await client.get(
            "/api/v1/fs/overview",
            params={"uri": uri},
            headers=_identity_headers(tenant_id),
        )
        resp.raise_for_status()
        return resp.json().get("overview", "")
    except Exception as e:
        logger.warning("OpenViking overview failed for %s: %s", uri, e)
        return ""


async def tree(uri: str = "viking://resources/", *, tenant_id: str) -> dict[str, Any]:
    """List hierarchy with abstracts."""
    client = _get_client()
    if not client:
        return {}
    try:
        resp = await client.get(
            "/api/v1/fs/tree",
            params={"uri": uri, "output": "agent"},
            headers=_identity_headers(tenant_id),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("OpenViking tree failed for %s: %s", uri, e)
        return {}


async def close() -> None:
    """Close the HTTP client."""
    global _client
    if _client:
        await _client.aclose()
        _client = None


def is_configured() -> bool:
    """Check if OpenViking is configured."""
    return bool(settings.OPENVIKING_URL)
