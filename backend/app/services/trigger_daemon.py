"""Trigger Daemon — evaluates all agent triggers in a single background loop.

Replaces the separate heartbeat and scheduler services with a unified trigger
evaluation engine. Runs as an asyncio background task.

Every 15 seconds:
  1. Load all enabled triggers from DB
  2. Evaluate each trigger (cron/once/interval/poll/on_message/webhook)
  3. Group fired triggers by agent_id (30s dedup window)
  4. Invoke each agent once with all its fired triggers as context
"""

import asyncio
import hashlib
import ipaddress
import json as _json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.core.events import get_redis
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.trigger import AgentTrigger
from app.models.agent import Agent
from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
from app.services.plan_mode_core import build_plan_execution_instruction
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.runtime_task_service import create_runtime_task_record, update_runtime_task_record
from app.services.trigger_preflight import (
    collect_trigger_runtime_options,
    evaluate_trigger_preflight,
    load_context_from,
    select_trigger_model,
)

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 120  # seconds — same agent won't be invoked twice within this window
MAX_AGENT_CHAIN_DEPTH = 5  # A→B→A→B→A max depth before stopping
MIN_POLL_INTERVAL_MINUTES = 5  # align with tenant/agent min_poll_interval_floor defaults
MAX_FIRES_PER_HOUR = 6  # hard cap: ~10 min minimum interval between fires
_TRIGGER_FIRE_LEASE_TTL_SECONDS = 600
_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS = 6 * 60 * 60

# Track last invocation time per agent to enforce dedup window
_last_invoke: dict[uuid.UUID, datetime] = {}

# Track fire timestamps per agent for hourly rate limiting
_fire_history: dict[uuid.UUID, list[datetime]] = {}


async def _create_trigger_runtime_task(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    metadata_json: dict | None = None,
) -> str | None:
    """Create a best-effort RuntimeTask ledger row for a fired trigger batch."""
    trigger_names = [str(getattr(trigger, "name", "")) for trigger in triggers]
    metadata = {
        "source": "trigger_daemon",
        "agent_id": str(agent_id),
        "trigger_ids": [str(getattr(trigger, "id", "")) for trigger in triggers],
        "trigger_names": trigger_names,
        "trigger_types": [str(getattr(trigger, "type", "")) for trigger in triggers],
        "trigger_classes": [
            str((getattr(trigger, "config", None) or {}).get("trigger_class") or "")
            for trigger in triggers
            if (getattr(trigger, "config", None) or {}).get("trigger_class")
        ],
    }
    metadata.update(metadata_json or {})
    try:
        task_id = uuid.uuid4().hex
        trace_id = f"trigger:{task_id}"
        metadata.update(
            {
                "runtime_task_id": task_id,
                "request_id": str(uuid.UUID(task_id)),
                "trace_id": trace_id,
            }
        )
        return await create_runtime_task_record(
            task_id=task_id,
            task_type="trigger",
            status="running",
            parent_agent_id=agent_id,
            prompt=f"Trigger wake: {', '.join(name for name in trigger_names if name) or 'unknown'}",
            trace_id=trace_id,
            metadata_json=metadata,
        )
    except Exception as exc:
        logger.warning("[TriggerDaemon] Failed to create trigger RuntimeTask for {}: {}", agent_id, exc)
        return None


async def _update_trigger_runtime_task(
    runtime_task_id: str | None,
    *,
    status: str,
    result_summary: str,
    session_id: str | None = None,
    metadata_json: dict | None = None,
) -> None:
    if not runtime_task_id:
        return
    fields = {
        "status": status,
        "result_summary": result_summary[:2000],
        "metadata_json": metadata_json or {},
    }
    if session_id:
        fields["child_session_id"] = session_id
    try:
        await update_runtime_task_record(runtime_task_id, **fields)
    except Exception as exc:
        logger.warning("[TriggerDaemon] Failed to update trigger RuntimeTask {}: {}", runtime_task_id, exc)


async def _skip_trigger_runtime_task(
    runtime_task_id: str | None,
    *,
    skip_reason: str,
    result_summary: str,
    metadata_json: dict | None = None,
) -> None:
    metadata = {"skip_reason": skip_reason}
    metadata.update(metadata_json or {})
    await _update_trigger_runtime_task(
        runtime_task_id,
        status="skipped",
        result_summary=result_summary,
        metadata_json=metadata,
    )


# M-16: Persist dedup state to survive process restarts
# Use AGENT_DATA_DIR if available, otherwise a restricted temp path
def _get_dedup_path() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().AGENT_DATA_DIR) / ".trigger_dedup.json"
    except Exception:
        return Path("/tmp/.hive_trigger_dedup.json")


_DEDUP_FILE = _get_dedup_path()


def _load_dedup_state() -> None:
    global _last_invoke
    try:
        if _DEDUP_FILE.exists():
            data = _json.loads(_DEDUP_FILE.read_text())
            _last_invoke = {uuid.UUID(k): datetime.fromisoformat(v) for k, v in data.items()}
    except Exception as exc:
        logger.debug("[TriggerDaemon] Failed to load dedup state: {}", exc)


def _save_dedup_state() -> None:
    import os
    import tempfile

    try:
        data = {str(k): v.isoformat() for k, v in _last_invoke.items()}
        _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_DEDUP_FILE.parent), suffix=".tmp")
        fd_closed = False
        try:
            os.write(tmp_fd, _json.dumps(data).encode("utf-8"))
            os.close(tmp_fd)
            fd_closed = True
            os.replace(tmp_path, str(_DEDUP_FILE))
        except BaseException:
            if not fd_closed:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError as _unlink_err:
                logger.debug("[TriggerDaemon] Failed to clean up temp dedup file: {}", _unlink_err)
            raise
    except Exception as exc:
        logger.debug("[TriggerDaemon] Failed to save dedup state: {}", exc)


# Webhook rate limiter: token -> list of timestamps
_webhook_hits: dict[str, list[float]] = {}
WEBHOOK_RATE_LIMIT = 5  # max hits per minute per token


# ── Reply target recovery for pre-unified triggers ──────────────────


