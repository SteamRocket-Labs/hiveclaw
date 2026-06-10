"""Cross-session recall over T0 chat logs with DB fallback."""

from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import String as SaString, cast as sa_cast, or_, select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

_FALLBACK_HEADLINE = "回顾到与查询相关的历史会话"
_EXCLUDED_CHANNELS = {"agent", "heartbeat", "trigger", "task", "dream"}
_EXCLUDED_ROLES = {"system", "tool_call"}
_QUERY_SPLIT_RE = re.compile(r"\s+")
_ARTIFACT_PATH_RE = re.compile(r"\b[\w./-]+\.[A-Za-z0-9]{1,8}\b")


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


def _build_summary(headline: str, snippets: list[str], *, max_chars: int = 220) -> str:
    parts: list[str] = []
    if headline and headline != _FALLBACK_HEADLINE:
        parts.append(headline)
    for snippet in snippets:
        cleaned = _clean_line(snippet)
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    summary = "；".join(parts).strip("； ")
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary or _FALLBACK_HEADLINE


def _extract_artifact_paths(lines: list[str]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for match in _ARTIFACT_PATH_RE.findall(line):
            if "/" not in match:
                continue
            if match in seen:
                continue
            seen.add(match)
            paths.append(match)
    return paths


def _select_evidence_lines(
    transcript_lines: list[str],
    query: str,
    *,
    max_lines: int = 2,
) -> list[str]:
    if not transcript_lines:
        return []

    window = _extract_transcript_window_from_lines(transcript_lines, query, context_radius=1, max_lines=max_lines * 2)
    candidate_lines = [line.strip() for line in window.splitlines() if line.strip()] or transcript_lines[:max_lines]
    evidence: list[str] = []

    assistant_lines = [line for line in candidate_lines if line.startswith("Assistant:")]
    user_lines = [line for line in candidate_lines if line.startswith("User:")]

    for line in assistant_lines + user_lines + candidate_lines:
        if line not in evidence:
            evidence.append(line)
        if len(evidence) >= max_lines:
            break

    if not any(line.startswith("Assistant:") for line in evidence):
        for line in transcript_lines:
            if line.startswith("Assistant:") and line not in evidence:
                evidence.append(line)
                if len(evidence) >= max_lines:
                    break

    return evidence[:max_lines]


def _strip_speaker(line: str) -> str:
    if ":" not in line:
        return line.strip()
    return line.split(":", 1)[1].strip()


def _build_focused_recap(
    *,
    headline: str,
    evidence_lines: list[str],
    fallback_summary: str,
) -> str:
    if not evidence_lines:
        return fallback_summary or headline or _FALLBACK_HEADLINE

    decision_line = next((line for line in evidence_lines if line.startswith("Assistant:")), evidence_lines[0])
    decision = _strip_speaker(decision_line)
    artifacts = _extract_artifact_paths(evidence_lines)

    recap_parts = [f"Decision: {decision}"]
    if artifacts:
        recap_parts.append(f"Artifacts: {', '.join(artifacts)}")

    supporting = [line for line in evidence_lines if line != decision_line]
    if supporting:
        recap_parts.append(f"Evidence: {' | '.join(supporting[:2])}")
    return " ".join(part.strip() for part in recap_parts if part.strip())


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
    summary = _build_summary(
        headline,
        (transcript_window.splitlines() if transcript_window else []) or context_snippets or snippets,
    )
    evidence_lines = _select_evidence_lines(transcript_lines, query)

    hit["headline"] = headline
    hit["transcript_window"] = transcript_window
    hit["summary"] = summary
    hit["evidence_lines"] = evidence_lines
    hit["focused_recap"] = _build_focused_recap(
        headline=headline,
        evidence_lines=evidence_lines,
        fallback_summary=summary,
    )
    return hit


async def _summarize_recall_hits(
    query: str,
    hits: list[dict],
    tenant_id: uuid.UUID | None,
) -> list[dict]:
    """Optionally enrich recall hits with a focused summary model.

    This keeps the retrieval source md-first / transcript-first while using a
    cheap summarizer to compress the recalled evidence into a cleaner recap.
    Falls back silently to the heuristic summary when no tenant summary model is
    configured or any LLM call fails.
    """
    if not hits or tenant_id is None:
        return hits

    try:
        from app.services.llm_client import LLMMessage, create_llm_client_from_config
        from app.services.memory_service import _get_summary_model_config

        model_config = await _get_summary_model_config(tenant_id)
        if not model_config:
            return hits

        client = create_llm_client_from_config(model_config)
        try:
            for hit in hits:
                evidence = hit.get("context_snippets") or hit.get("snippets") or []
                transcript_window = (hit.get("transcript_window") or "").strip()
                evidence_lines = hit.get("evidence_lines") or []
                evidence_block = "\n".join(f"- {snippet}" for snippet in evidence if snippet)
                if transcript_window:
                    evidence_block = f"{evidence_block}\nTranscript window:\n{transcript_window}".strip()
                if evidence_lines:
                    evidence_block = (
                        f"{evidence_block}\nKey evidence:\n" + "\n".join(f"- {line}" for line in evidence_lines if line)
                    ).strip()
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
                    "- Use ONLY the provided evidence block + headline + heuristic recap.\n"
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
                    f"Heuristic recap: {hit.get('focused_recap', '')}\n"
                    f"Evidence:\n{evidence_block}\n"
                    "</input>\n\n"
                    "<output_contract>\n"
                    "Respond with ONLY the 1-2 sentence summary. No prefix, no quotes,\n"
                    "no markdown formatting, no meta-commentary about the query.\n"
                    "</output_contract>"
                )
                response = await client.stream(
                    messages=[LLMMessage(role="user", content=prompt)],
                    max_tokens=180,
                    temperature=0.1,
                )
                summary = (response.content or "").strip()
                if summary:
                    hit["summary"] = summary
                    hit["focused_recap"] = summary
        finally:
            await client.close()
    except Exception:
        return hits

    return hits


