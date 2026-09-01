"""Session command execution for CC/Codex parity.

The command layer is a control surface over existing transcript truth. It never
rewrites prior transcript events. ``branch`` creates a new ChatSession index;
``rewind`` updates the active projection on the current session.
"""

from __future__ import annotations

import asyncio
import uuid
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.memory.t0.ledger import T0SessionEvent, replay_t0_session_events_tail
from app.runtime.hooks import HookEvent, emit_hook
from app.services.chat_transcript import append_session_event, read_transcript_revision
from app.services.conversation_branch_service import create_conversation_branch
from app.services.session_user_checkpoint import (
    event_item_kind,
    event_lifecycle,
    event_role as shared_event_role,
    is_assistant_final_message,
    is_human_input_checkpoint,
    is_human_input_row,
    user_checkpoint_content,
    user_checkpoint_events,
)
from app.services.memory_service import _generate_session_summary, _wrap_compressed_summary
from app.services.session_workspace_snapshot import (
    finalize_workspace_restore,
    restore_session_workspace_snapshot,
)
from app.services.session_live_input import submit_live_cancel_input, submit_live_human_input
from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES, get_active_web_chat_run

SESSION_COMMAND_NAMES = frozenset(
    {
        "resume",
        "checkpoints",
        "rewind",
        "rollback",
        "branch",
        "btw",
        "interrupt",
        "turn_steer",
        "steer",
        "rename",
        "tag",
        "export",
        "copy",
        "clear",
        "compact",
    }
)

_REPLAYABLE_TURN_EVENT_TYPES = {"user_message", "assistant_message", "tool_call", "tool_result", "assistant_delta"}
_INTERRUPTED_TAIL_EVENT_TYPES = {"user_message", "tool_call", "tool_result", "assistant_delta", "run_started"}
_FENCED_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