async def _recover_reply_target_from_session(
    agent_id: uuid.UUID,
    triggers: list,
) -> dict | None:
    """Try to recover a channel delivery target for triggers that have
    reply_context=NULL (created before the unified-delivery refactor).

    Strategy: find the agent's most recent non-web ChatSession that has
    a delivery_target_json with a "channel" key. This is a best-effort
    heuristic — it picks the last channel the user talked to this agent on.
    """
    from app.models.chat_session import ChatSession

    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel != "web",
                    ChatSession.source_channel != "agent",
                    ChatSession.delivery_target_json.isnot(None),
                )
                .order_by(ChatSession.last_message_at.desc().nullslast())
                .limit(1)
            )
            session = result.scalar_one_or_none()
            if not session or not session.delivery_target_json:
                return None

            target = dict(session.delivery_target_json)
            if not target.get("channel"):
                return None

            # Persist the recovered context back to the trigger so this
            # fallback only runs once per trigger.
            for trigger in triggers:
                if getattr(trigger, "reply_context", None) is None:
                    try:
                        trigger_r = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                        t_obj = trigger_r.scalar_one_or_none()
                        if t_obj:
                            t_obj.reply_context = target
                    except Exception as exc:
                        logger.debug("[TriggerDaemon] reply_context persist skipped: {}", exc)
            await db.commit()
            return target
    except Exception as exc:
        logger.debug("[TriggerDaemon] _recover_reply_target_from_session failed: {}", exc)
        return None


async def backfill_null_reply_contexts() -> dict:
    """One-time startup job: patch all enabled triggers that have reply_context=NULL.

    For each such trigger, look up the agent's most recent non-web ChatSession
    with a delivery_target_json and copy it into reply_context. This fixes
    triggers created before commit c0e00c8 (unified delivery refactor) where
    only Feishu triggers got reply_context — TG/WeChat were left NULL.

    Returns: {"patched": N, "skipped": M}
    """
    from app.models.chat_session import ChatSession

    patched = 0
    skipped = 0
    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="trigger reply_context backfill — enumerate all tenants' enabled triggers"),
        ):
            # All enabled triggers with NULL reply_context
            null_triggers = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.is_enabled.is_(True),
                    AgentTrigger.reply_context.is_(None),
                )
            )
            triggers = null_triggers.scalars().all()
            if not triggers:
                return {"patched": 0, "skipped": 0}

            # Group by agent for efficient session lookup
            agent_triggers: dict[uuid.UUID, list] = {}
            for t in triggers:
                agent_triggers.setdefault(t.agent_id, []).append(t)

            for aid, agent_trigs in agent_triggers.items():
                # Find the most recent non-web session with delivery_target
                session_r = await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == aid,
                        ChatSession.source_channel != "web",
                        ChatSession.source_channel != "agent",
                        ChatSession.delivery_target_json.isnot(None),
                    )
                    .order_by(ChatSession.last_message_at.desc().nullslast())
                    .limit(1)
                )
                session = session_r.scalar_one_or_none()
                if not session or not session.delivery_target_json or not session.delivery_target_json.get("channel"):
                    skipped += len(agent_trigs)
                    continue

                target = dict(session.delivery_target_json)
                for t in agent_trigs:
                    t.reply_context = target
                    patched += 1
                    logger.info(
                        "[TriggerDaemon] Backfilled reply_context for trigger '{}' (agent {}): channel={}",
                        t.name,
                        aid,
                        target.get("channel"),
                    )

            await db.commit()
    except Exception as exc:
        logger.warning("[TriggerDaemon] backfill_null_reply_contexts failed: {}", exc)

    return {"patched": patched, "skipped": skipped}


# ── SSRF Protection ─────────────────────────────────────────────────


