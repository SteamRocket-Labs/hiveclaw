"""Memory tools — agent-initiated memory read/write and cross-session search."""

from __future__ import annotations

import uuid

from app.services.session_recall import search_session_history
from app.tools.decorator import ToolMeta, tool


# -- save_memory ---------------------------------------------------------------


@tool(
    ToolMeta(
        name="save_memory",
        description=(
            "Persist a fact to your long-term memory so it is available in future conversations. "
            "This is the ONLY write path for durable memory — direct file edits under memory/ are refused.\n\n"
            "Use this tool when you encounter information worth remembering across sessions:\n"
            "- User corrections or preferences (category: feedback)\n"
            "- Important project decisions or deadlines (category: project)\n"
            "- Successful approaches worth reusing (category: strategy)\n"
            "- Approaches proven to fail (category: blocked_pattern)\n"
            "- Hard rules you must follow (category: constraint)\n"
            "- External system references, URLs, tool names (category: reference)\n"
            "- User role, knowledge, working style (category: user)\n\n"
            "Each fact should be a single, concise statement (under 200 chars is ideal).\n"
            "Do NOT store transient task state, raw tool output, or debugging logs.\n"
            "When the fact is promotion-lane evidence (a proven reusable method, an identity-level "
            "rule), pass container_candidate so the promotion lanes can find it later."
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
                        "user",
                        "feedback",
                        "project",
                        "reference",
                        "constraint",
                        "strategy",
                        "blocked_pattern",
                        "general",
                    ],
                    "description": "Memory category for retrieval prioritization.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional topic/subject tag for grouping related facts.",
                },
                "container_candidate": {
                    "type": "string",
                    "enum": [
                        "memory_append",
                        "soul_candidate",
                        "skill_candidate",
                        "workflow_candidate",
                        "artifact_only",
                    ],
                    "description": (
                        "Optional promotion-lane hint. Use skill_candidate / workflow_candidate for "
                        "proven reusable strategies, soul_candidate for repeated identity-level rules. "
                        "The promotion gates decide — this is evidence, not a command."
                    ),
                },
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional evidence pointers (T2 entry ids, artifact paths, session ids).",
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
    )
)
async def save_memory(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.memory.t3_store import append_t3_memory_candidate

    content = (arguments.get("content") or "").strip()
    if not content:
        return "[Error] content is required and cannot be empty."

    raw_refs = arguments.get("source_refs")
    source_refs = [str(ref).strip() for ref in raw_refs if str(ref).strip()] if isinstance(raw_refs, list) else []
    source_refs.append("tool:save_memory")

    result = await append_t3_memory_candidate(
        agent_id,
        category=arguments.get("category", "general"),
        content=content,
        source_refs=source_refs,
        proposed_by="agent_tool",
        container_candidate=arguments.get("container_candidate"),
    )

    if result.status == "rejected":
        return f"[Rejected] {result.sensitivity}: {result.reason}"

    if result.status == "duplicate" and result.similar:
        hit = result.similar
        ts = f" ({hit['timestamp']})" if hit.get("timestamp") else ""
        return (
            f"[Skipped] A similar memory already exists (similarity={hit['similarity']:.2f}):\n"
            f"  [{hit['category']}]{ts} {hit['content']}\n"
            f"If this new fact is intentionally distinct (different scope, newer value, "
            f"explicit correction), re-call save_memory with content that makes the "
            f"difference explicit (e.g. include the date or the delta)."
        )

    saved = content[:80]
    return f"Saved to long-term memory [{result.category}]: {saved}{'...' if len(content) > 80 else ''}"


# -- load_memory ---------------------------------------------------------------


@tool(
    ToolMeta(
        name="load_memory",
        description=(
            "Load full long-term memory entries by ID after search_memory or the prompt memory index returns IDs.\n\n"
            "Use this before relying on an indexed/preview-only memory entry. Supports batch IDs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Memory entry IDs returned by search_memory or the prompt memory index.",
                }
            },
            "required": ["ids"],
        },
        category="memory",
        display_name="Load Memory",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
def load_memory(agent_id: uuid.UUID, arguments: dict) -> str:
    from pathlib import Path

    from app.config import get_settings
    from app.memory.md_store import load_t3_entries_by_ids

    raw_ids = arguments.get("ids") or []
    if isinstance(raw_ids, str):
        ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    else:
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    ids = ids[:20]
    if not ids:
        return "[Error] ids is required and cannot be empty."

    settings = get_settings()
    entries = load_t3_entries_by_ids(Path(settings.AGENT_DATA_DIR), agent_id, ids)
    if not entries:
        return f"No memory entries found for ids: {', '.join(ids)}"

    found_ids = {entry.entry_id for entry in entries}
    lines = ["## Loaded Memory"]
    for entry in entries:
        ts = f" timestamp={entry.timestamp}" if entry.timestamp else ""
        lines.append(f"- id={entry.entry_id} source={entry.source} category={entry.category}{ts}")
        lines.append(f"  {entry.content}")
    missing = [entry_id for entry_id in ids if entry_id not in found_ids]
    if missing:
        lines.append("")
        lines.append(f"Missing ids: {', '.join(missing)}")
    return "\n".join(lines)


# -- search_memory -------------------------------------------------------------


@tool(
    ToolMeta(
        name="search_memory",
        description=(
            "Search your long-term memory and past session history.\n\n"
            "Use this tool when you need to recall:\n"
            "- What a user told you in a previous conversation\n"
            "- Decisions, preferences, or constraints from past sessions\n"
            "- Strategies that worked or approaches that failed\n"
            "- Any fact you saved previously with save_memory\n\n"
            "Returns matching fact IDs/previews and recalled session snippets ranked by relevance. "
            "Call load_memory(ids=[...]) to expand preview-only fact results."
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
    )
)
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
            Path(settings.AGENT_DATA_DIR),
            agent_id,
            query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )
        backend_facts = await _search_semantic_backend_facts(
            agent_id,
            tenant_id=tenant_id,
            query=query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            md_facts=facts,
        )
        if backend_facts:
            facts = _dedupe_fact_results([*backend_facts, *facts])[:limit]
        if facts:
            results.append("## Semantic Memory")
            for f in facts:
                entry_id = f.get("id", "")
                cat = f.get("category", "general")
                preview = f.get("preview") or f.get("content", "")
                ts = f.get("timestamp", "")
                ts_display = f" ({ts[:10]})" if ts else ""
                source = f.get("source", "")
                source_display = f" source={source}" if source else ""
                id_display = f"id={entry_id} " if entry_id else ""
                load_hint = f' load_memory(ids=["{entry_id}"])' if entry_id else ""
                results.append(f"- {id_display}[{cat}]{ts_display}{source_display} {preview}{load_hint}")

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


async def _search_semantic_backend_facts(
    agent_id: uuid.UUID,
    *,
    tenant_id: str | None,
    query: str,
    limit: int,
    date_from: str | None,
    date_to: str | None,
    md_facts: list[dict],
) -> list[dict]:
    if not tenant_id:
        return []
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except (TypeError, ValueError):
        return []
    try:
        from app.memory.backend import MDBackend, get_memory_backend
        from app.memory.hindsight_sync import LOOKUP_FAILED, _fetch_tenant_backend_pref

        pref = await _fetch_tenant_backend_pref(tenant_uuid)
        if pref is LOOKUP_FAILED:
            return []
        backend = get_memory_backend(tenant_id=tenant_uuid, tenant_backend_pref=pref)
        if isinstance(backend, MDBackend):
            return []
        scored = await backend.search(
            agent_id,
            query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception:
        return []

    md_by_content = {_normalize_fact_content(fact.get("content", "")): fact for fact in md_facts}
    facts: list[dict] = []
    for item in scored:
        content = (item.content or "").strip()
        if not content:
            continue
        matched = md_by_content.get(_normalize_fact_content(content))
        if matched:
            fact = dict(matched)
        else:
            fact = {
                "content": content,
                "preview": content[:160],
                "category": item.category or "general",
                "timestamp": item.timestamp or "",
                "source": "hindsight",
            }
        fact["semantic_backend"] = "hindsight"
        fact["semantic_score"] = item.score
        facts.append(fact)
    return facts


def _dedupe_fact_results(facts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for fact in facts:
        key = str(fact.get("id") or _normalize_fact_content(fact.get("content", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


def _normalize_fact_content(content: object) -> str:
    return " ".join(str(content or "").lower().split())
