"""Memory tools — agent-initiated memory read/write and cross-session search."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.services.session_recall import search_session_history
from app.tools.decorator import ToolMeta, tool


# -- save_memory ---------------------------------------------------------------

@tool(ToolMeta(
    name="save_memory",
    description=(
        "Persist a fact to your long-term memory so it is available in future conversations.\n\n"
        "Use this tool when you encounter information worth remembering across sessions:\n"
        "- User corrections or preferences (category: feedback)\n"
        "- Important project decisions or deadlines (category: project)\n"
        "- Successful approaches worth reusing (category: strategy)\n"
        "- Approaches proven to fail (category: blocked_pattern)\n"
        "- Hard rules you must follow (category: constraint)\n"
        "- External system references, URLs, tool names (category: reference)\n"
        "- User role, knowledge, working style (category: user)\n\n"
        "Each fact should be a single, concise statement (under 200 chars is ideal).\n"
        "Do NOT store transient task state, raw tool output, or debugging logs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember. Keep concise and durable.",
            },
            "category": {
                "type": "string",
                "enum": [
                    "user", "feedback", "project", "reference",
                    "constraint", "strategy", "blocked_pattern", "general",
                ],
                "description": "Memory category for retrieval prioritization.",
            },
            "subject": {
                "type": "string",
                "description": "Optional topic/subject tag for grouping related facts.",
            },
        },
        "required": ["content", "category"],
    },
    category="memory",
    display_name="Save Memory",
    icon="\U0001f9e0",
    read_only=False,
    parallel_safe=False,
    governance="sensitive",
    adapter="agent_args",
))
def save_memory(agent_id: uuid.UUID, arguments: dict) -> str:
    from pathlib import Path

    from app.config import get_settings
    from app.memory.md_store import (
        MEMORY_DEDUP_THRESHOLD,
        append_t3_entry,
        find_similar_t3_entries,
    )
    from app.memory.types import MEMORY_CATEGORIES

    content = (arguments.get("content") or "").strip()
    if not content:
        return "[Error] content is required and cannot be empty."

    category = arguments.get("category", "general")
    if category not in MEMORY_CATEGORIES:
        category = "general"

    settings = get_settings()
    data_root = Path(settings.AGENT_DATA_DIR)

    # Semantic near-dedup: reject paraphrases of an already-saved fact so
    # T3 does not accumulate "用户喜欢简短回复" / "偏好简短的回复" twice.
    similar = find_similar_t3_entries(
        data_root,
        agent_id,
        content=content[:2000],
        category=category,
        threshold=MEMORY_DEDUP_THRESHOLD,
        limit=1,
    )
    if similar:
        hit = similar[0]
        ts = f" ({hit['timestamp']})" if hit.get("timestamp") else ""
        return (
            f"[Skipped] A similar memory already exists (similarity={hit['similarity']:.2f}):\n"
            f"  [{hit['category']}]{ts} {hit['content']}\n"
            f"If this new fact is intentionally distinct (different scope, newer value, "
            f"explicit correction), re-call save_memory with content that makes the "
            f"difference explicit (e.g. include the date or the delta)."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    append_t3_entry(
        data_root,
        agent_id,
        category=category,
        content=content[:2000],
        timestamp=timestamp,
    )

    return f"Saved to long-term memory [{category}]: {content[:80]}{'...' if len(content) > 80 else ''}"


# -- search_memory -------------------------------------------------------------

@tool(ToolMeta(
    name="search_memory",
    description=(
        "Search your long-term memory and past session history.\n\n"
        "Use this tool when you need to recall:\n"
        "- What a user told you in a previous conversation\n"
        "- Decisions, preferences, or constraints from past sessions\n"
        "- Strategies that worked or approaches that failed\n"
        "- Any fact you saved previously with save_memory\n\n"
        "Returns matching facts and recalled session snippets ranked by relevance."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords or phrase to find in memory.",
            },
            "scope": {
                "type": "string",
                "enum": ["facts", "sessions", "all"],
                "description": "Search scope: 'facts' (semantic memory only), 'sessions' (past conversation recall), 'all' (both). Default: all.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return. Default: 10.",
            },
            "date_from": {
                "type": "string",
                "description": "Only include facts on or after this date (YYYY-MM-DD). Use get_current_time to resolve relative dates like 'last month'.",
            },
            "date_to": {
                "type": "string",
                "description": "Only include facts on or before this date (YYYY-MM-DD).",
            },
        },
        "required": ["query"],
    },
    category="memory",
    display_name="Search Memory",
    icon="\U0001f50d",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    adapter="agent_args",
))
async def search_memory(agent_id: uuid.UUID, arguments: dict, tenant_id: str | None = None) -> str:
    from pathlib import Path

    from app.config import get_settings
    from app.memory.md_store import search_t3_facts

    query = (arguments.get("query") or "").strip()
    if not query:
        return "[Error] query is required."

    scope = arguments.get("scope", "all")
    limit = min(int(arguments.get("limit", 10)), 20)
    date_from = (arguments.get("date_from") or "").strip() or None
    date_to = (arguments.get("date_to") or "").strip() or None
    results: list[str] = []

    settings = get_settings()

    # --- Semantic facts search ---
    if scope in ("facts", "all"):
        facts = search_t3_facts(
            Path(settings.AGENT_DATA_DIR), agent_id, query,
            limit=limit, date_from=date_from, date_to=date_to,
        )
        if facts:
            results.append("## Semantic Memory")
            for f in facts:
                cat = f.get("category", "general")
                content = f.get("content", "")
                ts = f.get("timestamp", "")
                ts_display = f" ({ts[:10]})" if ts else ""
                results.append(f"- [{cat}]{ts_display} {content}")

    # --- Cross-session recall ---
    if scope in ("sessions", "all"):
        try:
            tenant_uuid = uuid.UUID(str(tenant_id)) if tenant_id else None
            recalled = await search_session_history(
                agent_id,
                query,
                limit=limit,
                snippet_limit=3,
                tenant_id=tenant_uuid,
            )
            if recalled:
                results.append("## Session Recall")
                for hit in recalled:
                    ts = hit.get("started_at", "?")
                    source = hit.get("source", "unknown")
                    headline = hit.get("headline", "past session")
                    results.append(f"- ({ts} [{source}]) {headline}")
                    focused_recap = (hit.get("focused_recap") or "").strip()
                    if focused_recap:
                        results.append(f"  Recap: {focused_recap}")
                    summary = (hit.get("summary") or "").strip()
                    if summary and summary != focused_recap:
                        results.append(f"  Summary: {summary}")
                    evidence_lines = hit.get("evidence_lines") or []
                    if evidence_lines:
                        results.append("  Evidence:")
                        for line in evidence_lines:
                            cleaned = line.strip()
                            if cleaned:
                                results.append(f"    {cleaned}")
                    transcript_window = (hit.get("transcript_window") or "").strip()
                    if transcript_window:
                        results.append("  Context:")
                        for line in transcript_window.splitlines():
                            cleaned = line.strip()
                            if cleaned:
                                results.append(f"    {cleaned}")
                    display_snippets = hit.get("context_snippets") or hit.get("snippets", [])
                    for snippet in display_snippets:
                        results.append(f"  - {snippet}")
        except Exception as exc:
            results.append(f"## Session Recall\n- [Search error: {exc}]")

    if not results:
        return f"No memory found for query: {query}"

    return "\n".join(results)