def _is_private_url(url: str) -> bool:
    """Block private/internal URLs to prevent SSRF attacks."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True

        # Block obvious private hostnames
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True

        # Try to resolve hostname and check IP
        import socket

        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except (socket.gaierror, ValueError):
            return True  # Cannot resolve = block

        return False
    except Exception:
        return True  # Block on any parsing error


def _is_trigger_in_active_hours(active_hours: str, now: datetime, tz_name: str = "UTC") -> bool:
    """Return whether a configured trigger time window is currently active."""
    try:
        from zoneinfo import ZoneInfo

        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(str(tz_name or "UTC"))
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=timezone.utc).astimezone(tz)
        current_minutes = local_now.hour * 60 + local_now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True


# ── Trigger Evaluation ──────────────────────────────────────────────


def _inflight_fire_is_active(config: dict, now: datetime) -> bool:
    inflight = config.get("_fire_inflight")
    if not isinstance(inflight, dict):
        return False
    started_at = inflight.get("started_at")
    if not started_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - parsed) < timedelta(seconds=_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS)


async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    """Return True if this trigger should fire right now."""
    if not trigger.is_enabled:
        return False
    cfg = trigger.config or {}
    if _inflight_fire_is_active(cfg, now):
        return False
    backoff_until = cfg.get("backoff_until")
    if backoff_until:
        try:
            parsed_backoff = datetime.fromisoformat(str(backoff_until).replace("Z", "+00:00"))
            if parsed_backoff.tzinfo is None:
                parsed_backoff = parsed_backoff.replace(tzinfo=timezone.utc)
            if now < parsed_backoff:
                return False
        except ValueError:
            logger.debug("[TriggerDaemon] Invalid backoff_until for trigger {}: {}", trigger.name, backoff_until)
    if trigger.expires_at and now >= trigger.expires_at:
        # Auto-disable expired triggers
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    # Cooldown check
    if trigger.last_fired_at:
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    active_hours = str(cfg.get("active_hours") or "").strip()
    if active_hours:
        tz_name = str(cfg.get("timezone") or "").strip()
        if not tz_name:
            from app.services.timezone_utils import get_agent_timezone

            tz_name = await get_agent_timezone(trigger.agent_id)
        if not _is_trigger_in_active_hours(active_hours, now, tz_name):
            return False

    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr")
        if not expr:
            logger.warning(f"Cron trigger '{trigger.name}' has no expr in config — skipping")
            return False
        base = trigger.last_fired_at or trigger.created_at
        try:
            # Resolve timezone: trigger config → agent → tenant → UTC
            tz_name = cfg.get("timezone")
            if not tz_name:
                from app.services.timezone_utils import get_agent_timezone

                tz_name = await get_agent_timezone(trigger.agent_id)
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, Exception):
                tz = ZoneInfo("UTC")
            # Evaluate cron in agent's timezone
            local_now = now.astimezone(tz)
            local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
            cron = croniter(expr, local_base)
            next_run = cron.get_next(datetime)
            return local_now >= next_run
        except Exception as e:
            logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {e}")
            return False

    elif t == "once":
        at_str = cfg.get("at")
        if not at_str:
            return False
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return now >= at and trigger.fire_count == 0
        except Exception:
            return False

    elif t == "interval":
        minutes = cfg.get("minutes", 30)
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    elif t == "poll":
        interval_min = max(cfg.get("interval_min", 5), MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        # Actual HTTP poll + change detection
        return await _poll_check(trigger)

    elif t == "on_message":
        return await _check_new_agent_messages(trigger)

    elif t == "webhook":
        # Check if a webhook payload is pending
        if cfg.get("_webhook_pending"):
            return True
        return False

    return False


async def _poll_check(trigger: AgentTrigger) -> bool:
    """HTTP poll: fetch URL, extract value via json_path, detect change.

    Persists _last_value into the trigger's config JSONB so it survives
    across process restarts.
    """
    import httpx

    cfg = trigger.config or {}
    url = cfg.get("url")
    if not url:
        return False

    # SSRF protection: block private/internal URLs
    if _is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.name}: private/internal URL '{url}'")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(cfg.get("method", "GET"), url, headers=cfg.get("headers", {}))
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        current_value = _extract_json_path(data, json_path)
        current_str = str(current_value)

        fire_on = cfg.get("fire_on", "change")
        should_fire = False

        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
            if should_fire:
                cfg["_last_event"] = f"Polled {url} → value matched: {current_str[:500]}"
        else:  # "change"
            last_value = cfg.get("_last_value")
            # First poll — don't fire, just record baseline
            if last_value is None:
                should_fire = False
            else:
                should_fire = current_str != last_value
                if should_fire:
                    # Record what changed so the agent digests the actual event
                    # (event-driven v1: emit the event, not just "a trigger fired").
                    cfg["_last_event"] = (
                        f"Polled {url} → value changed from '{str(last_value)[:200]}' to '{current_str[:200]}'"
                    )

        # Persist _last_value to DB so it survives restarts
        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update

            tid = await resolve_tenant_for_agent(trigger.agent_id)
            async with tenant_scoped_session(tid) as db:
                await db.execute(update(AgentTrigger).where(AgentTrigger.id == trigger.id).values(config=cfg))
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.name}: {e}")

        return should_fire

    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return False


def _extract_json_path(data, path: str):
    """Simple JSONPath extraction: $.key.subkey → data['key']['subkey']."""
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def _check_new_agent_messages(trigger: AgentTrigger) -> bool:
    """Check if there are new messages matching this trigger.

    Supports two modes:
    - from_agent_name: check for agent-to-agent messages
    - from_user_name: check for human user messages (Feishu/Slack/Discord)

    Stores the actual message content in trigger.config['_matched_message']
    so the invocation context can include it.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config or {}
    from_agent_name = str(cfg.get("from_agent_name") or "").strip()
    from_agent_id = str(cfg.get("from_agent_id") or "").strip()
    from_user_name = str(cfg.get("from_user_name") or "").strip()
    from_user_identity = str(cfg.get("from_user_identity") or "").strip()
    from_channel = str(cfg.get("from_channel") or "").strip()
    reply_to_current_sender = bool(cfg.get("reply_to_current_sender"))

    if not any([from_agent_name, from_agent_id, from_user_name, from_user_identity, reply_to_current_sender]):
        return False

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        tid = await resolve_tenant_for_agent(trigger.agent_id)
        async with tenant_scoped_session(tid) as db:
            from sqlalchemy import cast as sa_cast, String as SaString, or_
            from app.models.agent import Agent as AgentModel
            from app.models.participant import Participant
            from app.models.user import User

            def _record_match(msg: ChatMessage, source_label: str) -> bool:
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = source_label
                msg_id = getattr(msg, "id", None)
                if msg_id:
                    cfg["_matched_event_key"] = f"chat_message:{msg_id}"
                return True

            def _session_sender_identity(session_obj: ChatSession) -> str:
                from app.services.channel_delivery_service import ChannelDeliveryService
                from app.services.pending_reply_service import sender_identity_from_external_conv_id

                sender_identity = sender_identity_from_external_conv_id(
                    getattr(session_obj, "external_conv_id", "") or ""
                )
                if sender_identity:
                    return sender_identity
                return ChannelDeliveryService.identity_from_delivery_target(
                    getattr(session_obj, "delivery_target_json", None)
                )

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == trigger.agent_id))
            current_agent = agent_r.scalar_one_or_none()
            current_tenant_id = getattr(current_agent, "tenant_id", None)

            if reply_to_current_sender:
                reply_ctx = getattr(trigger, "reply_context", None) or {}
                session_id = str(reply_ctx.get("session_id") or "")
                if not session_id:
                    return False
                result = await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == trigger.agent_id,
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(
                    msg, reply_ctx.get("user_label") or reply_ctx.get("sender_identity") or "current_sender"
                )

            if from_user_identity:
                result = await db.execute(
                    select(ChatMessage, ChatSession)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(20)
                )
                for msg, session_obj in result.all():
                    if from_channel and getattr(session_obj, "source_channel", "") != from_channel:
                        continue
                    if _session_sender_identity(session_obj) != from_user_identity:
                        continue
                    source_label = from_user_identity
                    delivery_target = getattr(session_obj, "delivery_target_json", None) or {}
                    source_label = delivery_target.get("user_label") or delivery_target.get("user_id") or source_label
                    return _record_match(msg, source_label)
                return False

            if from_agent_id or from_agent_name:
                source_agent = None
                if from_agent_id:
                    try:
                        source_agent_id = uuid.UUID(from_agent_id)
                    except ValueError:
                        return False
                    agent_r = await db.execute(
                        select(AgentModel).where(
                            AgentModel.id == source_agent_id,
                            AgentModel.tenant_id == current_tenant_id,
                        )
                    )
                    source_agent = agent_r.scalar_one_or_none()
                else:
                    agent_r = await db.execute(
                        select(AgentModel).where(
                            AgentModel.tenant_id == current_tenant_id,
                            AgentModel.name.ilike(from_agent_name),
                        )
                    )
                    matches = agent_r.scalars().all()
                    if len(matches) != 1:
                        if len(matches) > 1:
                            cfg["_match_error"] = f"Ambiguous from_agent_name: {from_agent_name}"
                        return False
                    source_agent = matches[0]

                if not source_agent:
                    return False

                result = await db.execute(
                    select(Participant.id).where(
                        Participant.type == "agent",
                        Participant.ref_id == source_agent.id,
                    )
                )
                from_participant = result.scalar_one_or_none()
                if not from_participant:
                    return False

                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        ChatMessage.role == "assistant",
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, getattr(source_agent, "name", from_agent_name or from_agent_id))

            if from_user_name:
                user_r = await db.execute(
                    select(User)
                    .where(
                        User.tenant_id == current_tenant_id,
                        or_(
                            User.display_name.ilike(f"%{from_user_name}%"),
                            User.username.ilike(f"%{from_user_name}%"),
                        ),
                    )
                    .limit(1)
                )
                target_user = user_r.scalars().first()
                if not target_user:
                    return False

                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatSession.user_id == target_user.id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, from_user_name)

    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return False

    return False


