"""Audited repair of answers overwritten by the retired tool-evidence verifier.

The repair never guesses semantic content. It matches only the verifier's exact
fixed signature and restores a message only when durable stream chunks from the
same run and final generation window reconstruct the original model output.
The original transcript/T0 evidence remains immutable; apply mode appends a
``response_repair`` event and updates only the product ``ChatMessage`` read model.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.invocation_span import InvocationSpan
from app.services.chat_transcript import append_session_event


REPAIR_VERSION = "false_tool_evidence_notice.v1"
logger = logging.getLogger(__name__)
_FALSE_NOTICE_PREFIX = "我不能确认刚才的工具状态：本轮没有实际工具调用记录，因此不能声称 "
_FALSE_NOTICE_PATTERN = re.compile(
    r"\A我不能确认刚才的工具状态：本轮没有实际工具调用记录，因此不能声称 "
    r"`[A-Za-z0-9_.:-]+`(?:, `[A-Za-z0-9_.:-]+`)* "
    r"已返回、失败或超时。请重试该操作，我会先调用对应工具并基于工具结果给出结论。\Z"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_false_tool_evidence_notice(content: str | None) -> bool:
    """Return true only for the exact retired verifier output grammar."""

    return isinstance(content, str) and _FALSE_NOTICE_PATTERN.fullmatch(content) is not None


def reconstruct_original_final(
    events: Iterable[Any],
    *,
    terminal_event: Any,
    generation_started_at: datetime | None,
) -> str | None:
    """Reconstruct delta chunks from one run's final generation window.

    A missing generation timestamp is deliberately unrecoverable: using older
    chunks could join a previous tool round or retry and invent an answer.
    """

    if generation_started_at is None:
        return None
    run_id = getattr(terminal_event, "run_id", None)
    terminal_sequence = getattr(terminal_event, "sequence", None)
    if run_id is None or terminal_sequence is None:
        return None

    chunks: list[tuple[int, str]] = []
    for event in events:
        if str(getattr(event, "event_type", "") or "") != "chunk":
            continue
        if str(getattr(event, "run_id", "") or "") != str(run_id):
            continue
        sequence = getattr(event, "sequence", None)
        created_at = getattr(event, "created_at", None)
        if not isinstance(sequence, int) or sequence >= int(terminal_sequence):
            continue
        if not isinstance(created_at, datetime) or created_at < generation_started_at:
            continue
        content = getattr(event, "content", None)
        if isinstance(content, str) and content:
            chunks.append((sequence, content))

    reconstructed = "".join(content for _, content in sorted(chunks, key=lambda item: item[0]))
    return reconstructed if reconstructed.strip() else None


async def _terminal_event_for_message(db: AsyncSession, message: ChatMessage) -> ChatTranscriptEvent | None:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.message_id == message.id,
            ChatTranscriptEvent.event_type == "assistant_message",
        )
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_generation_span(
    db: AsyncSession,
    terminal_event: ChatTranscriptEvent,
) -> InvocationSpan | None:
    if terminal_event.run_id is None:
        return None
    query = select(InvocationSpan).where(
        InvocationSpan.runtime_task_id == terminal_event.run_id,
        InvocationSpan.span_type == "generation",
    )
    if terminal_event.created_at is not None:
        query = query.where(InvocationSpan.started_at <= terminal_event.created_at)
    result = await db.execute(query.order_by(InvocationSpan.started_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def _stream_events_for_terminal(
    db: AsyncSession,
    terminal_event: ChatTranscriptEvent,
    generation_started_at: datetime,
) -> list[ChatTranscriptEvent]:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.session_id == terminal_event.session_id,
            ChatTranscriptEvent.run_id == terminal_event.run_id,
            ChatTranscriptEvent.event_type == "chunk",
            ChatTranscriptEvent.sequence < terminal_event.sequence,
            ChatTranscriptEvent.created_at >= generation_started_at,
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    return list(result.scalars().all())


async def _already_repaired(db: AsyncSession, message_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(ChatTranscriptEvent.id)
        .where(
            ChatTranscriptEvent.message_id == message_id,
            ChatTranscriptEvent.event_type == "response_repair",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def repair_false_tool_evidence_notices(
    db: AsyncSession,
    *,
    apply: bool = False,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    recent_days: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Classify or repair exact verifier notices without exposing message text."""

    query = select(ChatMessage).where(
        ChatMessage.role == "assistant",
        ChatMessage.content.like(f"{_FALSE_NOTICE_PREFIX}%"),
    )
    if tenant_id is not None:
        query = query.where(ChatMessage.tenant_id == tenant_id)
    if agent_id is not None:
        query = query.where(ChatMessage.agent_id == agent_id)
    if recent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, recent_days))
        query = query.where(ChatMessage.created_at >= cutoff)
    query = query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    if limit is not None:
        query = query.limit(max(0, limit))

    messages = list((await db.execute(query)).scalars().all())
    matches = [message for message in messages if is_false_tool_evidence_notice(message.content)]
    tenant_counts: Counter[str] = Counter()
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "repair_version": REPAIR_VERSION,
        "messages_scanned": len(messages),
        "matched": len(matches),
        "recoverable": 0,
        "unrecoverable_original_output": 0,
        "already_repaired": 0,
        "repaired": 0,
        "failed": 0,
    }

    for message in matches:
        tenant_counts[str(message.tenant_id)] += 1
        if await _already_repaired(db, message.id):
            report["already_repaired"] += 1
            continue
        terminal_event = await _terminal_event_for_message(db, message)
        if terminal_event is None:
            report["unrecoverable_original_output"] += 1
            continue
        generation_span = await _latest_generation_span(db, terminal_event)
        if generation_span is None:
            report["unrecoverable_original_output"] += 1
            continue
        chunks = await _stream_events_for_terminal(db, terminal_event, generation_span.started_at)
        reconstructed = reconstruct_original_final(
            chunks,
            terminal_event=terminal_event,
            generation_started_at=generation_span.started_at,
        )
        if reconstructed is None or is_false_tool_evidence_notice(reconstructed):
            report["unrecoverable_original_output"] += 1
            continue

        report["recoverable"] += 1
        if not apply:
            continue
        try:
            original_notice = message.content
            original_hash = _sha256(original_notice)
            repaired_hash = _sha256(reconstructed)
            idempotency_key = f"{REPAIR_VERSION}:{message.id}"
            message.content = reconstructed
            await append_session_event(
                db=db,
                agent_id=message.agent_id,
                tenant_id=message.tenant_id,
                session_id=terminal_event.session_id,
                run_id=terminal_event.run_id,
                actor_type="system",
                event_type="response_repair",
                role="assistant",
                t0_role="assistant",
                user_id=message.user_id,
                external_principal_id=message.external_principal_id,
                participant_id=message.participant_id,
                message_id=message.id,
                parent_event_id=terminal_event.id,
                content=reconstructed,
                materialize_chat_message=False,
                source="repair_false_tool_evidence_notices",
                metadata={
                    "source": "repair_false_tool_evidence_notices",
                    "repair_version": REPAIR_VERSION,
                    "idempotency_key": idempotency_key,
                    "original_message_id": str(message.id),
                    "original_transcript_event_id": str(terminal_event.id),
                    "generation_span_id": str(generation_span.id),
                    "original_notice_sha256": original_hash,
                    "repaired_content_sha256": repaired_hash,
                    "evidence_refs": [
                        f"chat_transcript_event:{terminal_event.id}",
                        f"invocation_span:{generation_span.id}",
                        *[f"chat_transcript_event:{event.id}" for event in chunks],
                    ],
                    "supersedes_message_id": str(message.id),
                },
            )
            await db.commit()
            report["repaired"] += 1
        except Exception:
            logger.exception("Failed to repair false tool-evidence notice message_id=%s", message.id)
            await db.rollback()
            report["failed"] += 1

    report["matches_by_tenant"] = dict(sorted(tenant_counts.items()))
    return report
