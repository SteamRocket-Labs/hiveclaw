"""Backfill legacy chat_messages into chat_transcript_events and T0 ledger.

Default mode is dry-run. Use --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.services.chat_transcript import CHAT_MESSAGE_ROLES
from app.services.t0_logger import backfill_recent_chat_logs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, type=uuid.UUID)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    parser.add_argument("--recent-days", type=int, default=3650)
    parser.add_argument("--limit-sessions", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    return parser


async def _dry_run(
    *, agent_id: uuid.UUID, tenant_id: uuid.UUID | None, recent_days: int, limit_sessions: int
) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    async with tenant_scoped_session(tenant_id) as db:
        sessions = (
            (
                await db.execute(
                    select(ChatSession)
                    .where(ChatSession.agent_id == agent_id, ChatSession.created_at >= cutoff)
                    .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
                    .limit(limit_sessions)
                )
            )
            .scalars()
            .all()
        )
        rows: list[dict[str, Any]] = []
        total_messages = 0
        missing_transcript_sessions = 0
        for session in sessions:
            message_count = int(
                (
                    await db.execute(
                        select(func.count(ChatMessage.id)).where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(session.id),
                            ChatMessage.role.in_(tuple(sorted(CHAT_MESSAGE_ROLES))),
                        )
                    )
                ).scalar()
                or 0
            )
            transcript_count = int(
                (
                    await db.execute(
                        select(func.count(ChatTranscriptEvent.id)).where(
                            ChatTranscriptEvent.agent_id == agent_id,
                            ChatTranscriptEvent.session_id == session.id,
                        )
                    )
                ).scalar()
                or 0
            )
            total_messages += message_count
            if message_count and transcript_count == 0:
                missing_transcript_sessions += 1
            rows.append(
                {
                    "session_id": str(session.id),
                    "message_count": message_count,
                    "transcript_event_count": transcript_count,
                    "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
                }
            )
    return {
        "mode": "dry_run",
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "sessions_scanned": len(rows),
        "message_count": total_messages,
        "sessions_missing_transcript": missing_transcript_sessions,
        "sessions": rows[:50],
    }


async def _main() -> None:
    args = _parser().parse_args()
    if not args.apply:
        report = await _dry_run(
            agent_id=args.agent_id,
            tenant_id=args.tenant_id,
            recent_days=args.recent_days,
            limit_sessions=args.limit_sessions,
        )
    else:
        report = await backfill_recent_chat_logs(
            args.agent_id,
            recent_days=args.recent_days,
            limit_sessions=args.limit_sessions,
            tenant_id=args.tenant_id,
        )
        report = {
            "mode": "apply",
            "agent_id": str(args.agent_id),
            "tenant_id": str(args.tenant_id) if args.tenant_id else None,
            **report,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