def _default_trigger_event_key(trigger: AgentTrigger, now: datetime, evaluation: dict | bool | None = None) -> str:
    cfg = trigger.config or {}
    if isinstance(evaluation, dict) and evaluation.get("event_key"):
        return str(evaluation["event_key"])
    if cfg.get("_matched_event_key"):
        return str(cfg["_matched_event_key"])
    if trigger.type == "once":
        return f"once:{trigger.id}:{cfg.get('at') or trigger.created_at.isoformat()}"
    if trigger.type == "cron":
        return f"cron:{trigger.id}:{now.strftime('%Y%m%d%H%M')}"
    if trigger.type == "interval":
        minutes = max(int(cfg.get("minutes", 30) or 30), 1)
        return f"interval:{trigger.id}:{int(now.timestamp()) // (minutes * 60)}"
    if trigger.type == "poll":
        current_value = str(cfg.get("_last_value") or "")
        return f"poll:{trigger.id}:{hashlib.sha256(current_value.encode('utf-8')).hexdigest()[:16]}"
    if trigger.type == "webhook":
        payload = str(cfg.get("_webhook_payload") or "")
        payload_hash = (
            hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] if payload else now.strftime("%Y%m%d%H%M%S")
        )
        return f"webhook:{trigger.id}:{cfg.get('_webhook_event_key') or payload_hash}"
    return f"{trigger.type}:{trigger.id}:{now.strftime('%Y%m%d%H%M%S')}"


async def _acquire_trigger_fire_lease(
    trigger_id: uuid.UUID,
    event_key: str,
    *,
    ttl_seconds: int = _TRIGGER_FIRE_LEASE_TTL_SECONDS,
) -> bool:
    lease_key = f"trigger_fire:{trigger_id}:{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:24]}"
    try:
        redis = await get_redis()
        acquired = await redis.set(lease_key, "1", ex=ttl_seconds, nx=True)
        return bool(acquired)
    except Exception as exc:
        logger.warning("[TriggerDaemon] Failed to acquire trigger fire lease for {}: {}", trigger_id, exc)
        return False


async def _preflight_trigger_group(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    now: datetime,
) -> tuple[bool, str | None, str, dict]:
    """Run P5 wake gate before mutating trigger fire counters."""
    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                return False, "agent_not_found", f"Agent {agent_id} was not found.", {}
            model, model_metadata, model_error = await select_trigger_model(db, agent, triggers)
            if model_error:
                return (
                    False,
                    model_error,
                    f"Trigger wake skipped by preflight: {model_error}.",
                    model_metadata,
                )
            preflight = await evaluate_trigger_preflight(db, agent=agent, model=model, triggers=triggers, now=now)
            metadata = {**model_metadata, **preflight.metadata}
            return preflight.ok, preflight.skip_reason, preflight.result_summary, metadata
    except Exception as exc:
        logger.warning("[TriggerDaemon] Trigger preflight failed for {}: {}", agent_id, exc)
        return False, "preflight_failed", f"Trigger preflight failed: {str(exc)[:500]}", {"error": str(exc)[:1000]}


async def _record_trigger_success_state(agent_id: uuid.UUID, trigger_ids: list[uuid.UUID]) -> None:
    if not trigger_ids:
        return
    from app.services.trigger_failure_policy import reset_trigger_failure_policy

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        changed = False
        for trigger_id in trigger_ids:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            cfg = dict(trigger.config or {})
            cfg.pop("_fire_inflight", None)
            if trigger.type == "webhook":
                cfg["_webhook_pending"] = False
                cfg["_webhook_payload"] = None
            trigger.config = cfg
            trigger.last_fired_at = datetime.now(timezone.utc)
            trigger.fire_count = int(trigger.fire_count or 0) + 1
            if trigger.type == "once":
                trigger.is_enabled = False
            if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
                trigger.is_enabled = False
            if reset_trigger_failure_policy(trigger):
                cfg = dict(trigger.config or {})
                cfg.pop("_fire_inflight", None)
                trigger.config = cfg
            changed = True
        if changed:
            await db.commit()


async def _mark_trigger_fire_started(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    now: datetime,
    runtime_task_id: str | uuid.UUID | None,
    event_keys: dict[uuid.UUID, str],
) -> None:
    if not triggers:
        return
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        changed = False
        for detached in triggers:
            trigger_id = getattr(detached, "id", None)
            if trigger_id is None:
                continue
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            cfg = dict(trigger.config or {})
            cfg["_fire_inflight"] = {
                "event_key": event_keys.get(trigger_id) or _default_trigger_event_key(trigger, now),
                "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
                "started_at": now.isoformat(),
            }
            trigger.config = cfg
            changed = True
        if changed:
            await db.commit()


