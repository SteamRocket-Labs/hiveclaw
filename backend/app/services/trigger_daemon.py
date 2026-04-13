"""Trigger Daemon — evaluates all agent triggers in a single background loop.

Replaces the separate heartbeat, scheduler, and supervision reminder services
with a unified trigger evaluation engine. Runs as an asyncio background task.

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
from urllib.parse import urlparse

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.core.events import get_redis
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.models.agent import Agent
from app.services.session_service import create_chat_session, session_conversation_id

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 120  # seconds — same agent won't be invoked twice within this window
MAX_AGENT_CHAIN_DEPTH = 5  # A→B→A→B→A max depth before stopping
MIN_POLL_INTERVAL_MINUTES = 30  # minimum poll interval to prevent token waste
MAX_FIRES_PER_HOUR = 6   # hard cap: ~10 min minimum interval between fires
_TRIGGER_FIRE_LEASE_TTL_SECONDS = 600

# Track last invocation time per agent to enforce dedup window
_last_invoke: dict[uuid.UUID, datetime] = {}

# Track fire timestamps per agent for hourly rate limiting
_fire_history: dict[uuid.UUID, list[datetime]] = {}

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
        logger.debug("[TriggerDaemon] Failed to load dedup state: %s", exc)


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
                logger.debug("[TriggerDaemon] Failed to clean up temp dedup file: %s", _unlink_err)
            raise
    except Exception as exc:
        logger.debug("[TriggerDaemon] Failed to save dedup state: %s", exc)

# Webhook rate limiter: token -> list of timestamps
_webhook_hits: dict[str, list[float]] = {}
WEBHOOK_RATE_LIMIT = 5   # max hits per minute per token


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


# ── Trigger Evaluation ──────────────────────────────────────────────

async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool | dict:
    """Return True if this trigger should fire right now."""
    from app.services.schedule_surface import schedule_manual_evaluation

    manual_evaluation = schedule_manual_evaluation(trigger)
    if manual_evaluation:
        return manual_evaluation
    if not trigger.is_enabled:
        return False
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

    cfg = trigger.config or {}
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
        else:  # "change"
            last_value = cfg.get("_last_value")
            # First poll — don't fire, just record baseline
            if last_value is None:
                should_fire = False
            else:
                should_fire = current_str != last_value

        # Persist _last_value to DB so it survives restarts
        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(AgentTrigger)
                    .where(AgentTrigger.id == trigger.id)
                    .values(config=cfg)
                )
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
        async with async_session() as db:
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

                sender_identity = sender_identity_from_external_conv_id(getattr(session_obj, "external_conv_id", "") or "")
                if sender_identity:
                    return sender_identity
                return ChannelDeliveryService.identity_from_delivery_target(getattr(session_obj, "delivery_target_json", None))

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == trigger.agent_id))
            current_agent = agent_r.scalar_one_or_none()
            current_tenant_id = getattr(current_agent, "tenant_id", None)

            if reply_to_current_sender:
                reply_ctx = getattr(trigger, "reply_context", None) or {}
                session_id = str(reply_ctx.get("session_id") or "")
                if not session_id:
                    return False
                result = await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.agent_id == trigger.agent_id,
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    ).order_by(ChatMessage.created_at.desc()).limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, reply_ctx.get("user_label") or reply_ctx.get("sender_identity") or "current_sender")

            if from_user_identity:
                result = await db.execute(
                    select(ChatMessage, ChatSession).join(
                        ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                    ).where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    ).order_by(ChatMessage.created_at.desc()).limit(20)
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
                    select(ChatMessage).join(
                        ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                    ).where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        ChatMessage.role == "assistant",
                    ).order_by(ChatMessage.created_at.desc()).limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, getattr(source_agent, "name", from_agent_name or from_agent_id))

            if from_user_name:
                user_r = await db.execute(
                    select(User).where(
                        User.tenant_id == current_tenant_id,
                        or_(
                            User.display_name.ilike(f"%{from_user_name}%"),
                            User.username.ilike(f"%{from_user_name}%"),
                        )
                    ).limit(1)
                )
                target_user = user_r.scalars().first()
                if not target_user:
                    return False

                result = await db.execute(
                    select(ChatMessage).join(
                        ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                    ).where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatSession.user_id == target_user.id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    ).order_by(ChatMessage.created_at.desc()).limit(1)
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
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] if payload else now.strftime("%Y%m%d%H%M%S")
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
        logger.warning("[TriggerDaemon] Failed to acquire trigger fire lease for %s: %s", trigger_id, exc)
        return True


# ── Agent Invocation ────────────────────────────────────────────────

async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.api.websocket import call_llm
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.llm import LLMModel
    from app.models.audit import ChatMessage
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log
    from app.services.activity_logger import log_activity
    from app.services.schedule_surface import build_schedule_activity_entries

    try:
        async with async_session() as db:
            # Load agent
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning("[TriggerDaemon] Agent %s not found — skipping trigger", agent_id)
                return
            if agent.status in ("expired", "stopped", "error", "archived"):
                return

            # Set execution identity — autonomous agent action
            from app.core.execution_context import set_agent_bot_identity
            set_agent_bot_identity(agent_id, agent.name, source="trigger")

            # Load LLM model
            if not agent.primary_model_id:
                logger.warning(f"Agent {agent.name} has no LLM model, skipping trigger invocation")
                return
            result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = result.scalar_one_or_none()
            if not model:
                return

            # Build trigger context
            context_parts = []
            trigger_names = []
            for t in triggers:
                part = f"Trigger: {t.name} ({t.type})\nReason: {t.reason}"
                if t.focus_ref:
                    part += f"\nRelated Focus: {t.focus_ref}"
                # Include matched message for on_message triggers
                cfg = t.config or {}
                if t.type == "on_message" and cfg.get("_matched_message"):
                    part += f"\nMessage from {cfg.get('_matched_from', '?')}:\n\"{cfg['_matched_message'][:500]}\""
                # Include webhook payload
                if t.type == "webhook" and cfg.get("_webhook_payload"):
                    payload_str = cfg["_webhook_payload"]
                    if len(payload_str) > 2000:
                        payload_str = payload_str[:2000] + "... (truncated)"
                    part += f"\nWebhook Payload:\n{payload_str}"
                # Inject reply channel context if the trigger was created from a channel
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

            # G3: Inject focus.md so agent can track progress during trigger execution
            focus_context = ""
            try:
                from app.config import get_settings as _get_settings
                _settings = _get_settings()
                for _base in [
                    Path(_settings.AGENT_DATA_DIR) / str(agent_id),
                    Path("/tmp/hive_workspaces") / str(agent_id),
                ]:
                    _focus_path = _base / "focus.md"
                    if _focus_path.exists():
                        _focus_text = _focus_path.read_text(encoding="utf-8")[:1500]
                        if _focus_text.strip() and _focus_text.strip() not in ("# Focus", "# Agenda"):
                            focus_context = f"\n\nCurrent Focus (your work priorities):\n{_focus_text}"
                        break
            except Exception as _focus_err:
                logger.debug("[TriggerDaemon] Failed to read focus.md for trigger context: %s", _focus_err)

            trigger_context = (
                "===== Trigger Awakening Context =====\n"
                f"Source: trigger ({'multiple triggers fired simultaneously' if len(triggers) > 1 else 'single trigger fired'})\n\n"
                + "\n---\n".join(context_parts)
                + focus_context
                + "\n\nIf you completed any focus.md task during this execution, use write_file to update focus.md and mark it [x]."
                "\n==========================="
            )

            # Create Reflection Session
            title = f"🤖 Reflection: {', '.join(trigger_names)}"
            # Find agent's participant
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = await create_chat_session(
                db,
                agent_id=agent_id,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                title=title[:200],
            )
            session_id = session.id

            memory_messages = [{"role": "user", "content": trigger_context}]
            messages = list(memory_messages)

            # Store trigger context as a message in the session
            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=session_conversation_id(session),
                role="user",
                content=trigger_context,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            ))
            await db.commit()
            # Cache participant ID for callbacks
            agent_participant_id = agent_participant.id if agent_participant else None

        # Call LLM (outside the DB session to avoid long transactions)
        collected_content = []

        async def on_chunk(text):
            collected_content.append(text)

        # Persist tool calls into Reflection Session for Reflections visibility
        async def on_tool_call(data):
            try:
                async with async_session() as _tc_db:
                    if data["status"] == "done":
                        result_str = str(data.get("result", ""))[:2000]
                        _tc_db.add(ChatMessage(
                            agent_id=agent_id,
                            conversation_id=str(session_id),
                            role="tool_call",
                            content=_json.dumps({
                                "name": data["name"],
                                "args": data.get("args"),
                                "status": "done",
                                "result": result_str,
                                "reasoning_content": data.get("reasoning_content"),
                            }, ensure_ascii=False, default=str),
                            user_id=agent.creator_id,
                            participant_id=agent_participant_id,
                        ))
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
        _delivery_token = None
        if reply_target:
            _delivery_token = channel_delivery_target.set(reply_target)
        try:
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
            )
        finally:
            if _delivery_token is not None:
                channel_delivery_target.reset(_delivery_token)

        # Save assistant reply to Reflection session
        async with async_session() as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="assistant",
                content=reply or "".join(collected_content),
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            ))

            # NOTE: trigger state (last_fired_at, fire_count, auto-disable)
            # is already updated in _tick() BEFORE this task was launched,
            # to prevent race-condition duplicate fires.

            await db.commit()

        # Trigger results live in the Reflection Session only.
        # Do NOT push to user's chat WebSocket — it pollutes the conversation.
        # Users can view trigger results in the self-awareness tab.

        # Evolution feedback — close the learning loop for trigger executions (BP-1 fix)
        final_reply = reply or "".join(collected_content)
        for entry in build_schedule_activity_entries(triggers, final_reply):
            await log_activity(
                agent_id,
                entry["action_type"],
                entry["summary"],
                detail=entry["detail"],
            )
        try:
            from app.services.heartbeat import (
                _parse_heartbeat_outcome,
                _update_evolution_files,
            )
            trigger_outcome, trigger_score = _parse_heartbeat_outcome(final_reply)
            trigger_summary = final_reply[:80] if final_reply else "empty"

            # Write to evolution files only — heartbeat/dream promote durable outcomes
            # into canonical markdown memory during the md-first consolidation cycle.
            await asyncio.to_thread(
                _update_evolution_files,
                agent_id,
                trigger_outcome,
                trigger_score,
                f"[trigger] {trigger_summary}",
                source="trigger",
            )
            logger.debug(
                "[TriggerDaemon] Evolution feedback for %s: %s score=%s",
                agent_id, trigger_outcome, trigger_score,
            )
        except Exception as _evo_err:
            logger.debug("[TriggerDaemon] Evolution feedback failed (non-fatal): %s", _evo_err)

        # Count trigger execution as a session for auto-dream gate
        try:
            from app.services.auto_dream import record_session_end, should_dream, run_dream
            record_session_end(agent_id)
            if should_dream(agent_id) and agent.tenant_id:
                asyncio.create_task(run_dream(agent_id, agent.tenant_id))
                logger.info("[TriggerDaemon] Auto-dream triggered for agent %s", agent_id)
        except Exception as _dream_err:
            logger.debug("[TriggerDaemon] Auto-dream check failed: %s", _dream_err)

        # Audit log
        await write_audit_log("trigger_fired", {
            "agent_name": agent.name,
            "triggers": [{"name": t.name, "type": t.type} for t in triggers],
        }, agent_id=agent_id)

        logger.info(f"⚡ Triggers fired for {agent.name}: {[t.name for t in triggers]}")

        # Emit TRIGGER_END hook → T0 log + extraction pipeline
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
            logger.debug("[TriggerDaemon] TRIGGER_END hook failed (non-fatal): %s", _hook_err)

    except Exception as e:
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}", exc_info=True)


# ── Main Tick Loop ──────────────────────────────────────────────────

async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.is_enabled)
        )
        all_triggers = result.scalars().all()

    if not all_triggers:
        logger.debug("[TriggerDaemon] No enabled triggers — tick skipped")
        return


    # Evaluate and group fired triggers by agent
    fired_by_agent: dict[uuid.UUID, list[AgentTrigger]] = {}
    for trigger in all_triggers:
        # Auto-disable expired triggers
        if trigger.expires_at and now >= trigger.expires_at:
            async with async_session() as db:
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
                logger.debug("[TriggerDaemon] Duplicate fire lease rejected for %s (%s)", trigger.name, event_key)
                continue
            fired_by_agent.setdefault(trigger.agent_id, []).append(trigger)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Invoke each agent for the trigger events that acquired a fire lease.
    # Per-agent try/except so one agent's failure doesn't block others (C-08)
    for agent_id, agent_triggers in fired_by_agent.items():
        try:
            # ── Immediately update trigger state BEFORE launching async task ──
            try:
                async with async_session() as db:
                    for t in agent_triggers:
                        result = await db.execute(
                            select(AgentTrigger).where(AgentTrigger.id == t.id)
                        )
                        trigger = result.scalar_one_or_none()
                        if trigger:
                            from app.services.schedule_surface import clear_schedule_manual_pending, is_schedule_surface_trigger

                            trigger.last_fired_at = now
                            trigger.fire_count += 1
                            if trigger.type == "once":
                                trigger.is_enabled = False
                            if is_schedule_surface_trigger(trigger):
                                trigger.config = clear_schedule_manual_pending(trigger.config)
                            if trigger.type == "webhook" and trigger.config:
                                trigger.config = {
                                    **trigger.config,
                                    "_webhook_pending": False,
                                    "_webhook_payload": None,
                                }
                            if trigger.max_fires and trigger.fire_count >= trigger.max_fires:
                                trigger.is_enabled = False
                    # IMPORTANT: commit() MUST complete before create_task() below
                    # to ensure trigger state (last_fired_at, fire_count) is persisted
                    # before the async invocation reads it — prevents duplicate fires.
                    await db.commit()
            except Exception as e:
                logger.warning(f"Failed to pre-update trigger state: {e}")

            asyncio.create_task(_invoke_agent_for_triggers(agent_id, agent_triggers))
        except Exception as _agent_err:
            logger.warning("[TriggerDaemon] Failed to process agent %s: %s", agent_id, _agent_err)


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    logger.info("⚡ Trigger Daemon started (15s tick, heartbeat every ~60s)")
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error(f"Trigger Daemon error: {e}")
            import traceback
            traceback.print_exc()

        # Run heartbeat check every 4th tick (~60 seconds)
        _heartbeat_counter += 1
        if _heartbeat_counter >= 4:
            _heartbeat_counter = 0
            try:
                from app.services.heartbeat import _heartbeat_tick
                await _heartbeat_tick()
            except Exception as e:
                logger.error(f"Heartbeat tick error: {e}")

            # Cleanup expired pending reply contexts (piggyback on heartbeat cadence)
            try:
                from app.services.pending_reply_service import cleanup_expired_replies

                async with async_session() as _pr_db:
                    await cleanup_expired_replies(_pr_db)
                    await _pr_db.commit()
            except Exception as e:
                logger.debug(f"PendingReply cleanup error (non-fatal): {e}")

        await asyncio.sleep(TICK_INTERVAL)
