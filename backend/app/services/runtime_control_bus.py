from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.core.events import get_redis
from app.runtime.hooks import HookEvent, emit_hook


RUNTIME_CONTROL_SCHEMA = "hive.runtime.control.v1"
RUNTIME_CONTROL_CHANNEL = "hive:runtime:control"
_STATE: dict[str, Any] = {
    "running": False,
    "received": 0,
    "last_type": None,
    "last_error": None,
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


async def publish_session_lifecycle_hook(
    *,
    event: str | HookEvent,
    agent_id: str | Any,
    session_id: str | Any | None,
    messages: list[dict[str, Any]],
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    event_value = event.value if isinstance(event, HookEvent) else str(event)
    await publish_runtime_control_event(
        {
            "type": "session_lifecycle_hook",
            "event": event_value,
            "agent_id": str(agent_id),
            "session_id": str(session_id) if session_id is not None else None,
            "messages": messages,
            "source": source,
            "metadata": dict(metadata or {}),
        }
    )


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

    if event_type == "session_lifecycle_hook":
        hook_event = HookEvent(str(message.get("event") or ""))
        await emit_hook(
            hook_event,
            agent_id=message.get("agent_id"),
            session_id=message.get("session_id"),
            messages=message.get("messages") if isinstance(message.get("messages"), list) else [],
            source=str(message.get("source") or "runtime_control"),
            metadata=message.get("metadata") if isinstance(message.get("metadata"), dict) else {},
        )
        return True

    return False


async def start_runtime_control_listener() -> None:
    _STATE.update({"running": True, "last_error": None})
    try:
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - process can still run; API falls back to DB state.
        _STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("[RuntimeControlBus] listener stopped: {}", exc)
    finally:
        _STATE["running"] = False


def runtime_control_bus_snapshot() -> dict[str, Any]:
    return dict(_STATE)
