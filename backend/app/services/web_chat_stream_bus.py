from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.core.events import get_redis


WEB_CHAT_STREAM_SCHEMA = "hive.web_chat.stream.v1"
WEB_CHAT_STREAM_LIVE_CHANNEL = "hive:web_chat:stream:live"
WEB_CHAT_STREAM_MAXLEN = 10_000
_FORWARDER_STATE: dict[str, Any] = {
    "running": False,
    "forwarded": 0,
    "last_sequence": None,
    "last_error": None,
}


def web_chat_run_stream_key(run_id: str) -> str:
    return f"hive:web_chat:stream:{run_id}"


def web_chat_run_sequence_key(run_id: str) -> str:
    return f"hive:web_chat:stream:{run_id}:seq"


async def publish_web_chat_stream_event(
    *,
    tenant_id: Any,
    agent_id: Any,
    session_id: Any,
    run_id: Any,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not run_id:
        return None
    run_key = str(run_id)
    envelope = {
        "schema": WEB_CHAT_STREAM_SCHEMA,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "agent_id": str(agent_id),
        "session_id": str(session_id) if session_id else None,
        "run_id": run_key,
        "sequence": None,
        "event_type": event_type,
        "payload": payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        redis = await get_redis()
        sequence = int(await redis.incr(web_chat_run_sequence_key(run_key)))
        envelope["sequence"] = sequence
        serialized = json.dumps(envelope, ensure_ascii=False, default=str)
        await redis.xadd(
            web_chat_run_stream_key(run_key),
            {"envelope": serialized},
            maxlen=WEB_CHAT_STREAM_MAXLEN,
            approximate=True,
        )
        await redis.publish(WEB_CHAT_STREAM_LIVE_CHANNEL, serialized)
        return envelope
    except Exception as exc:  # noqa: BLE001 - live stream must not fail the run.
        logger.warning("[WebChatStreamBus] publish failed for run {}: {}", run_key, exc)
        return None


async def start_web_chat_stream_forwarder() -> None:
    from app.services.web_chat_broker import web_chat_broker

    _FORWARDER_STATE.update({"running": True, "last_error": None})
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(WEB_CHAT_STREAM_LIVE_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                raw = message.get("data")
                envelope = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                payload = dict(envelope.get("payload") or {})
                agent_id = envelope.get("agent_id")
                session_id = envelope.get("session_id")
                if not agent_id:
                    continue
                await web_chat_broker.send_session_message(str(agent_id), str(session_id) if session_id else None, payload)
                _FORWARDER_STATE["forwarded"] = int(_FORWARDER_STATE.get("forwarded") or 0) + 1
                _FORWARDER_STATE["last_sequence"] = envelope.get("sequence")
            except Exception as exc:  # noqa: BLE001 - one malformed event must not stop the forwarder.
                _FORWARDER_STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.warning("[WebChatStreamBus] forward failed: {}", exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _FORWARDER_STATE["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("[WebChatStreamBus] forwarder stopped: {}", exc)
    finally:
        _FORWARDER_STATE["running"] = False


def web_chat_stream_forwarder_snapshot() -> dict[str, Any]:
    return dict(_FORWARDER_STATE)
