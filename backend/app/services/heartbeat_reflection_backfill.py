"""Backfill non-noop heartbeat reflections into the learning lanes.

This service is intentionally candidate-first. It does not import lineage
summaries and it does not write T2/T3/soul directly; apply mode replays
heartbeat assistant reflections through the same Learning Brain / Extractor
hook used by live heartbeats.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, cast, select

from app.database import tenant_scoped_session
from app.services.heartbeat import _route_heartbeat_reflection_learning, _should_route_heartbeat_reflection


async def _load_recent_heartbeat_records(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    days: int,
) -> list[dict[str, Any]]:
    """Best-effort DB loader for recent heartbeat assistant messages."""

    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    since = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(ChatMessage, ChatSession)
            .join(ChatSession, cast(ChatSession.id, String) == ChatMessage.conversation_id)
            .where(ChatMessage.agent_id == agent_id)
            .where(ChatMessage.role == "assistant")
            .where(ChatSession.source_channel == "heartbeat")
            .where(ChatMessage.created_at >= since)
            .order_by(ChatMessage.created_at.asc())
        )
        rows = result.all()

    records: list[dict[str, Any]] = []
    for message, session in rows:
        content = str(getattr(message, "content", "") or "")
        outcome_type, score = _parse_backfill_outcome(content)
        records.append(
            {
                "session_id": str(getattr(session, "id", "") or getattr(message, "conversation_id", "")),
                "assistant_message_id": str(getattr(message, "id", "")),
                "runtime_task_id": None,
                "content": content,
                "outcome_type": outcome_type,
                "outcome_lane": "idle" if outcome_type == "noop" else "agent_action",
                "score": score,
                "runtime_messages": [{"role": "assistant", "content": content}],
            }
        )
    return records


def _parse_backfill_outcome(content: str) -> tuple[str, int | None]:
    from app.services.heartbeat import _parse_heartbeat_outcome

    return _parse_heartbeat_outcome(content)


async def run_heartbeat_reflection_backfill(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    agent_name: str,
    records: list[dict[str, Any]] | None = None,
    days: int = 7,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Dry-run or apply heartbeat reflection replay.

    `dry_run=False` requires `confirm=True`. Records can be injected by tests or
    admin scripts; when omitted, recent heartbeat ChatMessages are loaded from DB.
    """

    if not dry_run and not confirm:
        raise ValueError("heartbeat reflection backfill apply requires confirm=True")

    loaded_records = records
    if loaded_records is None:
        loaded_records = await _load_recent_heartbeat_records(agent_id=agent_id, tenant_id=tenant_id, days=days)

    result = {
        "method": "llm_primary_hook",
        "dry_run": dry_run,
        "scanned": len(loaded_records),
        "would_process": 0,
        "processed": 0,
        "skipped_low_signal": 0,
        "errors": [],
    }

    for record in loaded_records:
        content = str(record.get("content") or "")
        outcome_type = str(record.get("outcome_type") or "noop")
        should_route, _reason = _should_route_heartbeat_reflection(outcome_type, content)
        if not should_route:
            result["skipped_low_signal"] += 1
            continue

        result["would_process"] += 1
        if dry_run:
            continue

        try:
            routed = await _route_heartbeat_reflection_learning(
                agent_id=agent_id,
                tenant_id=tenant_id,
                agent_name=agent_name,
                session_id=str(record.get("session_id") or "unknown-heartbeat-session"),
                runtime_task_id=record.get("runtime_task_id"),
                assistant_message_id=record.get("assistant_message_id"),
                runtime_messages=record.get("runtime_messages") or [{"role": "assistant", "content": content}],
                reply=content,
                outcome_type=outcome_type,
                outcome_lane=str(record.get("outcome_lane") or "unknown"),
                score=record.get("score"),
            )
            if routed.get("status") in {"emitted", "scheduled"}:
                result["processed"] += 1
            else:
                result["skipped_low_signal"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                {
                    "session_id": str(record.get("session_id") or ""),
                    "assistant_message_id": str(record.get("assistant_message_id") or ""),
                    "error": str(exc),
                }
            )

    return result
