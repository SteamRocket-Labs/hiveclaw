from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from app.core.events import get_redis
from app.runtime.hooks import HookEvent, emit_hook


RUNTIME_CONTROL_SCHEMA = "hive.runtime.control.v1"
RUNTIME_CONTROL_CHANNEL = "hive:runtime:control"
RUNTIME_CONTROL_RECONNECT_SECONDS = 2.0
SESSION_LIFECYCLE_MESSAGE_LIMIT = 100
_STATE: dict[str, Any] = {
    "running": False,
    "received": 0,
    "last_type": None,
    "last_error": None,
    "restart_count": 0,
    "last_restart_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def publish_runtime_control_event(payload: dict[str, Any]) -> None:
    envelope = {
        "schema": RUNTIME_CONTROL_SCHEMA,
        "created_at": _now_iso(),
        **payload,
    }
    redis = await get_redis()
    await redis.publish(RUNTIME_CONTROL_CHANNEL, json.dumps(envelope, ensure_ascii=False, default=str))


async def publish_web_chat_cancel(
    *,
    run_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    user_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "web_chat_cancel",
            "run_id": str(run_id).replace("-", ""),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id is not None else None,
        }
    )


async def publish_delegation_cancel(
    *,
    task_id: str,
    parent_agent_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "delegation_cancel",
            "task_id": str(task_id),
            "parent_agent_id": str(parent_agent_id) if parent_agent_id is not None else None,
        }
    )


async def publish_transcript_t0_bridge(
    *,
    transcript_event_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    tenant_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "transcript_t0_bridge",
            "transcript_event_id": str(transcript_event_id),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
        }
    )


