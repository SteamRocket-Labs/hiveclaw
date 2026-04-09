"""T0 Raw Behavior Logger — writes per-behavior MD files to logs/ directory.

T0 is the bottom layer of the 4-layer MD pyramid (T0→T2→T3→soul).
Each behavior (chat, trigger, delegation, heartbeat, dream) produces
a timestamped MD file with YAML frontmatter + body.

Files are organized by date: logs/YYYY-MM-DD/{type}-{HHmm}-{short_id}.md
Retention: 30 days (cleanup_old_logs removes older date directories).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

logger = logging.getLogger(__name__)


def _agent_logs_dir(agent_id: uuid.UUID) -> Path:
    """Return the logs/ directory for an agent."""
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "logs"


def _generate_filename(behavior_type: str, short_id: str = "") -> str:
    """Generate T0 filename: {type}-{HHmm}-{short_id}.md"""
    now = datetime.now(timezone.utc)
    hhmm = now.strftime("%H%M")
    if not short_id:
        short_id = uuid.uuid4().hex[:4]
    return f"{behavior_type}-{hhmm}-{short_id}.md"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _date_dir(agent_id: uuid.UUID, when: datetime | None = None) -> Path:
    """Return a date directory under logs/, creating it if needed."""
    target = when.astimezone(timezone.utc) if when else datetime.now(timezone.utc)
    date_label = target.strftime("%Y-%m-%d")
    d = _agent_logs_dir(agent_id) / date_label
    d.mkdir(parents=True, exist_ok=True)
    return d


def _yaml_frontmatter(fields: dict[str, Any]) -> str:
    """Format a dict as YAML frontmatter block."""
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(i) for i in v)}]")
        elif isinstance(v, datetime):
            lines.append(f"{k}: {v.isoformat()}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# ── Format functions for 5 behavior types ──


def _format_chat_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a chat session as T0 MD."""
    started_at = _coerce_datetime(metadata.get("started_at")) or datetime.now(timezone.utc)
    source = metadata.get("source", "web")
    session_id = metadata.get("session_id", "unknown")
    user_name = metadata.get("user_name", "User")

    # Collect tool names from assistant messages
    tools_used: list[str] = []
    turn_count = 0
    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            turn_count += 1
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "") if isinstance(fn, dict) else ""
                if name and name not in tools_used:
                    tools_used.append(name)

    front = _yaml_frontmatter({
        "type": "chat",
        "session_id": session_id,
        "source": source,
        "user": user_name,
        "started": started_at.isoformat(),
        "turns": turn_count,
        "tools": tools_used or [],
    })

    # Build turn-by-turn body
    body_parts: list[str] = []
    turn_num = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-part content — extract text parts
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content:
            continue

        if role == "user":
            turn_num += 1
            body_parts.append(f"\n## Turn {turn_num}\n**User**: {_truncate(content, 2000)}")
        elif role == "assistant":
            body_parts.append(f"**Agent**: {_truncate(content, 2000)}")
            # Append tool calls if present
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                tool_lines = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?") if isinstance(fn, dict) else "?"
                    args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                    tool_lines.append(f"- `{name}({_truncate(str(args), 200)})`")
                body_parts.append("**Tools**:\n" + "\n".join(tool_lines))

    return front + "\n" + "\n".join(body_parts) + "\n"


def _format_trigger_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a trigger execution as T0 MD."""
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter({
        "type": "trigger",
        "trigger_name": metadata.get("trigger_name", "unknown"),
        "trigger_type": metadata.get("trigger_type", "unknown"),
        "executed": now.isoformat(),
        "status": metadata.get("status", "unknown"),
        "duration_ms": metadata.get("duration_ms", 0),
    })

    instruction = metadata.get("instruction", "")
    result = metadata.get("result", "")
    execution = _messages_to_execution(messages)

    body = f"""
## Instruction
{_truncate(instruction, 2000)}

## Execution
{execution}

