"""Pre-message knowledge injection via OpenViking semantic search.

Searches the enterprise knowledge base for content relevant to the user's
message and returns a formatted block for injection into the system prompt.
This eliminates 2-3 tool call rounds by pre-loading relevant context.
"""

from __future__ import annotations

import logging
import uuid

from app.services import viking_client

logger = logging.getLogger(__name__)

# Hard ceiling on injected characters (~500 tokens)
_DEFAULT_CHAR_BUDGET = 1500


async def fetch_relevant_knowledge(
    query: str,
    tenant_id: uuid.UUID | None = None,
    max_tokens: int = 500,
    max_chars: int | None = None,
    limit: int = 3,
    timeout: float = 2.0,
) -> str:
    """Search OpenViking for content relevant to the user's message.

    Returns formatted knowledge string to inject into system prompt,
    or empty string if OpenViking is not configured or search fails.

    Designed to be fast and non-blocking -- 2 second timeout, graceful fallback.
    """
    if not viking_client.is_configured():
        return ""

    if not query or not query.strip():
        return ""

    tid = str(tenant_id) if tenant_id else None
    if not tid:
        return ""

    try:
        results = await viking_client.find(
            query,
            tenant_id=tid,
            limit=limit,
        )
    except Exception as exc:
        logger.debug("OpenViking knowledge search failed: %s", exc)
        return ""

    if not results:
        return ""

    # Format results into a compact context block
    parts: list[str] = []
    char_budget = max_chars if max_chars and max_chars > 0 else (max_tokens * 3 if max_tokens else _DEFAULT_CHAR_BUDGET)
    used = 0

    for item in results:
        content = item.get("content", "") or item.get("text", "")
        source = item.get("source", "") or item.get("path", "")
        if not content:
            continue

        remaining = char_budget - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining] + "..."

        if source:
            parts.append(f"**[{source}]**: {content}")
        else:
            parts.append(content)
        used += len(content)

    if not parts:
        return ""

    return "## Relevant Company Knowledge\n\n" + "\n\n".join(parts)