async def publish_session_lifecycle_hook(
    *,
    event: str | HookEvent,
    agent_id: str | Any,
    session_id: str | Any | None,
    messages: list[dict[str, Any]] | None = None,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    event_value = event.value if isinstance(event, HookEvent) else str(event)
    clean_metadata = dict(metadata or {})
    if messages is not None:
        clean_metadata.setdefault("message_count", len(messages))
    await publish_runtime_control_event(
        {
            "type": "session_lifecycle_hook",
            "event": event_value,
            "agent_id": str(agent_id),
            "session_id": str(session_id) if session_id is not None else None,
            "source": source,
            "metadata": clean_metadata,
        }
    )


def _uuid_or_none(value: str | Any | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _load_session_lifecycle_messages(
    *,
    agent_id: str | Any | None,
    session_id: str | Any | None,
    limit: int = SESSION_LIFECYCLE_MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    if not session_id:
        return []

    from app.database import async_session, enter_rls_bypass
    from app.models.audit import ChatMessage

    filters = [ChatMessage.conversation_id == str(session_id)]
    agent_uuid = _uuid_or_none(agent_id)
    if agent_uuid is not None:
        filters.append(ChatMessage.agent_id == agent_uuid)

    async with async_session() as db:
        async with enter_rls_bypass(db, reason=f"runtime control lifecycle hook session load {session_id}") as bypass_db:
            result = await bypass_db.execute(
                select(ChatMessage).where(*filters).order_by(ChatMessage.created_at.desc()).limit(max(1, limit))
            )
            rows = list(result.scalars().all())

    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows if row.content is not None]


async def bridge_transcript_event_to_t0(
    *,
    transcript_event_id: str | Any,
    attempts: int = 40,
    retry_delay_seconds: float = 0.25,
) -> bool:
    event_uuid = _uuid_or_none(transcript_event_id)
    if event_uuid is None:
        _STATE["last_error"] = f"ValueError: invalid transcript_event_id {transcript_event_id!r}"
        return False

    from app.database import async_session, enter_rls_bypass
    from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent

    attempts = max(1, attempts)
    for attempt in range(attempts):
        async with async_session() as db:
            async with enter_rls_bypass(db, reason=f"runtime control transcript T0 bridge {event_uuid}") as bypass_db:
                result = await bypass_db.execute(
                    select(ChatTranscriptEvent).where(ChatTranscriptEvent.id == event_uuid).with_for_update()
                )
                transcript_event = result.scalar_one_or_none()
                if transcript_event is not None:
                    metadata = dict(transcript_event.metadata_json or {})
                    if metadata.get("t0_bridge_relayed_at"):
                        return True

                    chat_message = None
                    if transcript_event.message_id:
                        chat_message = await bypass_db.get(ChatMessage, transcript_event.message_id)
                    actor_id = getattr(chat_message, "user_id", None) or transcript_event.agent_id
                    role = metadata.get("t0_role") or metadata.get("role")
                    source = str(metadata.get("source") or "runtime_control")

                    existing_t0_event = next(
                        (
                            event
                            for event in replay_t0_session_events(
                                agent_id=transcript_event.agent_id,
                                session_id=transcript_event.session_id,
                            )
                            if str(event.metadata.get("transcript_event_id") or "") == str(event_uuid)
                        ),
                        None,
                    )
                    if existing_t0_event is None:
                        t0_result = append_t0_session_event(
                            agent_id=transcript_event.agent_id,
                            session_id=transcript_event.session_id,
                            event_type=transcript_event.event_type,
                            role=str(role) if role else None,
                            content=transcript_event.content or "",
                            message_id=transcript_event.message_id,
                            actor_id=actor_id,
                            tenant_id=transcript_event.tenant_id,
                            runtime_task_id=transcript_event.run_id,
                            source=source,
                            metadata=metadata,
                            created_at=transcript_event.created_at,
                        )
                        segment_id = t0_result.segment_id
                        t0_event_id = t0_result.event_id
                        t0_sequence = t0_result.sequence
                    else:
                        segment_id = existing_t0_event.segment_id
                        t0_event_id = existing_t0_event.event_id
                        t0_sequence = existing_t0_event.sequence
                    metadata.update(
                        {
                            "t0_bridge_pending": False,
                            "t0_bridge_relayed_at": _now_iso(),
                            "t0_bridge_relay_source": "runtime_control_bus",
                            "t0_bridge_segment_id": segment_id,
                            "t0_bridge_event_id": t0_event_id,
                            "t0_bridge_sequence": t0_sequence,
                        }
                    )
                    transcript_event.metadata_json = metadata
                    await bypass_db.commit()
                    return True

        if attempt < attempts - 1:
            await asyncio.sleep(retry_delay_seconds)

    _STATE["last_error"] = f"LookupError: transcript_event {event_uuid} not visible after {attempts} attempts"
    return False


async def handle_runtime_control_message(message: dict[str, Any]) -> bool:
    if message.get("schema") != RUNTIME_CONTROL_SCHEMA:
        return False
    event_type = str(message.get("type") or "")
    _STATE["received"] = int(_STATE.get("received") or 0) + 1
    _STATE["last_type"] = event_type

    if event_type == "web_chat_cancel":
        from app.services.web_chat_runtime import apply_remote_web_chat_cancel

        return bool(await _maybe_await(apply_remote_web_chat_cancel(str(message.get("run_id") or ""))))

    if event_type == "delegation_cancel":
        from app.agents.orchestrator import apply_remote_async_delegation_cancel

        return bool(await _maybe_await(apply_remote_async_delegation_cancel(str(message.get("task_id") or ""))))

    if event_type == "transcript_t0_bridge":
        return bool(
            await bridge_transcript_event_to_t0(transcript_event_id=str(message.get("transcript_event_id") or ""))
        )

    if event_type == "session_lifecycle_hook":
        hook_event = HookEvent(str(message.get("event") or ""))
        messages = message.get("messages")
        if not isinstance(messages, list):
            messages = await _maybe_await(
                _load_session_lifecycle_messages(agent_id=message.get("agent_id"), session_id=message.get("session_id"))
            )
        await emit_hook(
            hook_event,
            agent_id=message.get("agent_id"),
            session_id=message.get("session_id"),
            messages=messages,
            source=str(message.get("source") or "runtime_control"),
            metadata=message.get("metadata") if isinstance(message.get("metadata"), dict) else {},
        )
        return True

    return False


async def _listen_runtime_control_once() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(RUNTIME_CONTROL_CHANNEL)
    async for raw_message in pubsub.listen():
        if raw_message.get("type") != "message":
            continue
        try:
            raw = raw_message.get("data")
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            await handle_runtime_control_message(payload)
        except Exception as exc:  # noqa: BLE001 - one malformed control event must not stop listener.
            _STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.warning("[RuntimeControlBus] failed to handle control message: {}", exc)


async def start_runtime_control_listener(
    *,
    reconnect_delay_seconds: float = RUNTIME_CONTROL_RECONNECT_SECONDS,
) -> None:
    _STATE.update({"running": True, "last_error": None})
    try:
        while True:
            try:
                await _listen_runtime_control_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - process can still run; reconnect keeps control plane live.
                _STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
                _STATE["last_restart_at"] = _now_iso()
                logger.warning("[RuntimeControlBus] listener reconnecting after error: {}", exc)
                await asyncio.sleep(max(0.0, reconnect_delay_seconds))
                continue
            _STATE["last_error"] = "RuntimeControlListenerEnded: pubsub listener returned"
            _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
            _STATE["last_restart_at"] = _now_iso()
            logger.warning("[RuntimeControlBus] listener returned; reconnecting")
            await asyncio.sleep(max(0.0, reconnect_delay_seconds))
    except asyncio.CancelledError:
        raise
    finally:
        _STATE["running"] = False


def runtime_control_bus_snapshot() -> dict[str, Any]:
    return dict(_STATE)