async def _record_trigger_failure_state(agent_id: uuid.UUID, triggers: list[AgentTrigger], error: str) -> dict:
    from app.services.trigger_failure_policy import apply_trigger_failure_policy

    metadata: dict = {"failure_backoff": []}
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        for detached in triggers:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == getattr(detached, "id", None)))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            failure_meta = apply_trigger_failure_policy(trigger, error=error)
            cfg = dict(trigger.config or {})
            cfg.pop("_fire_inflight", None)
            trigger.config = cfg
            metadata["failure_backoff"].append(
                {
                    "trigger_id": str(trigger.id),
                    "trigger_name": trigger.name,
                    **failure_meta,
                }
            )
        await db.commit()
    return metadata


# ── Agent Invocation ────────────────────────────────────────────────


async def _load_confirmed_plan_for_trigger(db: Any, trigger: AgentTrigger, agent_id: uuid.UUID) -> Any | None:
    """E: load the confirmed plan a plan-born trigger fires for.

    The trigger carries ``config.plan_id`` (set at handoff — the load-bearing
    backstop contract). Fire-time reads the plan row fresh: single source of
    truth, no config bloat, and the plan body cannot go stale. Fails closed
    (returns ``None`` → wake by reason/focus exactly as before) when there is no
    ``plan_id``, the plan is missing, not ``confirmed``, or belongs to a
    different agent.
    """
    config = getattr(trigger, "config", None) or {}
    plan_id_raw = str(config.get("plan_id") or "").strip()
    if not plan_id_raw:
        return None
    try:
        plan_uuid = uuid.UUID(plan_id_raw)
    except (TypeError, ValueError):
        logger.warning(
            "[TriggerDaemon] trigger {} has invalid plan_id {!r} — waking without plan", trigger.name, plan_id_raw
        )
        return None

    from app.models.plan_request import AgentPlanRequest

    plan = (await db.execute(select(AgentPlanRequest).where(AgentPlanRequest.id == plan_uuid))).scalar_one_or_none()
    if plan is None:
        logger.warning(
            "[TriggerDaemon] trigger {} plan_id {} not found — waking without plan", trigger.name, plan_id_raw
        )
        return None
    if plan.status != "confirmed":
        logger.info(
            "[TriggerDaemon] trigger {} plan {} status={} (not confirmed) — waking without plan",
            trigger.name,
            plan_id_raw,
            plan.status,
        )
        return None
    if str(plan.agent_id) != str(agent_id):
        logger.warning(
            "[TriggerDaemon] trigger {} plan {} belongs to agent {} not {} — waking without plan",
            trigger.name,
            plan_id_raw,
            plan.agent_id,
            agent_id,
        )
        return None
    return plan


async def _build_confirmed_plan_context(db: Any, triggers: list[AgentTrigger], agent_id: uuid.UUID) -> str:
    """E: render the confirmed-plan marching orders for the plan-born triggers in
    this fire batch (deduped by plan id).

    Returns ``""`` when no fired trigger carries a confirmed plan — the ordinary
    reason/focus wake is then unchanged. The body is rendered by the shared
    :func:`build_plan_execution_instruction`, so a trigger wake executes the same
    confirmed plan the live chat would, with no wording drift.
    """
    seen: set[str] = set()
    blocks: list[str] = []
    for trigger in triggers:
        plan = await _load_confirmed_plan_for_trigger(db, trigger, agent_id)
        if plan is None:
            continue
        key = str(plan.id)
        if key in seen:
            continue
        seen.add(key)
        plan_json = plan.plan_json or {}
        blocks.append(
            build_plan_execution_instruction(
                plan_id=plan.id,
                plan_version=plan.plan_version,
                plan_markdown=str(plan_json.get("plan_markdown") or ""),
                objective=str(plan_json.get("objective") or ""),
                original_request=str(plan.original_request or ""),
                source="trigger",
            )
        )
    if not blocks:
        return ""
    return "\n\n===== 已确认的计划（本次唤醒按此执行）=====\n" + "\n\n".join(blocks)


def _format_trigger_event(trigger: AgentTrigger, cfg: dict) -> str:
    """The event payload for an event-driven trigger (poll/on_message/webhook).

    Exec/automation §2 — event-driven v1: the fired trigger must hand the agent
    the *actual event*, not just "a trigger fired". Returns "" for triggers with
    no captured event (e.g. a poll that fired with no recorded change)."""
    t = trigger.type
    if t == "on_message" and cfg.get("_matched_message"):
        return f'Message from {cfg.get("_matched_from", "?")}:\n"{cfg["_matched_message"][:500]}"'
    if t == "webhook" and cfg.get("_webhook_payload"):
        payload_str = str(cfg["_webhook_payload"])
        if len(payload_str) > 2000:
            payload_str = payload_str[:2000] + "... (truncated)"
        return f"Webhook payload:\n{payload_str}"
    if t == "poll":
        # The change description recorded by _poll_check (falls back to last value).
        event = cfg.get("_last_event") or (f"Polled value: {cfg.get('_last_value')}" if cfg.get("_last_value") else "")
        return str(event)
    return ""


def _build_trigger_context(
    triggers: list[AgentTrigger],
    *,
    explicit_context: str = "",
    confirmed_plan_context: str = "",
) -> tuple[str, list[str]]:
    """Build the wake context fed to the agent as one user message (pure/testable).

    Three-bucket framing (exec/automation §2): event-driven triggers
    (poll/on_message/webhook) are presented as an *event to react to* with the
    event payload inline; scheduled triggers (cron/once/interval) are a plain
    scheduled run. Returns ``(context, trigger_names)``.
    """
    from app.services.agent_tool_domains.triggers import TRIGGER_BUCKET_EVENT_DRIVEN, trigger_bucket

    context_parts: list[str] = []
    trigger_names: list[str] = []
    for t in triggers:
        cfg = t.config or {}
        if trigger_bucket(t.type) == TRIGGER_BUCKET_EVENT_DRIVEN:
            part = f"Event from trigger: {t.name} ({t.type})\nReason: {t.reason}"
            event = _format_trigger_event(t, cfg)
            if event:
                part += f"\n{event}"
        else:
            part = f"Scheduled trigger: {t.name} ({t.type})\nReason: {t.reason}"

        # Reply channel context if the trigger was created from a channel.
        reply_ctx = getattr(t, "reply_context", None) or {}
        if reply_ctx.get("channel"):
            ch = reply_ctx["channel"]
            user_label = reply_ctx.get("user_label", "the requesting user")
            part += (
                f"\nReply Channel: {ch}"
                f"\nReply To: {user_label}"
                "\n→ MUST use send_channel_message / send_channel_file for this reply target."
            )

        context_parts.append(part)
        trigger_names.append(t.name)

    multiple = len(triggers) > 1
    trigger_context = (
        "===== Trigger Awakening Context =====\n"
        f"Source: trigger ({'multiple triggers fired simultaneously' if multiple else 'single trigger fired'})\n\n"
        + "\n---\n".join(context_parts)
        + (f"\n\nExplicit Context From configured refs:\n{explicit_context}" if explicit_context else "")
        + confirmed_plan_context
        + "\n\nExecute this trigger now. If you finish or get blocked, record the outcome "
        "with concrete evidence in your work ledger and memory."
        "\n==========================="
    )
    return trigger_context, trigger_names