## Result
{_truncate(result, 2000)}
"""
    return front + body


def _format_delegation_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a delegation execution as T0 MD."""
    now = _coerce_datetime(metadata.get("delegated_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter({
        "type": "delegation",
        "from": metadata.get("from_agent", "unknown"),
        "to": metadata.get("to_agent", "unknown"),
        "task": metadata.get("task", ""),
        "delegated": now.isoformat(),
        "status": metadata.get("status", "unknown"),
    })

    task_text = metadata.get("task", "")
    result = metadata.get("result", "")
    execution = _messages_to_execution(messages)

    body = f"""
## Task
{_truncate(task_text, 2000)}

## Execution
{execution}

## Result
{_truncate(result, 2000)}
"""
    return front + body


def _format_heartbeat_log(_messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a heartbeat tick as T0 MD."""
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter({
        "type": "heartbeat",
        "tick": metadata.get("tick", 0),
        "session_started": metadata.get("session_started", now.isoformat()),
        "executed": now.isoformat(),
        "new_t2": metadata.get("new_t2", 0),
        "distilled": metadata.get("distilled", 0),
        "score": metadata.get("score", 0),
    })

    new_t2_entries = metadata.get("new_t2_entries", [])
    distillation = metadata.get("distillation", [])
    action = metadata.get("action", "none")

    t2_section = "\n".join(f"- {e}" for e in new_t2_entries) if new_t2_entries else "(none)"
    distill_section = "\n".join(f"- {d}" for d in distillation) if distillation else "(none)"

    body = f"""
## New T2 Entries
{t2_section}

## Distillation
{distill_section}

## Action
{_truncate(str(action), 2000)}
"""
    return front + body


def _format_dream_log(_messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a dream execution as T0 MD."""
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter({
        "type": "dream",
        "executed": now.isoformat(),
        "t3_processed": metadata.get("t3_processed", 0),
        "deduped": metadata.get("deduped", 0),
        "promoted_to_soul": metadata.get("promoted_to_soul", 0),
    })

    dedup = metadata.get("dedup_summary", "")
    promotions = metadata.get("soul_promotions", [])
    cleanup = metadata.get("cleanup_summary", "")

    promo_section = "\n".join(f"- {p}" for p in promotions) if promotions else "(none)"

    body = f"""
## Dedup
{_truncate(dedup, 2000) if dedup else '(none)'}

## Soul Promotion
{promo_section}

## Cleanup
{_truncate(cleanup, 2000) if cleanup else '(none)'}
"""
    return front + body


# ── Helpers ──


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _messages_to_execution(messages: list[dict]) -> str:
    """Convert messages list to a compact execution summary."""
    if not messages:
        return "(no messages)"
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content:
            continue
        parts.append(f"**{role}**: {_truncate(content, 500)}")
    return "\n\n".join(parts) if parts else "(no content)"


# ── Public API ──

_FORMATTERS: dict[str, Any] = {
    "chat": _format_chat_log,
    "trigger": _format_trigger_log,
    "delegation": _format_delegation_log,
    "heartbeat": _format_heartbeat_log,
    "dream": _format_dream_log,
}


def write_t0_log(
    agent_id: uuid.UUID,
    *,
    behavior_type: str,
    messages: list[dict] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    """Write a T0 raw log file. Returns the file path, or None on failure.

    This is a synchronous, fire-and-forget operation.
    Zero LLM dependency — pure pattern-based formatting.
    """
    formatter = _FORMATTERS.get(behavior_type)
    if not formatter:
        logger.warning("[T0] Unknown behavior type: %s", behavior_type)
        return None

    messages = messages or []
    metadata = metadata or {}

    try:
        content = formatter(messages, metadata)
        short_id = str(metadata.get("session_id", uuid.uuid4().hex))[:4]
        filename = _generate_filename(behavior_type, short_id)
        occurred_at = _coerce_datetime(metadata.get("occurred_at") or metadata.get("started_at"))
        filepath = _date_dir(agent_id, occurred_at) / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info("[T0] Wrote %s for agent %s (%d bytes)", filepath.name, agent_id, len(content))
        return filepath
    except Exception as exc:
        logger.error("[T0] Failed to write %s log for agent %s: %s", behavior_type, agent_id, exc)
        return None


def cleanup_old_logs(agent_id: uuid.UUID, retention_days: int = 30) -> int:
    """Remove T0 date directories older than retention_days. Returns count removed."""
    logs_dir = _agent_logs_dir(agent_id)
    if not logs_dir.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0

    for entry in logs_dir.iterdir():
        if not entry.is_dir():
            continue
        # Directory name should be YYYY-MM-DD
        if entry.name < cutoff_str:
            try:
                shutil.rmtree(entry)
                removed += 1
                logger.info("[T0] Cleaned up old log directory: %s", entry.name)
            except Exception as exc:
                logger.warning("[T0] Failed to remove %s: %s", entry.name, exc)

    if removed:
        logger.info("[T0] Cleaned %d old log directories for agent %s", removed, agent_id)
    return removed


def audit_t0_logs(agent_id: uuid.UUID, recent_days: int = 30) -> dict[str, Any]:
    """Inspect T0 log health for an agent."""
    logs_dir = _agent_logs_dir(agent_id)
    if not logs_dir.exists():
        return {
            "logs_dir_exists": False,
            "date_dirs": 0,
            "empty_date_dirs": 0,
            "total_files": 0,
            "recent_files": 0,
            "healthy": False,
        }

    cutoff_label = (datetime.now(timezone.utc) - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    date_dirs = 0
    empty_date_dirs = 0
    total_files = 0
    recent_files = 0

    for day_dir in logs_dir.iterdir():
        if not day_dir.is_dir():
            continue
        date_dirs += 1
        files = [path for path in day_dir.iterdir() if path.is_file()]
        if not files:
            empty_date_dirs += 1
            continue
        total_files += len(files)
        if day_dir.name >= cutoff_label:
            recent_files += len(files)

    return {
        "logs_dir_exists": True,
        "date_dirs": date_dirs,
        "empty_date_dirs": empty_date_dirs,
        "total_files": total_files,
        "recent_files": recent_files,
        "healthy": total_files > 0,
    }


def _extract_existing_session_ids(files: Iterable[Path]) -> set[str]:
    session_ids: set[str] = set()
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("session_id:"):
                    session_ids.add(line.split(":", 1)[1].strip())
                    break
        except Exception as exc:
            logger.debug("[T0] Failed to inspect %s: %s", path, exc)
    return session_ids


async def backfill_recent_chat_logs(agent_id: uuid.UUID, recent_days: int = 30, limit_sessions: int = 20) -> dict[str, int]:
    """Backfill recent chat sessions into T0 logs when raw files are missing."""
    logs_dir = _agent_logs_dir(agent_id)
    existing_session_ids = _extract_existing_session_ids(logs_dir.rglob("chat-*.md")) if logs_dir.exists() else set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    session_stmt = (
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.created_at >= cutoff,
        )
        .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
        .limit(limit_sessions)
    )

    try:
        async with async_session() as db:
            sessions = (await db.execute(session_stmt)).scalars().all()
            written = 0
            skipped_existing = 0
            skipped_empty = 0

            for session in sessions:
                session_key = str(session.id)
                if session_key in existing_session_ids:
                    skipped_existing += 1
                    continue

                message_stmt = (
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == session_key,
                        ChatMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(ChatMessage.created_at.asc())
                )
                messages = (await db.execute(message_stmt)).scalars().all()
                rendered_messages = [
                    {
                        "role": message.role,
                        "content": message.content,
                    }
                    for message in messages
                    if (message.content or "").strip()
                ]
                if not rendered_messages:
                    skipped_empty += 1
                    continue

                path = write_t0_log(
                    agent_id,
                    behavior_type="chat",
                    messages=rendered_messages,
                    metadata={
                        "session_id": session_key,
                        "source": session.source_channel,
                        "started_at": session.created_at,
                        "occurred_at": session.created_at,
                    },
                )
                if path is not None:
                    written += 1
                    existing_session_ids.add(session_key)
    except Exception as exc:
        logger.debug("[T0] Backfill skipped for %s: %s", agent_id, exc)
        return {
            "sessions_scanned": 0,
            "written": 0,
            "skipped_existing": 0,
            "skipped_empty": 0,
        }

    return {
        "sessions_scanned": len(sessions),
        "written": written,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
    }