async def _load_session(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session_id: uuid.UUID | str | None,
    access_level: str,
) -> ChatSession:
    if session_id is None:
        raise HTTPException(status_code=400, detail="session_id is required for this command")
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.agent_id == agent.id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(session.user_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized to use this session")
    return session


def _event_metadata(event: ChatTranscriptEvent | T0SessionEvent) -> dict[str, Any]:
    metadata = getattr(event, "metadata_json", None)
    if isinstance(metadata, dict):
        return metadata
    metadata = getattr(event, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _event_anchor_id(event: ChatTranscriptEvent | T0SessionEvent) -> str:
    metadata = _event_metadata(event)
    transcript_event_id = str(metadata.get("transcript_event_id") or "").strip()
    if transcript_event_id:
        return transcript_event_id
    raw_id = getattr(event, "id", None)
    if raw_id is not None:
        return str(raw_id)
    return str(getattr(event, "event_id", "") or "")


def _event_ledger_id(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    raw_id = getattr(event, "event_id", None)
    return str(raw_id) if raw_id else None


def _branch_anchor_event_id(event: ChatTranscriptEvent | T0SessionEvent) -> uuid.UUID:
    raw = _event_anchor_id(event)
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="This checkpoint has no transcript_event_id read-model anchor for branch projection.",
        ) from exc


def _parse_uuid_argument(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a UUID") from exc


def _created_at_value(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    created_at = getattr(event, "created_at", None)
    if hasattr(created_at, "isoformat"):
        return created_at.isoformat()
    return str(created_at) if created_at else None


def _event_payload(event: ChatTranscriptEvent | T0SessionEvent) -> dict[str, Any]:
    return {
        "id": _event_anchor_id(event),
        "ledger_event_id": _event_ledger_id(event),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "actor_type": getattr(event, "actor_type", None) or _event_metadata(event).get("actor_type"),
        "role": shared_event_role(event) or getattr(event, "role", None),
        "content": user_checkpoint_content(event) if is_human_input_row(event) else (event.content or ""),
        "metadata": _event_metadata(event),
        "created_at": _created_at_value(event),
        "truth_path": str(getattr(event, "truth_path", None) or "") or None,
        "projection_path": str(getattr(event, "path", None) or "") or None,
        "event_hash": getattr(event, "event_hash", None),
    }


def _session_payload(session: ChatSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title,
        "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
        "root_session_id": str(session.root_session_id) if session.root_session_id else None,
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
    }


def _control_event_payload(
    event_type: str, *, event: Any | None = None, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    event_id = getattr(event, "event_id", None) or getattr(event, "id", None)
    return {
        "event_type": event_type,
        "event_id": str(event_id) if event_id else None,
        "sequence": getattr(event, "sequence", None),
        "metadata": metadata or {},
    }


def _typed_result(
    *,
    command: str,
    action: str,
    session_id: uuid.UUID | str,
    ui_action: dict[str, Any],
    ok: bool = True,
    control_event: dict[str, Any] | None = None,
    debug_payload: dict[str, Any] | None = None,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "command": command,
        "action": action,
        "session_id": str(session_id),
        "ui_action": ui_action,
        "control_event": control_event,
        "debug_payload": debug_payload or {},
        **payload,
    }


async def _append_control_event(
    *,
    db: AsyncSession,
    agent: Agent,
    session: ChatSession,
    user: User,
    event_type: str,
    content: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    event = await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", getattr(session, "tenant_id", None)),
        session_id=session.id,
        actor_type="system",
        event_type=event_type,
        content=content,
        role="system",
        user_id=getattr(user, "id", None),
        root_session_id=session.root_session_id or session.id,
        parent_session_id=session.parent_session_id,
        metadata=metadata,
        source="command",
    )
    return _control_event_payload(event_type, event=event, metadata=metadata)


def _run_request_payload(run_request: Any | None) -> dict[str, Any] | None:
    if run_request is None:
        return None
    extra_metadata = dict(getattr(run_request, "extra_metadata", None) or {})
    return {
        "content": getattr(run_request, "content", ""),
        "display_content": getattr(run_request, "display_content", ""),
        "file_name": getattr(run_request, "file_name", ""),
        "append_user_message": bool(getattr(run_request, "append_user_message", True)),
        "attachments": getattr(run_request, "attachments", None) or [],
        "parts": getattr(run_request, "parts", None) or [],
        **extra_metadata,
    }


def _event_role(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    shared_role = shared_event_role(event)
    if shared_role:
        return shared_role
    event_type = getattr(event, "event_type", None)
    if event_type == "assistant_message":
        return "assistant"
    if event_type in {"tool_call", "tool_result"}:
        return "tool"
    return None


def _is_replayable_turn_event(event: ChatTranscriptEvent | T0SessionEvent) -> bool:
    event_type = getattr(event, "event_type", None)
    if event_type in _REPLAYABLE_TURN_EVENT_TYPES:
        if event_type == "assistant_message" and not (getattr(event, "content", None) or "").strip():
            return False
        return True
    # Session V2 parity, typed by item kind/lifecycle only: an authoritative
    # HumanInput checkpoint and a completed assistant final mirror the legacy
    # user_message/assistant_message tails; tool rounds and assistant deltas
    # mirror the legacy tool_call/tool_result/assistant_delta tails. A
    # zero-copy final carries its bytes in source blocks, so replayability
    # must not depend on inline content.
    if is_human_input_checkpoint(event):
        return bool(user_checkpoint_content(event).strip())
    if is_assistant_final_message(event):
        return True
    item_kind = event_item_kind(event)
    if item_kind in {"tool_call", "tool_result"}:
        return True
    return item_kind == "assistant_text" and event_lifecycle(event) == "delta"


def _is_interrupted_tail_event(event: ChatTranscriptEvent | T0SessionEvent) -> bool:
    if getattr(event, "event_type", None) in _INTERRUPTED_TAIL_EVENT_TYPES:
        return True
    if is_human_input_checkpoint(event):
        return True
    item_kind = event_item_kind(event)
    if item_kind in {"tool_call", "tool_result"}:
        return True
    return item_kind == "assistant_text" and event_lifecycle(event) == "delta"


def _last_replayable_turn_event(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> ChatTranscriptEvent | T0SessionEvent | None:
    for event in reversed(events):
        if _is_replayable_turn_event(event):
            return event
    return None


async def _latest_session_runtime_state(db: Any, *, agent: Agent, session: ChatSession) -> str | None:
    if not isinstance(db, AsyncSession):
        return None
    result = await db.execute(
        select(RuntimeTask.status)
        .where(
            RuntimeTask.tenant_id == session.tenant_id,
            RuntimeTask.parent_agent_id == agent.id,
            RuntimeTask.parent_session_id == str(session.id),
            RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES),
        )
        .order_by(RuntimeTask.created_at.desc(), RuntimeTask.id.desc())
        .limit(1)
    )
    status = result.scalar_one_or_none()
    return str(status).strip().lower() if status else None


def _user_checkpoint_events(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> list[ChatTranscriptEvent | T0SessionEvent]:
    return user_checkpoint_events(events)


def _checkpoint_payload(event: ChatTranscriptEvent | T0SessionEvent, *, turn_index: int) -> dict[str, Any]:
    payload = _event_payload(event)
    payload["checkpoint_event_id"] = _event_anchor_id(event)
    payload["turn_index"] = turn_index
    payload["checkpoint_type"] = "user_message"
    payload["role"] = "user"
    payload["content"] = user_checkpoint_content(event)
    return payload


def _checkpoint_payloads(events: list[ChatTranscriptEvent | T0SessionEvent]) -> list[dict[str, Any]]:
    return [
        _checkpoint_payload(event, turn_index=index) for index, event in enumerate(_user_checkpoint_events(events), 1)
    ]


def _workspace_restore_scope_after_checkpoint(
    events: list[ChatTranscriptEvent | T0SessionEvent],
    *,
    checkpoint: ChatTranscriptEvent | T0SessionEvent,
) -> tuple[
    list[str],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[str],
]:
    """Return latest session-owned file states after the selected checkpoint."""

    checkpoint_sequence = int(getattr(checkpoint, "sequence", 0) or 0)
    states: dict[str, dict[str, Any]] = {}
    lineage: dict[str, list[dict[str, Any]]] = {}
    unverifiable: set[str] = set()
    for event in events:
        if int(getattr(event, "sequence", 0) or 0) <= checkpoint_sequence:
            continue
        if getattr(event, "event_type", None) != "file_changes":
            continue
        metadata = _event_metadata(event)
        raw_states = metadata.get("file_change_states")
        event_states = raw_states if isinstance(raw_states, dict) else {}
        raw_lineage = metadata.get("file_change_lineage")
        event_lineage = (
            [dict(item) for item in raw_lineage if isinstance(item, dict)] if isinstance(raw_lineage, list) else []
        )
        paths = metadata.get("file_change_paths")
        for raw_path in paths if isinstance(paths, list) else []:
            path = str(raw_path or "").strip()
            if not path:
                continue
            state = event_states.get(path)
            state_is_verifiable = (
                isinstance(state, dict)
                and "exists" in state
                and (
                    not state.get("exists")
                    or (isinstance(state.get("sha256"), str) and len(str(state.get("sha256"))) == 64)
                )
            )
            if not state_is_verifiable:
                states.pop(path, None)
                unverifiable.add(path)
                continue
            states[path] = dict(state)
            path_lineage = [record for record in event_lineage if str(record.get("path") or "") == path]
            if not path_lineage:
                unverifiable.add(path)
                continue
            lineage.setdefault(path, []).extend(path_lineage)
        errors = metadata.get("file_change_state_errors")
        if isinstance(errors, dict):
            for raw_path in errors:
                path = str(raw_path or "").strip()
                if path:
                    states.pop(path, None)
                    unverifiable.add(path)
    paths = sorted(set(states) | unverifiable)
    return paths, states, lineage, sorted(unverifiable)


async def _rollback_deferred_workspace_restore(
    *,
    agent_id: uuid.UUID,
    workspace_restore_payload: dict[str, Any] | None,
) -> None:
    if not isinstance(workspace_restore_payload, dict):
        return
    transaction_id = str(workspace_restore_payload.get("transaction_id") or "")
    if not transaction_id or not workspace_restore_payload.get("requires_finalize"):
        return
    await asyncio.to_thread(
        finalize_workspace_restore,
        agent_id=agent_id,
        transaction_id=transaction_id,
        commit=False,
    )
    workspace_restore_payload["requires_finalize"] = False


def _has_explicit_rewind_target(arguments: dict[str, Any]) -> bool:
    return any(arguments.get(key) for key in ("checkpoint_event_id", "anchor_event_id", "event_id", "num_turns"))


def _session_event_revision(events: list[ChatTranscriptEvent | T0SessionEvent]) -> int:
    revisions: list[int] = []
    for event in events:
        try:
            revisions.append(int(getattr(event, "sequence", 0) or 0))
        except (TypeError, ValueError):
            continue
    return max(revisions, default=0)


def _expected_rewind_revision(arguments: dict[str, Any]) -> int | None:
    raw = arguments.get("expected_last_sequence")
    if raw is None or raw == "":
        return None
    try:
        revision = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="expected_last_sequence must be an integer") from exc
    if revision < 0:
        raise HTTPException(status_code=400, detail="expected_last_sequence must be non-negative")
    return revision


def _rewind_revision_conflict(*, expected: int, actual: int, reason: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "rewind_revision_conflict",
            "message": "The session changed before Rewind could be applied. Refresh the checkpoints and try again.",
            "expected_last_sequence": expected,
            "actual_last_sequence": actual,
            "reason": reason,
            "retryable": True,
        },
    )


async def _read_rewind_revision(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    lock: bool,
) -> int | None:
    if not isinstance(db, AsyncSession):
        return None
    return await read_transcript_revision(db, session_id=session_id, lock=lock)


async def _lock_rewind_session_row(db: AsyncSession, *, session_id: uuid.UUID) -> None:
    if not isinstance(db, AsyncSession):
        return
    locked_session_id = (
        await db.execute(select(ChatSession.id).where(ChatSession.id == session_id).with_for_update())
    ).scalar_one_or_none()
    if locked_session_id is None:
        raise HTTPException(status_code=404, detail="Session not found while preparing Rewind")


async def _prepare_rewind_mutation(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    arguments: dict[str, Any],
    observed_last_sequence: int,
) -> dict[str, Any]:
    """Interrupt an active turn, then CAS the transcript before mutating projection/workspace."""

    current_before = await _read_rewind_revision(db, session_id=session.id, lock=False)
    if current_before is None:
        current_before = observed_last_sequence
    expected = _expected_rewind_revision(arguments)
    if expected is not None and expected != current_before:
        raise _rewind_revision_conflict(
            expected=expected,
            actual=current_before,
            reason="stale_client_revision",
        )

    active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    interrupted_run_id: str | None = None
    if active_run is not None:
        raw_run_id = active_run.get("run_id") or active_run.get("id") or active_run.get("runtime_task_id")
        if raw_run_id:
            run_id = _parse_uuid_argument(raw_run_id, field="run_id")
            try:
                await submit_live_cancel_input(
                    db=db,
                    agent=agent,
                    user=user,
                    session=session,
                    run_id=run_id,
                    source="session_command_rewind",
                    idempotency_key=str(arguments.get("idempotency_key") or f"session-command-rewind:{run_id}"),
                )
                interrupted_run_id = str(run_id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise

    current_after = await _read_rewind_revision(db, session_id=session.id, lock=True)
    if current_after is None:
        current_after = current_before
    await _lock_rewind_session_row(db, session_id=session.id)
    if current_after != current_before:
        raise _rewind_revision_conflict(
            expected=current_before,
            actual=current_after,
            reason="transcript_changed_during_interrupt",
        )
    remaining_active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    if remaining_active_run is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "rewind_active_run_conflict",
                "message": "The active turn did not stop cleanly. Wait for it to settle, then retry Rewind.",
                "retryable": True,
            },
        )
    return {
        "last_sequence": current_after,
        "interrupted_active_run": interrupted_run_id is not None,
        "interrupted_run_id": interrupted_run_id,
        "cas": "transcript_advisory_lock",
    }


def _event_to_summary_message(event: ChatTranscriptEvent | T0SessionEvent) -> dict[str, str] | None:
    content = (getattr(event, "content", None) or "").strip()
    if not content:
        return None
    role = _event_role(event) or "system"
    if role == "tool_call":
        role = "tool"
    if role not in {"user", "assistant", "tool", "system"}:
        role = "system"
    return {"role": role, "content": content}


def _events_to_summary_messages(events: list[ChatTranscriptEvent | T0SessionEvent]) -> list[dict[str, str]]:
    # Session V2 parity: one authoritative HumanInput checkpoint per item with
    # its exact rendered content enters the compact summary; superseded
    # accepted bytes and queued/bound/applied state rows never do. Legacy rows
    # keep their exact V1 projection and ordering.
    checkpoint_row_ids = {_event_anchor_id(event) for event in user_checkpoint_events(events)}
    messages: list[dict[str, str]] = []
    for event in events:
        if is_human_input_row(event):
            if _event_anchor_id(event) in checkpoint_row_ids:
                content = user_checkpoint_content(event)
                # Strip decides emptiness only; the appended bytes stay exact.
                if content.strip():
                    messages.append({"role": "user", "content": content})
            continue
        message = _event_to_summary_message(event)
        if message is not None:
            messages.append(message)
    return messages


def _assistant_copy_candidates(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> list[ChatTranscriptEvent | T0SessionEvent]:
    candidates: list[ChatTranscriptEvent | T0SessionEvent] = []
    for event in reversed(events):
        metadata = _event_metadata(event)
        if _event_role(event) != "assistant":
            continue
        if metadata.get("is_api_error_message") or metadata.get("api_error"):
            continue
        if not (getattr(event, "content", None) or "").strip():
            continue
        candidates.append(event)
    return candidates


def _extract_code_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, match in enumerate(_FENCED_CODE_BLOCK_RE.finditer(markdown)):
        lang = re.sub(r"[^A-Za-z0-9_+.-]", "", match.group(1).strip())
        blocks.append({"index": index, "lang": lang or None, "code": match.group(2)})
    return blocks


def _positive_int(value: Any, *, default: int, field: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a positive integer") from exc
    if parsed < 1:
        raise HTTPException(status_code=400, detail=f"{field} must be a positive integer")
    return parsed


async def _load_db_events(db: AsyncSession, *, session: ChatSession, limit: int = 500) -> list[ChatTranscriptEvent]:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.listed_surface == "chat")
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars().all())))


async def _load_events(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    limit: int = 500,
) -> tuple[list[ChatTranscriptEvent | T0SessionEvent], str]:
    """Load the NEWEST ``limit`` events, ascending (events[-1] is the latest).

    The committed DB stream is cloud run truth. T0 is a portable Memory
    evidence projection and remains a recovery fallback for legacy/imported
    sessions whose DB event stream is absent.
    """
    if db is not None:
        try:
            db_events = await _load_db_events(db, session=session, limit=limit)
        except Exception:  # noqa: BLE001 - legacy T0 evidence remains an observable recovery path.
            db_events = []
        if db_events:
            return db_events, "chat_transcript_events"
    try:
        t0_events = await asyncio.to_thread(
            replay_t0_session_events_tail,
            agent_id=agent.id,
            session_id=session.id,
            limit=limit,
        )
    except Exception:  # noqa: BLE001 - command surface must fall back to DB read model if files are unavailable.
        t0_events = []
    if t0_events:
        return list(t0_events), "t0_events_jsonl_fallback"
    return [], "chat_transcript_events"


async def _last_event(db: AsyncSession, *, session: ChatSession) -> ChatTranscriptEvent | None:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.listed_surface == "chat")
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_anchor_event_id(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    arguments: dict[str, Any],
) -> uuid.UUID:
    raw = arguments.get("anchor_event_id") or arguments.get("event_id")
    if raw:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    events, _truth_source = await _load_events(db, agent=agent, session=session, limit=1000)
    event = events[-1] if events else await _last_event(db, session=session)
    if event is None:
        raise HTTPException(status_code=400, detail="Cannot branch or rewind an empty transcript")
    return _branch_anchor_event_id(event)


def _select_user_checkpoint(
    events: list[ChatTranscriptEvent | T0SessionEvent],
    *,
    arguments: dict[str, Any],
    default_num_turns: int = 1,
) -> tuple[ChatTranscriptEvent | T0SessionEvent, int]:
    raw_checkpoint = (
        arguments.get("checkpoint_event_id") or arguments.get("anchor_event_id") or arguments.get("event_id")
    )
    checkpoints = _user_checkpoint_events(events)
    if raw_checkpoint:
        for index, event in enumerate(checkpoints, 1):
            if _event_anchor_id(event) == str(raw_checkpoint) or _event_ledger_id(event) == str(raw_checkpoint):
                return event, index
        raise HTTPException(status_code=404, detail="User-message checkpoint not found in this transcript")

    num_turns = _positive_int(arguments.get("num_turns"), default=default_num_turns, field="num_turns")
    if len(checkpoints) < num_turns:
        raise HTTPException(status_code=400, detail="Not enough user-message checkpoints to roll back that many turns")
    target = checkpoints[-num_turns]
    return target, len(checkpoints) - num_turns + 1


async def _export_session(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    events, truth_source = await _load_events(db, agent=agent, session=session)
    db_events = events if truth_source == "chat_transcript_events" else await _load_db_events(db, session=session)
    messages_result = await db.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == str(session.id)).order_by(ChatMessage.created_at.asc())
    )
    messages = list(messages_result.scalars().all())
    artifact_result = await db.execute(
        select(ChatArtifact).where(ChatArtifact.session_id == session.id).order_by(ChatArtifact.created_at.asc())
    )
    artifacts = list(artifact_result.scalars().all())
    return {
        "session": {
            "id": str(session.id),
            "agent_id": str(session.agent_id),
            "title": session.title,
            "source_channel": session.source_channel,
            "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
            "root_session_id": str(session.root_session_id) if session.root_session_id else None,
            "metadata": session.transcript_metadata_json or {},
        },
        "truth_source": truth_source,
        "t0_events": [_event_payload(event) for event in events] if truth_source == "t0_events_jsonl_fallback" else [],
        "transcript_events": [_event_payload(event) for event in db_events],
        "messages": [
            {
                "id": str(message.id),
                "role": message.role,
                "content": message.content or "",
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in messages
        ],
        "artifacts": [
            {
                "id": str(artifact.id),
                "path": artifact.path,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
            }
            for artifact in artifacts
        ],
        "truth_surface": "t0_events_jsonl_memory_evidence_fallback"
        if truth_source == "t0_events_jsonl_fallback"
        else "chat_transcript_events_cloud_truth_with_t0_memory_projection",
    }


@dataclass(frozen=True, slots=True)
class SessionCommandContext:
    db: AsyncSession
    agent: Agent
    user: User
    access_level: str
    session_id: uuid.UUID | str | None
    arguments: dict[str, Any]


async def execute_session_command(
    context: SessionCommandContext,
    command_name: str,
) -> dict[str, Any]:
    """Dispatch one session command through its single typed owner."""
    handler = _SESSION_COMMAND_HANDLERS.get(command_name)
    if handler is None:
        raise HTTPException(status_code=501, detail=f"Unsupported session command {command_name!r}")
    session = await _load_session(
        context.db,
        agent=context.agent,
        user=context.user,
        session_id=context.session_id,
        access_level=context.access_level,
    )
    return await handler(context, session, command_name)


async def _handle_resume(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    events, truth_source = await _load_events(context.db, agent=context.agent, session=session)
    last_turn_event = _last_replayable_turn_event(events)
    checkpoints = _user_checkpoint_events(events)
    latest_runtime_state = await _latest_session_runtime_state(context.db, agent=context.agent, session=session)
    interrupted_tail = bool(last_turn_event and _is_interrupted_tail_event(last_turn_event))
    if latest_runtime_state == "needs_reconciliation":
        resume_state = "needs_reconciliation"
    elif latest_runtime_state in {"pending", "running", "suspended", "resumable"}:
        resume_state = "active"
    else:
        resume_state = "interrupted" if interrupted_tail else "ready"
    interrupted = resume_state == "interrupted"
    resume_checkpoint = checkpoints[-1] if interrupted and checkpoints else None
    return _typed_result(
        command="resume",
        action="resume_status",
        session_id=session.id,
        ui_action={
            "type": "open_resume_picker",
            "session_id": str(session.id),
            "interrupted": interrupted,
            "resume_state": resume_state,
        },
        truth_source=truth_source,
        event_count=len(events),
        checkpoint_count=len(checkpoints),
        resume_state=resume_state,
        interrupted=interrupted,
        repair_strategy="transcript_replay_chain_repair",
        raw_last_event_type=events[-1].event_type if events else None,
        last_replayable_event=_event_payload(last_turn_event) if last_turn_event else None,
        resume_from_checkpoint_event_id=_event_anchor_id(resume_checkpoint) if resume_checkpoint else None,
        repair_actions=[
            "ignore_non_turn_tail_events",
            "ignore_empty_assistant_messages",
            "continue_if_tail_is_user_or_tool_turn",
        ],
        next_query="Continue from where you left off." if interrupted else None,
    )


async def _handle_checkpoints(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    events, truth_source = await _load_events(
        context.db,
        agent=context.agent,
        session=session,
        limit=_positive_int(context.arguments.get("limit"), default=500, field="limit"),
    )
    checkpoints = _checkpoint_payloads(events)
    return _typed_result(
        command="checkpoints",
        action="checkpoints_listed",
        session_id=session.id,
        ui_action={"type": "open_checkpoint_selector", "session_id": str(session.id), "checkpoints": checkpoints},
        truth_source=truth_source,
        event_count=len(events),
        checkpoint_count=len(checkpoints),
        checkpoints=checkpoints,
        checkpoint_strategy="user_message_turn_boundary",
    )


async def _handle_copy(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    arguments = context.arguments
    events, truth_source = await _load_events(
        context.db,
        agent=context.agent,
        session=session,
        limit=_positive_int(arguments.get("limit"), default=500, field="limit"),
    )
    candidates = _assistant_copy_candidates(events)
    if not candidates:
        raise HTTPException(status_code=404, detail="No assistant message to copy")
    n = _positive_int(arguments["n"] if "n" in arguments else arguments.get("index"), default=1, field="n")
    if n > len(candidates):
        noun = "message" if len(candidates) == 1 else "messages"
        raise HTTPException(status_code=400, detail=f"Only {len(candidates)} assistant {noun} available to copy")
    event = candidates[n - 1]
    content = event.content or ""
    return _typed_result(
        command="copy",
        action="copy_ready",
        session_id=session.id,
        ui_action={
            "type": "copy_to_clipboard",
            "session_id": str(session.id),
            "content": content,
            "source_event_id": _event_anchor_id(event),
        },
        truth_source=truth_source,
        source_event_id=_event_anchor_id(event),
        ledger_event_id=_event_ledger_id(event),
        source_sequence=event.sequence,
        message_age=n - 1,
        available_assistant_messages=len(candidates),
        content=content,
        char_count=len(content),
        line_count=content.count("\n") + 1 if content else 0,
        code_blocks=_extract_code_blocks(content),
        copy_strategy="client_clipboard_or_file",
    )


async def _handle_rename(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    title = str(context.arguments.get("title") or context.arguments.get("name") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    session.title = title[:200]
    await context.db.flush()
    return _typed_result(
        command="rename",
        action="session_renamed",
        session_id=session.id,
        ui_action={"type": "toast", "level": "success", "message": "Session renamed."},
        title=session.title,
    )


async def _handle_tag(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    tags = context.arguments.get("tags")
    if isinstance(tags, str):
        tags = [tags]
    clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    metadata = dict(session.transcript_metadata_json or {})
    metadata["tags"] = sorted(set([*(metadata.get("tags") or []), *clean_tags]))
    session.transcript_metadata_json = metadata
    await context.db.flush()
    return _typed_result(
        command="tag",
        action="session_tagged",
        session_id=session.id,
        ui_action={"type": "toast", "level": "success", "message": "Session tags updated."},
        tags=metadata["tags"],
    )


async def _handle_export(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    exported = await _export_session(context.db, agent=context.agent, session=session)
    return _typed_result(
        command="export",
        action="export_ready",
        session_id=session.id,
        ui_action={"type": "open_export_panel", "session_id": str(session.id)},
        **exported,
    )


async def _handle_clear(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    agent, user, db = context.agent, context.user, context.db
    new_session = ChatSession(
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", getattr(user, "tenant_id", None)),
        user_id=user.id,
        title=str(context.arguments.get("title") or f"{session.title} (clear)")[:200],
        source_channel=session.source_channel,
        session_kind=session.session_kind,
        actor_type=session.actor_type,
        runtime_source=session.runtime_source,
        visibility_scope=session.visibility_scope,
        listed_surface=session.listed_surface,
        parent_session_id=session.id,
        root_session_id=session.root_session_id or session.id,
        transcript_metadata_json={"command": "clear", "source_session_id": str(session.id), "keeps_evidence": True},
    )
    db.add(new_session)
    await db.flush()
    control_event = await _append_control_event(
        db=db,
        agent=agent,
        session=session,
        user=user,
        event_type="session_clear",
        content=f"Started fresh context session {new_session.id}",
        metadata={
            "command": "clear",
            "source_session_id": str(session.id),
            "new_session_id": str(new_session.id),
            "keeps_evidence": True,
        },
    )
    return _typed_result(
        command="clear",
        action="session_created",
        session_id=new_session.id,
        ui_action={"type": "switch_session", "session_id": str(new_session.id), "reason": "clear"},
        control_event=control_event,
        source_session_id=str(session.id),
        session=_session_payload(new_session),
    )


async def _handle_branch(context: SessionCommandContext, session: ChatSession, command: str) -> dict[str, Any]:
    agent, user, db, arguments = context.agent, context.user, context.db, context.arguments
    anchor = await _resolve_anchor_event_id(db, agent=agent, session=session, arguments=arguments)
    result = await create_conversation_branch(
        db=db,
        agent=agent,
        user=user,
        source_session=session,
        mode="branch",
        anchor_event_id=anchor,
        title=str(arguments.get("title") or f"{session.title} ({command})"),
    )
    branch = {**dict(result.branch), "command": command}
    control_event = await _append_control_event(
        db=db,
        agent=agent,
        session=session,
        user=user,
        event_type="session_branch",
        content=f"Created branch session {result.session.id}",
        metadata={
            "command": "branch",
            "source_session_id": str(session.id),
            "branch_session_id": str(result.session.id),
            "anchor_event_id": str(anchor),
            "branch_mode": "branch",
        },
    )
    return _typed_result(
        command=command,
        action="branch_created",
        session_id=result.session.id,
        ui_action={"type": "switch_session", "session_id": str(result.session.id), "reason": "branch"},
        control_event=control_event,
        source_session_id=str(session.id),
        session=_session_payload(result.session),
        branch=branch,
    )


async def _handle_btw(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    agent, user, db, arguments = context.agent, context.user, context.db, context.arguments
    question = str(
        arguments.get("question")
        or arguments.get("content")
        or arguments.get("message")
        or arguments.get("prompt")
        or ""
    ).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question or content is required")
    anchor = await _resolve_anchor_event_id(db, agent=agent, session=session, arguments=arguments)
    result = await create_conversation_branch(
        db=db,
        agent=agent,
        user=user,
        source_session=session,
        mode="side_question",
        anchor_event_id=anchor,
        content=question,
        display_content=str(arguments.get("display_content") or f"btw: {question}"),
        title=str(arguments.get("title") or f"{session.title} (btw)"),
    )
    return _typed_result(
        command="btw",
        action="side_question_opened",
        session_id=session.id,
        ui_action={
            "type": "open_side_question",
            "session_id": str(session.id),
            "side_session_id": str(result.session.id),
        },
        source_session_id=str(session.id),
        session=_session_payload(result.session),
        branch={**dict(result.branch), "command": "btw"},
        run_request=_run_request_payload(result.run_request),
    )


async def _handle_steer(context: SessionCommandContext, session: ChatSession, command: str) -> dict[str, Any]:
    arguments = context.arguments
    content = str(arguments.get("content") or arguments.get("message") or arguments.get("input") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    active = await get_active_web_chat_run(db=context.db, agent_id=context.agent.id, session_id=session.id)
    if not active or not active.get("run_id") or not active.get("turn_id"):
        raise HTTPException(status_code=404, detail="No active turn to steer")
    input_id = _parse_uuid_argument(arguments.get("input_id") or uuid.uuid4(), field="input_id")
    result = await submit_live_human_input(
        db=context.db,
        agent=context.agent,
        user=context.user,
        session=session,
        content=content,
        source="session_command_steer",
        input_id=input_id,
        idempotency_key=str(arguments.get("idempotency_key") or f"session-command-steer:{input_id}"),
        requested_kind="steer_current_turn",
        expected_turn_id=str(active["turn_id"]),
        expected_run_id=_parse_uuid_argument(active["run_id"], field="run_id"),
        terminal_fallback=str(arguments.get("terminal_fallback") or "queue_next_turn"),
        display_content=str(arguments.get("display_content") or ""),
        file_name=str(arguments.get("file_name") or ""),
        attachments=arguments.get("attachments") if isinstance(arguments.get("attachments"), list) else None,
        parts=arguments.get("parts") if isinstance(arguments.get("parts"), list) else None,
    )
    return _typed_result(
        command=command,
        action="turn_steer_queued",
        session_id=session.id,
        ui_action={"type": "toast", "level": "success", "message": "Update queued for the active turn."},
        **result,
    )


async def _handle_interrupt(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    active = await get_active_web_chat_run(db=context.db, agent_id=context.agent.id, session_id=session.id)
    run_id = context.arguments.get("run_id") or (active or {}).get("run_id")
    if not run_id:
        raise HTTPException(status_code=404, detail="No active turn to interrupt")
    result = await submit_live_cancel_input(
        db=context.db,
        agent=context.agent,
        user=context.user,
        session=session,
        run_id=_parse_uuid_argument(run_id, field="run_id"),
        source="session_command_interrupt",
        idempotency_key=str(context.arguments.get("idempotency_key") or f"session-command-interrupt:{run_id}"),
        control_id=context.arguments.get("control_id"),
    )
    return _typed_result(
        command="interrupt",
        action="active_turn_interrupted",
        session_id=session.id,
        ui_action={"type": "toast", "level": "success", "message": "Active turn interrupted."},
        interrupt_strategy="session_v2_control_input",
        **result,
    )


@dataclass(slots=True)
class _RewindPlan:
    mode: str
    truth_source: str
    events: list[Any]
    checkpoints: list[dict[str, Any]]
    target: Any
    checkpoint: dict[str, Any]
    turn_index: int
    observed_revision: int
    paths: list[str]
    states: dict[str, dict[str, Any]]
    lineage: dict[str, list[dict[str, Any]]]


async def _handle_rewind(context: SessionCommandContext, session: ChatSession, command: str) -> dict[str, Any]:
    arguments = context.arguments
    events, truth_source = await _load_events(
        context.db,
        agent=context.agent,
        session=session,
        limit=_positive_int(arguments.get("limit"), default=1000, field="limit"),
    )
    checkpoints = _checkpoint_payloads(events)
    revision = _session_event_revision(events)
    if command == "rewind" and not _has_explicit_rewind_target(arguments):
        return _rewind_selector(session, events, checkpoints, truth_source, revision)
    mode = str(arguments.get("mode") or "conversation").strip().lower()
    if mode not in {"conversation", "workspace", "both"}:
        raise HTTPException(status_code=400, detail="mode must be conversation, workspace, or both")
    target, turn_index = _select_user_checkpoint(events, arguments=arguments)
    plan = _RewindPlan(
        mode=mode,
        truth_source=truth_source,
        events=events,
        checkpoints=checkpoints,
        target=target,
        checkpoint=_checkpoint_payload(target, turn_index=turn_index),
        turn_index=turn_index,
        observed_revision=revision,
        paths=[],
        states={},
        lineage={},
    )
    if mode in {"workspace", "both"}:
        blocked = _prepare_workspace_rewind(context, session, command, plan)
        if blocked is not None:
            return blocked
    guard = await _prepare_rewind_mutation(
        db=context.db,
        agent=context.agent,
        user=context.user,
        session=session,
        arguments=arguments,
        observed_last_sequence=revision,
    )
    restore = await _restore_workspace_rewind(context, session, command, plan)
    if isinstance(restore, dict) and restore.get("_rewind_blocked"):
        return {key: value for key, value in restore.items() if key != "_rewind_blocked"}
    if mode == "workspace":
        return await _apply_workspace_rewind(context, session, command, plan, guard, restore)
    return await _apply_projection_rewind(context, session, command, plan, guard, restore)


def _rewind_selector(
    session: ChatSession,
    events: list[Any],
    checkpoints: list[dict[str, Any]],
    truth_source: str,
    revision: int,
) -> dict[str, Any]:
    return _typed_result(
        command="rewind",
        action="open_checkpoint_selector",
        session_id=session.id,
        ui_action={"type": "open_checkpoint_selector", "session_id": str(session.id), "checkpoints": checkpoints},
        truth_source=truth_source,
        event_count=len(events),
        checkpoint_count=len(checkpoints),
        checkpoints=checkpoints,
        checkpoint_strategy="user_message_turn_boundary",
        rewind_guard={"last_sequence": revision},
    )


def _prepare_workspace_rewind(
    context: SessionCommandContext,
    session: ChatSession,
    command: str,
    plan: _RewindPlan,
) -> dict[str, Any] | None:
    metadata = getattr(session, "transcript_metadata_json", None)
    snapshots = metadata.get("workspace_snapshots") if isinstance(metadata, dict) else None
    if not isinstance(snapshots, dict) or plan.checkpoint["checkpoint_event_id"] not in snapshots:
        return _typed_result(
            command=command,
            action="not_supported",
            session_id=session.id,
            ok=False,
            ui_action={
                "type": "toast",
                "level": "warning",
                "message": "Workspace rewind is not available because this session has no workspace snapshot.",
            },
            debug_payload={"missing": "workspace_snapshot", "requested_mode": plan.mode},
            truth_source=plan.truth_source,
            checkpoint_count=len(plan.checkpoints),
        )
    if not context.arguments.get("confirm_workspace_restore"):
        expected = _expected_rewind_revision(context.arguments)
        if expected is not None and expected != plan.observed_revision:
            raise _rewind_revision_conflict(
                expected=expected,
                actual=plan.observed_revision,
                reason="stale_workspace_confirmation",
            )
        return _workspace_confirmation(session, command, plan)
    plan.paths, plan.states, plan.lineage, unverifiable = _workspace_restore_scope_after_checkpoint(
        plan.events,
        checkpoint=plan.target,
    )
    if unverifiable:
        return _typed_result(
            command=command,
            action="workspace_restore_conflict",
            session_id=session.id,
            ok=False,
            ui_action={
                "type": "toast",
                "level": "error",
                "message": (
                    "Workspace rewind cannot safely verify one or more paths. "
                    "Keep the current files or create a new checkpoint before retrying."
                ),
            },
            debug_payload={
                "requested_mode": plan.mode,
                "checkpoint_event_id": plan.checkpoint["checkpoint_event_id"],
                "unverifiable_paths": unverifiable,
            },
            truth_source=plan.truth_source,
            checkpoint=plan.checkpoint,
        )
    return None


def _workspace_confirmation(session: ChatSession, command: str, plan: _RewindPlan) -> dict[str, Any]:
    return _typed_result(
        command=command,
        action="workspace_restore_requires_confirmation",
        session_id=session.id,
        ok=False,
        ui_action={
            "type": "confirm_workspace_restore",
            "level": "warning",
            "message": (
                "Workspace rewind will restore only files changed by this session since the selected checkpoint. "
                "Any later or interleaved write to the same file stops the restore instead of overwriting it. "
                "Confirm before applying."
            ),
            "checkpoint_event_id": plan.checkpoint["checkpoint_event_id"],
            "requested_mode": plan.mode,
        },
        debug_payload={
            "requested_mode": plan.mode,
            "checkpoint_event_id": plan.checkpoint["checkpoint_event_id"],
        },
        truth_source=plan.truth_source,
        checkpoint=plan.checkpoint,
        rewind_guard={"last_sequence": plan.observed_revision},
    )


async def _restore_workspace_rewind(
    context: SessionCommandContext,
    session: ChatSession,
    command: str,
    plan: _RewindPlan,
) -> dict[str, Any] | None:
    if plan.mode not in {"workspace", "both"}:
        return None
    restore = await asyncio.to_thread(
        restore_session_workspace_snapshot,
        agent_id=context.agent.id,
        session=session,
        checkpoint_event_id=plan.checkpoint["checkpoint_event_id"],
        restore_paths=plan.paths,
        expected_current_states=plan.states,
        expected_lineage=plan.lineage,
        defer_finalize=True,
    )
    if restore.ok:
        return restore.to_payload()
    return {
        "_rewind_blocked": True,
        **_typed_result(
            command=command,
            action="workspace_restore_failed",
            session_id=session.id,
            ok=False,
            ui_action={"type": "toast", "level": "error", "message": restore.error or "Workspace rewind failed."},
            debug_payload={"requested_mode": plan.mode, **restore.to_payload()},
            truth_source=plan.truth_source,
            checkpoint=plan.checkpoint,
        ),
    }


async def _apply_workspace_rewind(
    context: SessionCommandContext,
    session: ChatSession,
    command: str,
    plan: _RewindPlan,
    guard: dict[str, Any],
    restore: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        event = await _append_control_event(
            db=context.db,
            agent=context.agent,
            session=session,
            user=context.user,
            event_type="session_workspace_rewind",
            content=f"Restored workspace snapshot at checkpoint {plan.checkpoint['checkpoint_event_id']}",
            metadata={"command": command, "mode": plan.mode, "workspace_restore": restore},
        )
        await context.db.flush()
    except BaseException:
        await _rollback_deferred_workspace_restore(agent_id=context.agent.id, workspace_restore_payload=restore)
        raise
    return _typed_result(
        command=command,
        action="workspace_rewind_applied",
        session_id=session.id,
        ui_action={
            "type": "install_workspace_snapshot",
            "session_id": str(session.id),
            "message": "Workspace snapshot restored for this session.",
        },
        control_event=event,
        truth_source=plan.truth_source,
        checkpoint=plan.checkpoint,
        workspace_restore=restore,
        rewind_guard=guard,
    )


async def _apply_projection_rewind(
    context: SessionCommandContext,
    session: ChatSession,
    command: str,
    plan: _RewindPlan,
    guard: dict[str, Any],
    restore: dict[str, Any] | None,
) -> dict[str, Any]:
    projection = {
        "projection_reason": "rewind",
        "checkpoint_event_id": plan.checkpoint["checkpoint_event_id"],
        "ledger_event_id": plan.checkpoint.get("ledger_event_id"),
        "draft_content": plan.checkpoint.get("content") or "",
        "turn_index": plan.turn_index,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "truth_source": plan.truth_source,
        "mode": plan.mode,
        "rewind_guard": guard,
    }
    metadata = dict(session.transcript_metadata_json or {})
    metadata["active_projection"] = projection
    session.transcript_metadata_json = metadata
    try:
        event = await _append_control_event(
            db=context.db,
            agent=context.agent,
            session=session,
            user=context.user,
            event_type="session_rewind" if restore is None else "session_rewind_with_workspace",
            content=f"Rewound active projection to checkpoint {plan.checkpoint['checkpoint_event_id']}",
            metadata={"command": command, **projection, "workspace_restore": restore},
        )
        await context.db.flush()
    except BaseException:
        await _rollback_deferred_workspace_restore(agent_id=context.agent.id, workspace_restore_payload=restore)
        raise
    return _typed_result(
        command=command,
        action="rewind_applied",
        session_id=session.id,
        ui_action={
            "type": "install_active_projection" if restore is None else "install_active_projection_with_workspace",
            "session_id": str(session.id),
            "projection_reason": "rewind",
            "checkpoint_event_id": plan.checkpoint["checkpoint_event_id"],
            "draft_content": plan.checkpoint.get("content") or "",
            **({"message": "Session projection and workspace snapshot restored."} if restore is not None else {}),
        },
        control_event=event,
        truth_source=plan.truth_source,
        checkpoint=plan.checkpoint,
        workspace_restore=restore,
        rewind_guard=guard,
        rollback={
            "strategy": "active_projection_rewind",
            "num_turns": _positive_int(context.arguments.get("num_turns"), default=1, field="num_turns")
            if command == "rollback"
            else 1,
        },
    )


async def _handle_compact(context: SessionCommandContext, session: ChatSession, _command: str) -> dict[str, Any]:
    agent, user, arguments = context.agent, context.user, context.arguments
    reason = str(arguments.get("reason") or "manual compact command").strip()
    events, truth_source = await _load_events(
        context.db,
        agent=agent,
        session=session,
        limit=_positive_int(arguments.get("limit"), default=1000, field="limit"),
    )
    messages = _events_to_summary_messages(events)
    if not messages:
        return _typed_result(
            command="compact",
            action="not_supported",
            session_id=session.id,
            ok=False,
            ui_action={"type": "toast", "level": "warning", "message": "No session messages are available to compact."},
            debug_payload={"missing": "session_messages", "truth_source": truth_source},
        )
    await emit_hook(
        HookEvent.PRE_COMPACTION,
        evidence_mode="independent",
        agent_id=agent.id,
        session_id=str(session.id),
        source="command",
        messages=messages,
        metadata={"tenant_id": str(getattr(agent, "tenant_id", "") or ""), "reason": reason},
    )
    summary = await _generate_session_summary(
        messages,
        getattr(agent, "tenant_id", getattr(session, "tenant_id", None)),
        agent_id=agent.id,
        user_id=getattr(user, "id", None),
    )
    if not summary:
        return _typed_result(
            command="compact",
            action="not_supported",
            session_id=session.id,
            ok=False,
            ui_action={
                "type": "toast",
                "level": "warning",
                "message": "Compaction summary model is unavailable; context was not changed.",
            },
            debug_payload={"missing": "summary_model_or_summary", "truth_source": truth_source},
        )
    keep_recent = _positive_int(arguments.get("keep_recent"), default=10, field="keep_recent")
    recent = messages[-keep_recent:] if len(messages) > keep_recent else []
    replacement = [_wrap_compressed_summary(summary), *recent]
    projection = {
        "projection_reason": "compact",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "truth_source": truth_source,
        "reason": reason,
        "summary": summary,
        "original_message_count": len(messages),
        "kept_message_count": len(replacement),
        "replacement_messages": replacement,
    }
    metadata = dict(session.transcript_metadata_json or {})
    metadata["active_projection"] = projection
    session.transcript_metadata_json = metadata
    event = await _append_control_event(
        db=context.db,
        agent=agent,
        session=session,
        user=user,
        event_type="session_compact",
        content=summary,
        metadata={"command": "compact", "manual": True, **projection},
    )
    await context.db.flush()
    await emit_hook(
        HookEvent.POST_COMPACTION,
        evidence_db=context.db,
        agent_id=agent.id,
        session_id=str(session.id),
        source="command",
        metadata={
            "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
            "reason": reason,
            "summary": summary,
            "control_event_id": event.get("event_id"),
        },
    )
    return _typed_result(
        command="compact",
        action="compacted_context_installed",
        session_id=session.id,
        ui_action={
            "type": "install_compacted_context",
            "session_id": str(session.id),
            "message": "Compacted current context.",
        },
        control_event=event,
        debug_payload={"replacement_messages": replacement, "truth_source": truth_source},
        transcript_event_id=event.get("event_id"),
        hook_events=[HookEvent.PRE_COMPACTION.value, HookEvent.POST_COMPACTION.value],
        summary=summary,
        original_message_count=len(messages),
        kept_message_count=len(replacement),
    )


_SESSION_COMMAND_HANDLERS = {
    "resume": _handle_resume,
    "checkpoints": _handle_checkpoints,
    "copy": _handle_copy,
    "rename": _handle_rename,
    "tag": _handle_tag,
    "export": _handle_export,
    "clear": _handle_clear,
    "branch": _handle_branch,
    "btw": _handle_btw,
    "turn_steer": _handle_steer,
    "steer": _handle_steer,
    "interrupt": _handle_interrupt,
    "rewind": _handle_rewind,
    "rollback": _handle_rewind,
    "compact": _handle_compact,
}