async def _invoke_agent_for_triggers(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    runtime_task_id: str | None = None,
):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.api.websocket import call_llm
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log

    # §9 P8 (§6.2): triggers carrying a workflow_ref take the deterministic
    # engine branch; the rest continue down the existing prose-ReAct path.
    from app.services.workflow_trigger import fire_workflow_for_trigger

    react_triggers: list[AgentTrigger] = []
    for trigger in triggers:
        try:
            fire_result = await fire_workflow_for_trigger(
                agent_id=agent_id,
                trigger_config=trigger.config or {},
                trigger_name=trigger.name,
                webhook_payload=(trigger.config or {}).get("_webhook_payload"),
            )
        except Exception as exc:
            logger.error("[TriggerDaemon] workflow_ref fire failed for {}: {}", trigger.name, exc)
            fire_result = None
        if fire_result is None:
            react_triggers.append(trigger)
        else:
            logger.info(
                "[TriggerDaemon] trigger {} → workflow branch: {} (run={})",
                trigger.name,
                fire_result.status,
                fire_result.run_id,
            )
    if not react_triggers:
        await _skip_trigger_runtime_task(
            runtime_task_id,
            skip_reason="workflow_ref_handled",
            result_summary="All fired triggers were handled by the workflow engine branch.",
        )
        return
    triggers = react_triggers

    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Load agent
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning("[TriggerDaemon] Agent {} not found — skipping trigger", agent_id)
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_found",
                    result_summary=f"Skipped trigger invocation because agent {agent_id} was not found.",
                )
                return
            if agent.status in ("expired", "stopped", "error", "archived"):
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_runnable",
                    result_summary=f"Skipped trigger invocation because agent status is {agent.status}.",
                    metadata_json={"agent_status": agent.status},
                )
                return

            # Set execution identity — autonomous agent action
            from app.core.execution_context import set_agent_bot_identity

            set_agent_bot_identity(agent_id, agent.name, source="trigger")

            # Load LLM model. P5 supports per-job model pinning via trigger.config.model_id.
            model, model_metadata, model_error = await select_trigger_model(db, agent, triggers)
            if model_error:
                logger.warning("[TriggerDaemon] Model preflight failed for {}: {}", agent.name, model_error)
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason=model_error,
                    result_summary=f"Skipped trigger invocation because model preflight failed: {model_error}.",
                    metadata_json=model_metadata,
                )
                return
            runtime_options = collect_trigger_runtime_options(triggers)

            explicit_context = ""
            if runtime_options.get("context_from"):
                explicit_context = await load_context_from(
                    db,
                    agent_id=agent_id,
                    context_refs=list(runtime_options.get("context_from") or []),
                )

            # E: a plan-born trigger fires for a user-confirmed plan — inject the
            # approved plan body as marching orders (read fresh from the plan row).
            confirmed_plan_context = await _build_confirmed_plan_context(db, triggers, agent_id)

            # Three-bucket framing + event payload (exec/automation §2) — pure helper.
            trigger_context, trigger_names = _build_trigger_context(
                triggers,
                explicit_context=explicit_context,
                confirmed_plan_context=confirmed_plan_context,
            )

            # Create a fresh Reflection Session for this wake.
            title = f"🤖 Reflection: {', '.join(trigger_names)}"
            # Find agent's participant
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = ChatSession(
                agent_id=agent_id,
                tenant_id=tid,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                session_kind="trigger_run",
                actor_type="agent",
                runtime_source="trigger",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            memory_messages = [{"role": "user", "content": trigger_context}]
            messages = list(memory_messages)

            # Store trigger context as a message in the session
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=tid,
                    conversation_id=str(session_id),
                    role="user",
                    content=trigger_context,
                    user_id=agent.creator_id,
                    participant_id=agent_participant.id if agent_participant else None,
                )
            )
            session.last_message_at = datetime.now(timezone.utc)
            await db.commit()
            # Cache participant ID + tenant for callbacks (they run after this
            # scoped session closes; stage-2b ChatMessage INSERTs must set
            # tenant_id and run under a tenant-scoped session).
            agent_participant_id = agent_participant.id if agent_participant else None
            agent_tenant_id = tid

        # Call LLM (outside the DB session to avoid long transactions)
        collected_content = []

        async def on_chunk(text):
            collected_content.append(text)

        # Persist tool calls into Reflection Session for Reflections visibility
        async def on_tool_call(data):
            try:
                async with tenant_scoped_session(agent_tenant_id) as _tc_db:
                    if data["status"] == "done":
                        result_str = str(data.get("result", ""))[:2000]
                        _tc_db.add(
                            ChatMessage(
                                agent_id=agent_id,
                                tenant_id=agent_tenant_id,
                                conversation_id=str(session_id),
                                role="tool_call",
                                content=_json.dumps(
                                    {
                                        "name": data["name"],
                                        "args": data.get("args"),
                                        "status": "done",
                                        "result": result_str,
                                        "reasoning_content": data.get("reasoning_content"),
                                        "reasoning_signature": data.get("reasoning_signature"),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                user_id=agent.creator_id,
                                participant_id=agent_participant_id,
                            )
                        )
                    await _tc_db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        from app.services.channel_delivery_service import channel_delivery_target

        reply_target = next(
            (
                getattr(trigger, "reply_context", None)
                for trigger in triggers
                if getattr(trigger, "reply_context", None) and getattr(trigger, "reply_context", None).get("channel")
            ),
            None,
        )

        # Fallback: if reply_context is NULL (pre-unified-delivery triggers),
        # try to recover from the agent's most recent non-web ChatSession.
        if reply_target is None:
            try:
                reply_target = await _recover_reply_target_from_session(agent_id, triggers)
                if reply_target:
                    logger.info(
                        "[TriggerDaemon] Recovered reply_target from session for agent {}: channel={}",
                        agent_id,
                        reply_target.get("channel"),
                    )
            except Exception as _recover_err:
                logger.debug("[TriggerDaemon] reply_target recovery failed: {}", _recover_err)

        _delivery_token = None
        if reply_target:
            _delivery_token = channel_delivery_target.set(reply_target)
        system_prompt_suffix_parts = []
        if runtime_options.get("execution_class"):
            system_prompt_suffix_parts.append(f"Trigger execution class: {runtime_options['execution_class']}.")
        if runtime_options.get("workdir"):
            system_prompt_suffix_parts.append(
                f"Use this job workdir for generated files when applicable: {runtime_options['workdir']}."
            )
        if runtime_options.get("allowed_tool_names"):
            system_prompt_suffix_parts.append(
                "This job declares an explicit toolset; stay within it. If a needed capability is missing, use "
                "`tool_search` to discover matching deferred schemas when that tool is available, otherwise report "
                "the missing capability instead of assuming a loaded skill can expand tools."
            )
        system_prompt_suffix = "\n".join(system_prompt_suffix_parts)
        try:
            from app.runtime.session import SessionContext

            reply = await call_llm(
                model=model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=agent_id,
                user_id=agent.creator_id,
                on_chunk=on_chunk,
                on_tool_call=on_tool_call,
                session_id=str(session_id),
                memory_messages=memory_messages,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=agent_id,
                    label=f"Agent: {agent.name} (trigger)",
                ),
                allowed_tool_names=runtime_options.get("allowed_tool_names") or (),
                excluded_tool_names=runtime_options.get("excluded_tool_names") or (),
                system_prompt_suffix=system_prompt_suffix,
                # P1-1: trigger runs must NOT masquerade as live web chat. The
                # source drives the unattended Plan Mode lane and the T0 bucket
                # (trigger-*.md, not chat-*.md) — defaulting to "web" mis-routed
                # both and polluted T2 source weights.
                session_context=SessionContext(
                    session_id=str(session_id),
                    source="trigger",
                    channel="trigger",
                    metadata={
                        "tenant_id": str(agent_tenant_id) if agent_tenant_id else None,
                        "runtime_task_id": runtime_task_id,
                        "request_id": str(uuid.UUID(runtime_task_id)) if runtime_task_id else None,
                        "trace_id": f"trigger:{runtime_task_id}" if runtime_task_id else None,
                    },
                ),
                session_source="trigger",
                session_channel="trigger",
            )
        finally:
            if _delivery_token is not None:
                channel_delivery_target.reset(_delivery_token)

        # Save assistant reply to Reflection session
        async with tenant_scoped_session(agent_tenant_id) as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=agent_tenant_id,
                    conversation_id=str(session_id),
                    role="assistant",
                    content=reply or "".join(collected_content),
                    user_id=agent.creator_id,
                    participant_id=agent_participant.id if agent_participant else None,
                )
            )

            # NOTE: trigger state (last_fired_at, fire_count, auto-disable)
            # is already updated in _tick() BEFORE this task was launched,
            # to prevent race-condition duplicate fires.

            await db.commit()

        # Trigger results live in the Reflection Session only.
        # Do NOT push to user's chat WebSocket — it pollutes the conversation.
        # Users can view trigger results in the self-awareness tab.

        # Outcome metadata only. Durable learning enters the canonical T0 -> T2
        # path through the TRIGGER_END hook below.
        final_reply = reply or "".join(collected_content)
        trigger_outcome = "unknown"
        trigger_score = None
        try:
            from app.services.heartbeat import _parse_heartbeat_outcome

            trigger_outcome, trigger_score = _parse_heartbeat_outcome(final_reply)
            logger.debug(
                "[TriggerDaemon] Trigger outcome parsed for {}: {} score={}",
                agent_id,
                trigger_outcome,
                trigger_score,
            )
        except Exception as _outcome_err:
            logger.debug("[TriggerDaemon] Trigger outcome parse failed (non-fatal): {}", _outcome_err)

        # Count trigger execution as a session for auto-dream gate
        try:
            from app.services.auto_dream import record_session_end, should_dream, run_dream

            record_session_end(agent_id)
            if should_dream(agent_id) and agent.tenant_id:
                asyncio.create_task(run_dream(agent_id, agent.tenant_id))
                logger.info("[TriggerDaemon] Auto-dream triggered for agent {}", agent_id)
        except Exception as _dream_err:
            logger.debug("[TriggerDaemon] Auto-dream check failed: {}", _dream_err)

        # Audit log
        await write_audit_log(
            "trigger_fired",
            {
                "agent_name": agent.name,
                "triggers": [{"name": t.name, "type": t.type} for t in triggers],
            },
            agent_id=agent_id,
        )

        output_artifact = None
        try:
            from app.config import get_settings
            from app.services.trigger_artifacts import write_trigger_output_artifact

            output_artifact = write_trigger_output_artifact(
                agent_data_dir=get_settings().AGENT_DATA_DIR,
                agent_id=agent_id,
                runtime_task_id=runtime_task_id,
                triggers=triggers,
                final_reply=final_reply or "",
                metadata={
                    **model_metadata,
                    **runtime_options,
                    "outcome": trigger_outcome,
                    "score": trigger_score,
                    "session_id": str(session_id),
                },
            )
        except Exception as _artifact_err:
            logger.debug("[TriggerDaemon] Trigger output artifact failed (non-fatal): {}", _artifact_err)

        try:
            await _record_trigger_success_state(agent_id, [getattr(trigger, "id") for trigger in triggers])
        except Exception as _success_state_err:
            logger.debug("[TriggerDaemon] Trigger success state reset failed (non-fatal): {}", _success_state_err)

        await _update_trigger_runtime_task(
            runtime_task_id,
            status="completed",
            result_summary=(final_reply or "Trigger completed.")[:2000],
            session_id=str(session_id),
            metadata_json={
                **model_metadata,
                **runtime_options,
                "outcome": trigger_outcome,
                "score": trigger_score,
                "output_artifact": output_artifact,
            },
        )

        logger.info(f"⚡ Triggers fired for {agent.name}: {[t.name for t in triggers]}")

        # Emit TRIGGER_END hook → T0 session ledger + extraction pipeline
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.TRIGGER_END,
                agent_id=agent_id,
                session_id=str(session_id),
                messages=messages,
                source="trigger",
                metadata={
                    "trigger_name": trigger_names[0] if trigger_names else "unknown",
                    "trigger_type": triggers[0].type if triggers else "unknown",
                    "trigger_names": trigger_names,
                    "trigger_types": [t.type for t in triggers],
                    "status": "success",
                    "instruction": trigger_context[:1000] if trigger_context else "",
                    "result": final_reply[:2000] if final_reply else "",
                },
            )
        except Exception as _hook_err:
            logger.debug("[TriggerDaemon] TRIGGER_END hook failed (non-fatal): {}", _hook_err)

    except Exception as e:
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}", exc_info=True)
        failure_metadata = {"error": str(e)[:1000]}
        try:
            failure_metadata.update(await _record_trigger_failure_state(agent_id, triggers, str(e)))
        except Exception as _failure_state_err:
            logger.debug("[TriggerDaemon] Trigger failure state update failed (non-fatal): {}", _failure_state_err)
        await _update_trigger_runtime_task(
            runtime_task_id,
            status="failed",
            result_summary=f"Trigger invocation failed: {str(e)[:500]}",
            metadata_json=failure_metadata,
        )