def _infer_headline(body: str, query: str, *, fallback: str) -> str:
    snippets = _extract_snippets(body, query, snippet_limit=1)
    if snippets:
        return snippets[0][:120]
    for line in _body_lines(body):
        if line:
            return line[:120]
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


def _search_t0_chat_logs(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int,
    snippet_limit: int,
) -> list[dict]:
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
    return hits[:limit]


async def _search_session_history_db(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int,
    snippet_limit: int,
    tenant_id: uuid.UUID | None = None,
) -> list[dict]:
    needle = (query or "").strip()
    if not needle:
        return []

    fetch_limit = max(limit * max(snippet_limit, 1) * 4, 20)
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
            ChatSession.source_channel.notin_(_EXCLUDED_CHANNELS),
            or_(
                ChatMessage.content.ilike(pattern),
                ChatSession.summary.ilike(pattern),
            ),
        )
        .order_by(ChatSession.last_message_at.desc(), ChatMessage.created_at.asc())
        .limit(fetch_limit)
    )

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

            if len(grouped) >= limit and all(len(item["snippets"]) >= snippet_limit for item in grouped.values()):
                break

        session_ids = list(grouped.keys())[:limit]
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

    return list(grouped.values())[:limit]


async def search_session_history(
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int = 3,
    snippet_limit: int = 3,
    tenant_id: uuid.UUID | None = None,
) -> list[dict]:
    """Search past sessions, preferring canonical T0 chat md logs."""
    needle = (query or "").strip()
    if not needle:
        return []

    log_hits = _search_t0_chat_logs(
        agent_id,
        needle,
        limit=limit,
        snippet_limit=snippet_limit,
    )
    if log_hits:
        return await _summarize_recall_hits(needle, log_hits, tenant_id)

    db_hits = await _search_session_history_db(
        agent_id,
        needle,
        limit=limit,
        snippet_limit=snippet_limit,
        tenant_id=tenant_id,
    )
    return await _summarize_recall_hits(needle, db_hits, tenant_id)
