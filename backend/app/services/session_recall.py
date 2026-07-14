"""Cross-session recall over the append-only T0 session ledger with DB fallback."""

from __future__ import annotations

import re
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import String as SaString, cast as sa_cast, or_, select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.memory.t0.ledger import replay_t0_session_events
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

_FALLBACK_HEADLINE = "回顾到与查询相关的历史会话"
_EXCLUDED_CHANNELS = {"agent", "heartbeat", "trigger", "task", "dream"}
_EXCLUDED_ROLES = {"system", "tool_call"}
_QUERY_SPLIT_RE = re.compile(r"\s+")
logger = logging.getLogger(__name__)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def _query_terms(query: str) -> list[str]:
    normalized = _normalize_text(query)
    terms = [term for term in _QUERY_SPLIT_RE.split(normalized) if term]
    if normalized and normalized not in terms:
        terms.insert(0, normalized)
    return terms


def _match_score(text: str, terms: list[str]) -> int:
    haystack = _normalize_text(text)
    if not haystack or not terms:
        return 0
    phrase = terms[0]
    if phrase and phrase in haystack:
        return 100 + max(len(terms) - 1, 0)
    return sum(1 for term in terms[1:] if term in haystack)


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    parts = raw.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, raw
    _, remainder = parts
    frontmatter_lines = raw.splitlines()[1:]
    metadata: dict[str, str] = {}
    for line in frontmatter_lines:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, remainder


def _clean_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = cleaned.replace("**User**:", "").replace("**Agent**:", "").strip()
    cleaned = cleaned.removeprefix("User:").removeprefix("Assistant:").removeprefix("Agent:").strip()
    cleaned = cleaned.replace("**Tools**:", "Tools:")
    return re.sub(r"\s+", " ", cleaned).strip()


def _body_lines(body: str) -> list[str]:
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = _clean_line(raw_line)
        if not line or line.startswith("## Turn"):
            continue
        lines.append(line)
    return lines


