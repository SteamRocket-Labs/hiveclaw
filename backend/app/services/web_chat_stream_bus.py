from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.core.events import get_redis
from app.services.execution_receipts import canonical_payload_hash
from app.services.session_event_contract import serialize_session_event


WEB_CHAT_STREAM_SCHEMA = "hive.web_chat.stream.v1"
WEB_CHAT_STREAM_LIVE_CHANNEL = "hive:web_chat:stream:live"
WEB_CHAT_STREAM_MAXLEN = 10_000
SESSION_EVENT_LIVE_CHANNEL = "hive:session:event:v2:live"
WEB_CHAT_STREAM_RECONNECT_SECONDS = 2.0
_FORWARDER_STATE: dict[str, Any] = {
    "running": False,
    "forwarded": 0,
    "last_sequence": None,
    "last_error": None,
    "restart_count": 0,
    "last_restart_at": None,
}


async def publish_canonical_session_event(payload: dict[str, Any]) -> None:
    """Broadcast one DB-sequenced user projection without inventing a sequence.

    The durable outbox owns the operator envelope and validates its source hash
    before this boundary.  Redis and WebSocket are user-delivery transports, so
    provider-private bytes must be removed before publication.  The redacted
    event is still delivered with its original identity and sequence: dropping
    a private event would create a gap that makes the browser buffer every later
    public commentary/final event until a REST replay repairs the cursor.
    """

    required = {"event_id", "session_id", "sequence", "envelope_sha256", "envelope"}
    if not required.issubset(payload):
        raise ValueError("canonical_session_event_delivery_fields_missing")
    source_envelope = dict(payload.get("envelope") or {})
    source_envelope_sha256 = str(payload.get("envelope_sha256") or "")
    if canonical_payload_hash(source_envelope) != source_envelope_sha256:
        raise ValueError("canonical_session_event_source_hash_mismatch")
    user_envelope = serialize_session_event(source_envelope, audience="user")
    delivery = {
        **payload,
        "projection_audience": "user",
        "source_envelope_sha256": source_envelope_sha256,
        "envelope_sha256": canonical_payload_hash(user_envelope),
        "envelope": user_envelope,
    }
    redis = await get_redis()
    await redis.publish(
        SESSION_EVENT_LIVE_CHANNEL,
        json.dumps(delivery, ensure_ascii=False, sort_keys=True, default=str),
    )


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


async def _listen_web_chat_stream_once() -> None:
    from app.services.web_chat_broker import web_chat_broker

    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(WEB_CHAT_STREAM_LIVE_CHANNEL, SESSION_EVENT_LIVE_CHANNEL)
    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        try:
            raw = message.get("data")
            envelope = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            if envelope.get("schema") == "hive.session_event.delivery":
                delivered_envelope = dict(envelope.get("envelope") or {})
                if canonical_payload_hash(delivered_envelope) != str(envelope.get("envelope_sha256") or ""):
                    raise ValueError("canonical_session_event_delivery_hash_mismatch")
                # Rolling-deploy compatibility: an older runtime publisher may
                # still put the operator envelope on Redis while a newer API
                # forwarder is already serving sockets.  Project it here too;
                # new publishers mark their already-redacted envelope so the
                # non-idempotent JSON-pointer redaction is not applied twice.
                payload = (
                    delivered_envelope
                    if envelope.get("projection_audience") == "user"
                    else serialize_session_event(delivered_envelope, audience="user")
                )
                agent_id = envelope.get("agent_id")
                session_id = envelope.get("session_id")
                if (
                    payload.get("schema") != "hive.session_event"
                    or str(payload.get("event_id") or "") != str(envelope.get("event_id") or "")
                    or int(payload.get("sequence") or 0) != int(envelope.get("sequence") or 0)
                ):
                    raise ValueError("canonical_session_event_delivery_mismatch")
            else:
                payload = dict(envelope.get("payload") or {})
                agent_id = envelope.get("agent_id")
                session_id = envelope.get("session_id")
            if not agent_id:
                continue
            await web_chat_broker.send_session_message(str(agent_id), str(session_id) if session_id else None, payload)
            _FORWARDER_STATE["forwarded"] = int(_FORWARDER_STATE.get("forwarded") or 0) + 1
            _FORWARDER_STATE["last_sequence"] = envelope.get("sequence")
        except Exception as exc:  # noqa: BLE001 - one malformed event must not stop the forwarder.
            _FORWARDER_STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[WebChatStreamBus] forward failed: {}", exc)


async def start_web_chat_stream_forwarder(
    *,
    reconnect_delay_seconds: float = WEB_CHAT_STREAM_RECONNECT_SECONDS,
) -> None:
    _FORWARDER_STATE.update({"running": True, "last_error": None})
    try:
        while True:
            try:
                await _listen_web_chat_stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _FORWARDER_STATE["last_error"] = f"{type(exc).__name__}: {exc}"
                _FORWARDER_STATE["restart_count"] = int(_FORWARDER_STATE.get("restart_count") or 0) + 1
                _FORWARDER_STATE["last_restart_at"] = datetime.now(timezone.utc).isoformat()
                logger.warning("[WebChatStreamBus] forwarder reconnecting after error: {}", exc)
                await asyncio.sleep(max(0.0, reconnect_delay_seconds))
                continue
            _FORWARDER_STATE["last_error"] = "WebChatStreamForwarderEnded: pubsub listener returned"
            _FORWARDER_STATE["restart_count"] = int(_FORWARDER_STATE.get("restart_count") or 0) + 1
            _FORWARDER_STATE["last_restart_at"] = datetime.now(timezone.utc).isoformat()
            logger.warning("[WebChatStreamBus] forwarder returned; reconnecting")
            await asyncio.sleep(max(0.0, reconnect_delay_seconds))
    except asyncio.CancelledError:
        raise
    finally:
        _FORWARDER_STATE["running"] = False


def web_chat_stream_forwarder_snapshot() -> dict[str, Any]:
    return dict(_FORWARDER_STATE)