# ── Main Tick Loop ──────────────────────────────────────────────────


async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    now = datetime.now(timezone.utc)

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="trigger daemon tick — enumerate all enabled triggers across tenants"),
    ):
        result = await db.execute(
            select(AgentTrigger)
            .join(Agent, Agent.id == AgentTrigger.agent_id)
            .where(AgentTrigger.is_enabled, agent_lifecycle_active_clause())
        )
        all_triggers = result.scalars().all()

    if not all_triggers:
        logger.debug("[TriggerDaemon] No enabled triggers — tick skipped")
        return

    # Evaluate and group fired triggers by agent. A schedule is just a trigger,
    # so all of an agent's fired triggers collapse into one wake invocation.
    fired_by_group: dict[uuid.UUID, list[AgentTrigger]] = {}
    fire_event_keys: dict[uuid.UUID, str] = {}
    for trigger in all_triggers:
        # Auto-disable expired triggers — single-agent write, scope to its tenant.
        if trigger.expires_at and now >= trigger.expires_at:
            tid = await resolve_tenant_for_agent(trigger.agent_id)
            async with tenant_scoped_session(tid) as db:
                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                t = result.scalar_one_or_none()
                if t:
                    t.is_enabled = False
                    await db.commit()
            continue

        try:
            evaluation = await _evaluate_trigger(trigger, now)
            if not evaluation:
                continue
            event_key = _default_trigger_event_key(trigger, now, evaluation)
            if not await _acquire_trigger_fire_lease(trigger.id, event_key):
                logger.debug("[TriggerDaemon] Duplicate fire lease rejected for {} ({})", trigger.name, event_key)
                continue
            fire_event_keys[trigger.id] = event_key
            fired_by_group.setdefault(trigger.agent_id, []).append(trigger)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Invoke each agent for the trigger events that acquired a fire lease.
    # Per-agent try/except so one agent's failure doesn't block others (C-08)
    for agent_id, agent_triggers in fired_by_group.items():
        try:
            preflight_ok, skip_reason, skip_summary, preflight_metadata = await _preflight_trigger_group(
                agent_id,
                agent_triggers,
                now,
            )
            runtime_task_id = await _create_trigger_runtime_task(
                agent_id,
                agent_triggers,
                metadata_json=preflight_metadata,
            )
            if not preflight_ok:
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason=skip_reason or "preflight_blocked",
                    result_summary=skip_summary or "Trigger wake skipped by preflight.",
                    metadata_json=preflight_metadata,
                )
                continue

            try:
                await _mark_trigger_fire_started(
                    agent_id,
                    agent_triggers,
                    now=now,
                    runtime_task_id=runtime_task_id,
                    event_keys=fire_event_keys,
                )
            except Exception as e:
                logger.warning(f"Failed to mark trigger fire in-flight: {e}")
                await _update_trigger_runtime_task(
                    runtime_task_id,
                    status="failed",
                    result_summary=f"Trigger fire could not be marked in-flight: {str(e)[:500]}",
                    metadata_json={"error": str(e)[:1000], "stage": "mark_inflight"},
                )
                continue

            asyncio.create_task(
                _invoke_agent_for_triggers(
                    agent_id,
                    agent_triggers,
                    runtime_task_id=runtime_task_id,
                )
            )
        except Exception as _agent_err:
            logger.warning("[TriggerDaemon] Failed to process agent {}: {}", agent_id, _agent_err)


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup.

    P1-W2-4: heartbeat + workspace sync moved to `evolution_daemon` so a
    slow heartbeat tick or workspace volume I/O can't push trigger schedule
    jitter past TICK_INTERVAL. This loop is now single-purpose.
    """
    from app.services.daemon_liveness import mark_daemon_error, mark_daemon_started, mark_daemon_tick

    mark_daemon_started("trigger_daemon")
    logger.info(f"⚡ Trigger Daemon started ({TICK_INTERVAL}s tick)")

    while True:
        try:
            await _tick()
            mark_daemon_tick("trigger_daemon")
        except Exception as e:
            mark_daemon_error("trigger_daemon", e)
            logger.error(f"Trigger Daemon error: {e}")
            import traceback

            traceback.print_exc()

        await asyncio.sleep(TICK_INTERVAL)