def _speaker_label(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized == "user":
        return "User"
    if normalized in {"assistant", "agent"}:
        return "Assistant"
    if normalized == "tool":
        return "Tool"
    return role.strip().title() or "Message"


def _normalize_transcript_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("**User**:"):
        return f"User: {_clean_line(stripped)}"
    if stripped.startswith("**Agent**:"):
        return f"Assistant: {_clean_line(stripped)}"
    if stripped.startswith("**Assistant**:"):
        return f"Assistant: {_clean_line(stripped)}"
    return re.sub(r"\s+", " ", stripped).strip()


def _transcript_lines_from_body(body: str) -> list[str]:
    lines: list[str] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("## Turn"):
            continue
        normalized = _normalize_transcript_line(stripped)
        if normalized:
            lines.append(normalized)
    return lines


def _transcript_lines_from_messages(messages: list[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for role, content in messages:
        cleaned = re.sub(r"\s+", " ", (content or "").strip())
        if not cleaned:
            continue
        lines.append(f"{_speaker_label(role)}: {cleaned}")
    return lines


def _extract_snippets(body: str, query: str, *, snippet_limit: int) -> list[str]:
    terms = _query_terms(query)
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in _body_lines(body):
        score = _match_score(line, terms)
        if score <= 0:
            continue
        if line in seen:
            continue
        seen.add(line)
        candidates.append((score, line))

    if not candidates:
        return []

    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return [line for _score, line in candidates[:snippet_limit]]


def _extract_context_snippets_from_lines(
    lines: list[str],
    query: str,
    *,
    snippet_limit: int,
    context_radius: int = 1,
) -> list[str]:
    terms = _query_terms(query)
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        score = _match_score(line, terms)
        if score <= 0:
            continue
        start = max(0, index - context_radius)
        end = min(len(lines), index + context_radius + 1)
        context_lines = [lines[pos] for pos in range(start, end) if lines[pos]]
        snippet = " | ".join(context_lines).strip()
        if not snippet or snippet in seen:
            continue
        seen.add(snippet)
        candidates.append((score, snippet))

    if not candidates:
        return []

    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return [snippet for _score, snippet in candidates[:snippet_limit]]


def _extract_transcript_window_from_lines(
    lines: list[str],
    query: str,
    *,
    context_radius: int = 1,
    max_lines: int = 6,
) -> str:
    terms = _query_terms(query)
    match_indexes = [index for index, line in enumerate(lines) if _match_score(line, terms) > 0]
    if not match_indexes:
        return ""

    selected_indexes: list[int] = []
    seen_indexes: set[int] = set()
    for index in match_indexes:
        start = max(0, index - context_radius)
        end = min(len(lines), index + context_radius + 1)
        for candidate in range(start, end):
            if candidate in seen_indexes:
                continue
            seen_indexes.add(candidate)
            selected_indexes.append(candidate)
            if len(selected_indexes) >= max_lines:
                break
        if len(selected_indexes) >= max_lines:
            break

    window_lines = [lines[index] for index in selected_indexes]
    return "\n".join(window_lines).strip()


def _extract_context_snippets(
    body: str,
    query: str,
    *,
    snippet_limit: int,
    context_radius: int = 1,
) -> list[str]:
    return _extract_context_snippets_from_lines(
        _body_lines(body),
        query,
        snippet_limit=snippet_limit,
        context_radius=context_radius,
    )


def _select_evidence_lines(
    transcript_lines: list[str],
    query: str,
    *,
    max_lines: int | None = None,
) -> list[str]:
    """Return complete mechanical evidence; semantic selection belongs to the model."""
    del query, max_lines
    return [line.strip() for line in transcript_lines if line.strip()]


def _build_focused_recap(
    *,
    headline: str,
    evidence_lines: list[str],
    fallback_summary: str,
) -> str:
    del headline
    evidence = "\n".join(line for line in evidence_lines if line.strip()).strip()
    if evidence:
        return f"Evidence passthrough:\n{evidence}"
    return fallback_summary or _FALLBACK_HEADLINE


def _annotate_recall_hit(
    hit: dict,
    *,
    query: str,
    transcript_lines: list[str],
    headline: str,
) -> dict:
    transcript_window = _extract_transcript_window_from_lines(transcript_lines, query)
    context_snippets = hit.get("context_snippets") or []
    snippets = hit.get("snippets") or []
    evidence_lines = _select_evidence_lines(transcript_lines, query)
    transcript = "\n".join(transcript_lines).strip()
    evidence_passthrough = (
        transcript or "\n".join(context_snippets or snippets).strip() or headline or _FALLBACK_HEADLINE
    )

    hit["headline"] = headline
    hit["transcript"] = transcript
    hit["transcript_window"] = transcript_window
    hit["summary"] = evidence_passthrough
    hit["summary_method"] = "evidence_passthrough"
    hit["summary_model_status"] = "not_requested"
    hit["evidence_lines"] = evidence_lines
    hit["focused_recap"] = _build_focused_recap(
        headline=headline,
        evidence_lines=evidence_lines,
        fallback_summary=evidence_passthrough,
    )
    return hit


async def _summarize_recall_hits(
    query: str,
    hits: list[dict],
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None = None,
) -> list[dict]:
    """Optionally enrich recall hits with a focused summary model.

    Retrieval remains transcript-first. If the model is unavailable, the full
    evidence passthrough remains visible and failure status is explicit; the
    platform never authors a semantic fallback recap.
    """
    if not hits:
        return hits
    if tenant_id is None:
        for hit in hits:
            hit["summary_model_status"] = "no_tenant"
        return hits

    client = None
    try:
        from app.services.llm_client import (
            LLMMessage,
            create_llm_client_from_config,
            get_max_tokens,
            with_llm_usage_context,
        )
        from app.services.memory_service import _get_summary_model_config

        model_config = await _get_summary_model_config(tenant_id)
        if not model_config:
            for hit in hits:
                hit["summary_model_status"] = "no_model"
            return hits

        output_tokens = get_max_tokens(
            str(model_config.get("provider") or ""),
            str(model_config.get("model") or ""),
            model_config.get("max_output_tokens"),
        )

        client = create_llm_client_from_config(
            with_llm_usage_context(
                model_config,
                source="session_recall",
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
        )
        for hit in hits:
            try:
                evidence = hit.get("context_snippets") or hit.get("snippets") or []
                complete_transcript = (hit.get("transcript") or "").strip()
                transcript_window = (hit.get("transcript_window") or "").strip()
                evidence_lines = hit.get("evidence_lines") or []
                evidence_block = "\n".join(f"- {snippet}" for snippet in evidence if snippet)
                if transcript_window:
                    evidence_block = f"{evidence_block}\nTranscript window:\n{transcript_window}".strip()
                if evidence_lines:
                    evidence_block = (
                        f"{evidence_block}\nKey evidence:\n" + "\n".join(f"- {line}" for line in evidence_lines if line)
                    ).strip()
                if complete_transcript:
                    evidence_block = f"Complete transcript:\n{complete_transcript}\n\n{evidence_block}".strip()
                if not evidence_block:
                    continue

                prompt = (
                    "<role>\n"
                    "You are the session-recall summarizer. Given a user's current query\n"
                    "and one candidate past session, produce a 1-2 sentence recap that\n"
                    "helps the user decide whether this past session is worth reopening.\n"
                    "You are NOT synthesizing memory for long-term storage — this summary\n"
                    "is shown inline next to the recall hit in the UI.\n"
                    "</role>\n\n"
                    "<summary_rules>\n"
                    "- Exactly 1-2 sentences. No more.\n"
                    "- Lead with what was DECIDED or PRODUCED in that session.\n"
                    "- Name concrete artifacts: file paths, commit hashes, ticket IDs,\n"
                    "  URLs, tool results — whatever the evidence shows.\n"
                    "- Use ONLY the provided evidence block + headline + evidence passthrough.\n"
                    "  Do not infer, extrapolate, or fabricate details not present.\n"
                    "- Match the query's language (English query → English summary,\n"
                    "  Chinese → Chinese).\n"
                    "</summary_rules>\n\n"
                    "<good_examples>\n"
                    "Query: `我们上次是怎么处理 auth token 的？`\n"
                    "Evidence: `- middleware.py:142 reordered refresh before header write\\n- pytest: 24 passed`\n"
                    "Good summary: `重排了 middleware.py:142 的 refresh 顺序，24 个 auth 测试通过。`\n\n"
                    "Query: `What did we decide about the payment retry strategy?`\n"
                    "Evidence: `- PR #482: exponential backoff (1s, 2s, 4s, max 3 tries)\\n- Stripe webhook tested`\n"
                    "Good summary: `Adopted exponential backoff (1s → 4s, max 3 retries) in PR #482; Stripe webhook verified.`\n"
                    "</good_examples>\n\n"
                    "<bad_examples>\n"
                    "❌ `The user asked about auth and we looked at it.` (no outcome, no artifact)\n"
                    "❌ `Fixed several bugs and improved the system.` (vague; no decision; no artifact)\n"
                    "❌ `We probably discussed the retry logic and chose a reasonable approach.`\n"
                    "   (speculative — evidence doesn't say 'probably')\n"
                    "❌ 5-sentence paragraph summarizing every detail. (length cap violated)\n"
                    "</bad_examples>\n\n"
                    "<input>\n"
                    f"Query: {query}\n"
                    f"Headline: {hit.get('headline', _FALLBACK_HEADLINE)}\n"
                    f"Evidence passthrough: {hit.get('focused_recap', '')}\n"
                    f"Evidence:\n{evidence_block}\n"
                    "</input>\n\n"
                    "<output_contract>\n"
                    "Respond with ONLY the 1-2 sentence summary. No prefix, no quotes,\n"
                    "no markdown formatting, no meta-commentary about the query.\n"
                    "</output_contract>"
                )
                response = await client.stream(
                    messages=[LLMMessage(role="user", content=prompt)],
                    max_tokens=output_tokens,
                    temperature=0.1,
                )
                summary = (response.content or "").strip()
                if not summary:
                    raise RuntimeError("session recall summary model returned empty output")
                hit["summary"] = summary
                hit["focused_recap"] = summary
                hit["summary_method"] = "model"
                hit["summary_model_status"] = "completed"
                hit.pop("summary_model_error_class", None)
            except Exception as exc:  # noqa: BLE001 - keep complete evidence for this hit
                logger.exception("Session recall model summary failed")
                hit["summary_model_status"] = "failed"
                hit["summary_model_error_class"] = type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - setup failure is observable on every hit
        logger.exception("Session recall summary model setup failed")
        for hit in hits:
            hit["summary_model_status"] = "failed"
            hit["summary_model_error_class"] = type(exc).__name__
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Session recall summary client close failed: %s", exc)

    return hits


def _infer_headline(body: str, query: str, *, fallback: str) -> str:
    snippets = _extract_snippets(body, query, snippet_limit=1)
    if snippets:
        return snippets[0]
    for line in _body_lines(body):
        if line:
            return line
    return fallback


def _started_label(value: str | None) -> str:
    if not value:
        return "?"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def _unpack_match_row(row: tuple) -> tuple[object, str | None, object, str | None, str | None, str | None, object]:
    if len(row) == 7:
        return row
    if len(row) == 6:
        session_id, source, started_at, summary, content, created_at = row
        return session_id, source, started_at, summary, "unknown", content, created_at
    raise ValueError(f"Unexpected session recall match row shape: {len(row)}")


def _unpack_transcript_row(row: tuple) -> tuple[str, str | None, str | None, object]:
    if len(row) == 4:
        conversation_id, role, content, created_at = row
        return str(conversation_id), role, content, created_at
    if len(row) == 7:
        conversation_id, _source, _started_at, _summary, role, content, created_at = row
        return str(conversation_id), role, content, created_at
    if len(row) == 6:
        conversation_id, _source, _started_at, _summary, content, created_at = row
        return str(conversation_id), "unknown", content, created_at
    raise ValueError(f"Unexpected session transcript row shape: {len(row)}")


def _t0_log_root(agent_id: uuid.UUID) -> Path:
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "logs"


def _t0_session_root(agent_id: uuid.UUID) -> Path:
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / "t0" / "sessions"


def _search_t0_session_ledger(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int | None,
    snippet_limit: int,
) -> list[dict]:
    sessions_root = _t0_session_root(agent_id)
    if not sessions_root.exists():
        return []
    data_root = Path(get_settings().AGENT_DATA_DIR)

    terms = _query_terms(query)
    hits: list[dict] = []

    for session_dir in sorted((path for path in sessions_root.iterdir() if path.is_dir()), reverse=True):
        session_id = session_dir.name
        events = [
            event
            for event in replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=data_root)
            if event.event_type != "segment_boundary" and event.content.strip()
        ]
        if not events:
            continue
        source = next((event.source for event in events if event.source), "unknown")
        if source in _EXCLUDED_CHANNELS:
            continue
        transcript_messages = [(event.role or "unknown", event.content) for event in events]
        transcript_lines = _transcript_lines_from_messages(transcript_messages)
        body = "\n".join(transcript_lines)
        body_score = _match_score(body, terms)
        headline_score = _match_score(session_id, terms)
        if max(body_score, headline_score) <= 0:
            continue

        started_at = _started_label(events[0].created_at)
        headline = _infer_headline(body, query, fallback=_FALLBACK_HEADLINE)
        hit = {
            "session_id": session_id,
            "source": source,
            "started_at": started_at,
            "headline": headline,
            "snippets": _extract_snippets(body, query, snippet_limit=snippet_limit),
            "context_snippets": _extract_context_snippets_from_lines(
                transcript_lines,
                query,
                snippet_limit=snippet_limit,
            ),
            "_score": max(body_score, headline_score),
        }
        _annotate_recall_hit(hit, query=query, transcript_lines=transcript_lines, headline=headline)
        hits.append(hit)

    hits.sort(key=lambda item: (-item["_score"], item["started_at"], item["session_id"]))
    for hit in hits:
        hit.pop("_score", None)
    return hits if limit is None else hits[:limit]


def _search_t0_chat_logs(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int | None,
    snippet_limit: int,
) -> list[dict]:
    """Search legacy chat logs after the new session ledger misses."""
    logs_root = _t0_log_root(agent_id)
    if not logs_root.exists():
        return []

    terms = _query_terms(query)
    grouped: dict[str, dict] = {}

    for path in sorted(logs_root.rglob("chat-*.md"), reverse=True):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata, body = _split_frontmatter(raw)
        if metadata.get("type") != "chat":
            continue

        body_score = _match_score(body, terms)
        headline_score = _match_score(metadata.get("session_id", ""), terms)
        if max(body_score, headline_score) <= 0:
            continue

        session_key = metadata.get("session_id") or path.stem
        group = grouped.setdefault(
            session_key,
            {
                "session_id": session_key,
                "source": metadata.get("source", "unknown"),
                "started_at": _started_label(metadata.get("started")),
                "headline": _FALLBACK_HEADLINE,
                "snippets": [],
                "_score": 0,
                "_bodies": [],
            },
        )
        group["_score"] = max(group["_score"], body_score, headline_score)
        group["_bodies"].append(body)

    if not grouped:
        return []

    hits: list[dict] = []
    for group in grouped.values():
        merged_body = "\n".join(group.pop("_bodies"))
        transcript_lines = _transcript_lines_from_body(merged_body)
        headline = _infer_headline(merged_body, query, fallback=_FALLBACK_HEADLINE)
        group["snippets"] = _extract_snippets(merged_body, query, snippet_limit=snippet_limit)
        group["context_snippets"] = _extract_context_snippets(merged_body, query, snippet_limit=snippet_limit)
        _annotate_recall_hit(group, query=query, transcript_lines=transcript_lines, headline=headline)
        hits.append(group)

    hits.sort(key=lambda item: (-item["_score"], item["started_at"], item["session_id"]))
    for hit in hits:
        hit.pop("_score", None)
    return hits if limit is None else hits[:limit]


async def _search_session_history_db(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int | None,
    snippet_limit: int,
    tenant_id: uuid.UUID | None = None,
) -> list[dict]:
    needle = (query or "").strip()
    if not needle:
        return []

    fetch_limit = max(limit * max(snippet_limit, 1) * 4, 20) if limit is not None else None
    pattern = f"%{needle}%"

    stmt = (
        select(
            ChatSession.id,
            ChatSession.source_channel,
            ChatSession.created_at,
            ChatSession.summary,
            ChatMessage.role,
            ChatMessage.content,
            ChatMessage.created_at,
        )
        .join(ChatMessage, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
        .where(
            ChatSession.agent_id == agent_id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.role.notin_(_EXCLUDED_ROLES),
            ChatSession.listed_surface == "chat",
            ChatSession.source_channel.notin_(_EXCLUDED_CHANNELS),
            or_(
                ChatMessage.content.ilike(pattern),
                ChatSession.summary.ilike(pattern),
            ),
        )
        .order_by(ChatSession.last_message_at.desc(), ChatMessage.created_at.asc())
    )
    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)

    async with tenant_scoped_session(tenant_id) as db:
        rows = (await db.execute(stmt)).all()
        if not rows:
            return []

        grouped: OrderedDict[str, dict] = OrderedDict()
        for row in rows:
            session_id, source, started_at, summary, role, content, _message_created_at = _unpack_match_row(row)
            key = str(session_id)
            group = grouped.get(key)
            if group is None:
                group = {
                    "session_id": key,
                    "source": source or "unknown",
                    "started_at": started_at.strftime("%Y-%m-%d") if started_at else "?",
                    "headline": (summary or "").strip() or _FALLBACK_HEADLINE,
                    "snippets": [],
                    "context_snippets": [],
                    "transcript_window": "",
                    "summary": "",
                    "_matched_messages": [],
                }
                grouped[key] = group

            text = (content or "").strip()
            if text and (role, text) not in group["_matched_messages"]:
                group["_matched_messages"].append((role or "unknown", text))
            if text and text not in group["snippets"] and len(group["snippets"]) < snippet_limit:
                group["snippets"].append(text)

            if (
                limit is not None
                and len(grouped) >= limit
                and all(len(item["snippets"]) >= snippet_limit for item in grouped.values())
            ):
                break

        session_ids = list(grouped.keys())
        if limit is not None:
            session_ids = session_ids[:limit]
        transcript_stmt = (
            select(
                ChatMessage.conversation_id,
                ChatMessage.role,
                ChatMessage.content,
                ChatMessage.created_at,
            )
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id.in_(session_ids),
                ChatMessage.role.notin_(_EXCLUDED_ROLES),
            )
            .order_by(ChatMessage.conversation_id.asc(), ChatMessage.created_at.asc())
        )
        transcript_rows = (await db.execute(transcript_stmt)).all()

    transcript_map: dict[str, list[tuple[str, str]]] = {session_id: [] for session_id in session_ids}
    for row in transcript_rows:
        conversation_id, role, content, _created_at = _unpack_transcript_row(row)
        text = (content or "").strip()
        if not text:
            continue
        transcript_map.setdefault(conversation_id, []).append((role or "unknown", text))

    for item in grouped.values():
        matched_messages = item.pop("_matched_messages")
        transcript_messages = transcript_map.get(item["session_id"]) or matched_messages
        transcript_lines = _transcript_lines_from_messages(transcript_messages)
        context_source_lines = transcript_lines or _transcript_lines_from_messages(matched_messages)
        item["context_snippets"] = _extract_context_snippets_from_lines(
            context_source_lines,
            query,
            snippet_limit=snippet_limit,
        )
        _annotate_recall_hit(
            item,
            query=query,
            transcript_lines=context_source_lines,
            headline=item["headline"],
        )

    hits = list(grouped.values())
    return hits if limit is None else hits[:limit]


async def search_session_history(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int | None = None,
    snippet_limit: int = 3,
    tenant_id: uuid.UUID | None = None,
) -> list[dict]:
    """Search past sessions, preferring the canonical T0 session ledger."""
    needle = (query or "").strip()
    if not needle:
        return []

    ledger_hits = _search_t0_session_ledger(
        agent_id,
        needle,
        limit=limit,
        snippet_limit=snippet_limit,
    )
    if ledger_hits:
        return await _summarize_recall_hits(needle, ledger_hits, tenant_id, agent_id)

    legacy_log_hits = _search_t0_chat_logs(
        agent_id,
        needle,
        limit=limit,
        snippet_limit=snippet_limit,
    )
    if legacy_log_hits:
        return await _summarize_recall_hits(needle, legacy_log_hits, tenant_id, agent_id)

    db_hits = await _search_session_history_db(
        agent_id,
        needle,
        limit=limit,
        snippet_limit=snippet_limit,
        tenant_id=tenant_id,
    )
    return await _summarize_recall_hits(needle, db_hits, tenant_id, agent_id)
