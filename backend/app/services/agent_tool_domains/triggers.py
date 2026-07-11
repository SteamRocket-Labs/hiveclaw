"""Trigger management domain — CRUD for agent triggers (Aware Engine)."""

import logging
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.services.plan_mode_core import (
    stamp_confirmed_plan_provenance,
    stamp_user_declined_plan_exemption,
)
from app.runtime.schedule_decision_ledger import build_schedule_decision_entry, confirmed_plan_ref_from_args
from app.services.plan_mode_runtime_context import (
    trusted_plan_mode_user_decline_metadata,
    trusted_plan_mode_user_declined,
)
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

MAX_TRIGGERS_PER_AGENT = 20
#: B2 self-pace clamp (CC dynamic /loop alignment): the model picks its own
#: next wakeup delay inside these bounds.
SELF_PACE_WAKEUP_MIN_SECONDS = 60
SELF_PACE_WAKEUP_MAX_SECONDS = 3600
VALID_TRIGGER_TYPES = {"cron", "once", "interval", "poll", "on_message", "webhook"}
VALID_TRIGGER_CLASSES = {"scheduled_job", "event_wait", "system_maintenance"}
#: B3 — how a fired trigger reaches the agent. ``new_invocation`` (default)
#: keeps the historical behaviour (each fire starts a fresh trigger_run child
#: session); ``same_session`` routes the fire into an existing chat session as a
#: new turn (CC first-gen ``/loop`` cron "塞进当前 session" semantics).
VALID_DELIVERY_MODES = {"new_invocation", "same_session"}
EVENT_WAIT_TRIGGER_TYPES = {"poll", "on_message", "webhook"}
SCHEDULED_TRIGGER_TYPES = {"cron", "once", "interval"}


def _plan_authorization_stamp_kwargs(arguments: dict) -> dict:
    evidence = arguments.get("_plan_authorization")
    if not isinstance(evidence, dict):
        return {}
    return {
        "authorization_lease_id": evidence.get("lease_id"),
        "canonical_args_hash": evidence.get("canonical_args_hash"),
        "target_ref": evidence.get("target_ref"),
        "requester_user_id": evidence.get("requester_user_id"),
        "session_id": evidence.get("session_id"),
        "runtime_task_id": evidence.get("runtime_task_id"),
        "evidence_id": evidence.get("evidence_id"),
    }

#: Exec/automation CC-alignment (docs/trigger-cc-alignment.md §2): the three
#: driving-semantic buckets every wire-level ``type`` collapses into. The 6 type
#: strings are just the schedule/detection mechanisms; these three are the
#: semantics that decide *how* a fired trigger reaches the agent:
#:   - ``cron``: unconditional recurring time-driven run (cron, interval) → fires a complete call
#:   - ``once``: one-shot delayed background task → fires a complete call once
#:   - ``event_driven``: external condition/event source (poll, on_message, webhook) →
#:     lightweight detection emits an event the agent digests in its own loop
TRIGGER_BUCKET_CRON = "cron"
TRIGGER_BUCKET_ONCE = "once"
TRIGGER_BUCKET_EVENT_DRIVEN = "event_driven"


def trigger_bucket(trigger_type: str) -> str:
    """Classify a wire-level trigger ``type`` into its driving-semantic bucket (§2)."""
    if trigger_type in EVENT_WAIT_TRIGGER_TYPES:
        return TRIGGER_BUCKET_EVENT_DRIVEN
    if trigger_type == "once":
        return TRIGGER_BUCKET_ONCE
    return TRIGGER_BUCKET_CRON


def _trigger_next_fire_hint(trigger_type: str, config: dict) -> str | None:
    if trigger_type == "once":
        return str(config.get("at") or "") or None
    return None


def _set_trigger_schedule_decision_entry(arguments: dict, *, trigger_id: object | None, trigger_type: str) -> dict:
    config = arguments.get("config") if isinstance(arguments.get("config"), dict) else {}
    return build_schedule_decision_entry(
        command_origin="set_trigger",
        natural_vs_structured="structured",
        plan_gate_decision={"allowed": True, "reason": "tool_confirmed_or_exempt"},
        confirmed_plan_ref=confirmed_plan_ref_from_args(arguments),
        trigger_id=str(trigger_id or "") or None,
        next_fire=_trigger_next_fire_hint(trigger_type, config),
    )


def _capture_reply_context() -> dict | None:
    """Capture current channel context for trigger reply delivery.

    When an agent creates a trigger during a Feishu (or other channel) conversation,
    this captures who requested it and which channel, so the trigger daemon can
    inject delivery instructions when the trigger fires.
    """
    from app.services.channel_delivery_service import ChannelDeliveryService, channel_delivery_target

    ctx = ChannelDeliveryService.normalize_reply_target(channel_delivery_target.get(None)) or {}
    if not ctx:
        return None
    try:
        from app.core.execution_context import get_execution_identity

        identity = get_execution_identity()
        if identity and identity.label and not ctx.get("user_label"):
            ctx["user_label"] = identity.label
    except Exception as exc:
        logger.debug("Failed to resolve reply context label: %s", exc)

    sender_identity = ChannelDeliveryService.identity_from_delivery_target(ctx)
    if sender_identity:
        ctx["sender_identity"] = sender_identity
    return ctx or None


def _trigger_error(
    tool_name: str,
    error_class: str,
    message: str,
    *,
    actionable_hint: str | None = None,
    retryable: bool = False,
) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider="trigger",
        retryable=retryable,
        actionable_hint=actionable_hint,
    )


def _stamp_user_declined_plan_mode(config: dict) -> dict:
    if not trusted_plan_mode_user_declined():
        return config
    stamped = stamp_user_declined_plan_exemption(config)
    recommendation_id = trusted_plan_mode_user_decline_metadata().get("recommendation_id")
    if recommendation_id:
        metadata = dict(stamped.get("metadata") or {})
        metadata["plan_recommendation_id"] = str(recommendation_id)
        stamped["metadata"] = metadata
    return stamped


def _validate_trigger_config(tool_name: str, trigger_type: str, config: dict) -> str | None:
    if not isinstance(config, dict):
        return _trigger_error(
            tool_name,
            "bad_arguments",
            "Trigger config must be a JSON object.",
            actionable_hint="Pass a config object that matches the trigger type requirements.",
        )

    if trigger_type == "cron":
        expr = str(config.get("expr", "")).strip()
        if not expr:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                "cron trigger requires config.expr.",
                actionable_hint='Use a cron expression such as {"expr": "0 9 * * *"}.',
            )
        try:
            from croniter import croniter

            croniter(expr)
        except Exception:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                f"Invalid cron expression: '{expr}'",
                actionable_hint="Provide a valid cron expression before saving the trigger.",
            )
    elif trigger_type == "once":
        at = str(config.get("at", "")).strip()
        if not at:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                "once trigger requires config.at.",
                actionable_hint='Use an ISO timestamp such as {"at": "2026-03-10T09:00:00+08:00"}.',
            )
        try:
            datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                f"Invalid once trigger timestamp: '{at}'",
                actionable_hint="Pass a valid ISO-8601 timestamp with timezone information.",
            )
    elif trigger_type == "interval":
        if "minutes" not in config and "interval" in config:
            config["minutes"] = config.get("interval")
        minutes = config.get("minutes")
        try:
            minutes_int = int(minutes)
        except (ValueError, TypeError):
            minutes_int = 0
        if minutes_int <= 0:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                "interval trigger requires config.minutes to be a positive integer.",
                actionable_hint='Use a config such as {"minutes": 30}.',
            )
    elif trigger_type == "poll":
        url = str(config.get("url", "")).strip()
        if not url:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                "poll trigger requires config.url.",
                actionable_hint='Use a config such as {"url": "https://example.com/status"}.',
            )
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return _trigger_error(
                tool_name,
                "bad_arguments",
                f"Invalid poll trigger URL: '{url}'",
                actionable_hint="Provide a full http:// or https:// URL.",
            )
    elif trigger_type == "on_message":
        if config.get("reply_to_current_sender"):
            return None
        from_agent_id = str(config.get("from_agent_id", "")).strip()
        from_user_identity = str(config.get("from_user_identity", "")).strip()
        if from_agent_id:
            try:
                uuid.UUID(from_agent_id)
            except ValueError:
                return _trigger_error(
                    tool_name,
                    "bad_arguments",
                    f"Invalid from_agent_id: '{from_agent_id}'",
                    actionable_hint="Pass a valid UUID string for config.from_agent_id.",
                )
        if not any(
            [
                config.get("from_agent_name"),
                from_agent_id,
                config.get("from_user_name"),
                from_user_identity,
            ]
        ):
            return _trigger_error(
                tool_name,
                "bad_arguments",
                "on_message trigger requires config.reply_to_current_sender, config.from_agent_id/config.from_agent_name, or config.from_user_identity/config.from_user_name.",
                actionable_hint="Specify the current sender or which agent/human user identity should wake this trigger.",
            )
    return None


def _resolve_trigger_class(
    tool_name: str,
    arguments: dict,
    config: dict,
    *,
    trigger_type: str | None = None,
) -> tuple[str | None, str | None]:
    raw_class = (
        arguments.get("trigger_class")
        or config.get("trigger_class")
        or config.get("trigger_kind")
        or config.get("kind")
    )
    trigger_class = str(raw_class or "").strip()
    if not trigger_class:
        if trigger_type in EVENT_WAIT_TRIGGER_TYPES:
            trigger_class = "event_wait"
        elif trigger_type in SCHEDULED_TRIGGER_TYPES:
            trigger_class = "scheduled_job"
        else:
            return None, None
    if trigger_class not in VALID_TRIGGER_CLASSES:
        return None, _trigger_error(
            tool_name,
            "bad_arguments",
            f"Invalid trigger_class '{trigger_class}'. Valid values: {', '.join(sorted(VALID_TRIGGER_CLASSES))}.",
            actionable_hint="Use scheduled_job, event_wait, or system_maintenance.",
        )

    config["trigger_class"] = trigger_class
    return trigger_class, None


def _coerce_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_expires_at(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_trigger_lifecycle_policy(tool_name: str, trigger_type: str, config: dict, arguments: dict) -> str | None:
    trigger_class = str(config.get("trigger_class") or arguments.get("trigger_class") or "").strip()
    if trigger_class != "event_wait":
        return None
    max_fires = _coerce_int(arguments.get("max_fires") or config.get("max_fires"))
    expires_at = arguments.get("expires_at") or config.get("expires_at")
    if max_fires or expires_at:
        return None
    return _trigger_error(
        tool_name,
        "bad_arguments",
        "event_wait trigger requires max_fires or expires_at to prevent indefinite waiting.",
        actionable_hint="Pass max_fires=1 for one reply/event, or expires_at as an ISO timestamp.",
    )


def _apply_trigger_delivery(tool_name: str, config: dict, arguments: dict) -> str | None:
    """B3 — normalize the optional ``delivery`` field into the trigger config.

    ``delivery`` may arrive at the top level (``arguments``) or inside
    ``config``. Absent → default ``new_invocation`` (config left untouched so the
    default path stays byte-for-byte identical). ``same_session`` requires a
    ``source_session_id`` — supplied by the runtime (service layer injects the
    live session), never restated by the model — and stores both on the config
    so the trigger daemon can route the fire into that chat session. Returns an
    error envelope string on an invalid mode / missing source, else ``None``."""
    raw = arguments.get("delivery")
    if raw is None:
        raw = config.get("delivery")
    delivery = str(raw or "").strip()
    if not delivery:
        return None
    if delivery not in VALID_DELIVERY_MODES:
        return _trigger_error(
            tool_name,
            "bad_arguments",
            f"Invalid delivery '{delivery}'. Valid values: {', '.join(sorted(VALID_DELIVERY_MODES))}.",
            actionable_hint="Use new_invocation (default) or same_session.",
        )
    if delivery == "new_invocation":
        config["delivery"] = "new_invocation"
        config.pop("source_session_id", None)
        return None
    source_session_id = str(config.get("source_session_id") or arguments.get("source_session_id") or "").strip()
    if not source_session_id:
        return _trigger_error(
            tool_name,
            "not_configured",
            "delivery=same_session requires an active chat session to deliver into.",
            actionable_hint="Create a same_session trigger from within a chat session; the runtime supplies the source session.",
        )
    config["delivery"] = "same_session"
    config["source_session_id"] = source_session_id
    return None


async def _handle_set_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new trigger for the agent."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    ttype = arguments.get("type", "").strip()
    config = arguments.get("config", {})
    reason = arguments.get("reason", "").strip()

    if not name:
        return _trigger_error("set_trigger", "bad_arguments", "Missing required argument 'name'.")
    if ttype not in VALID_TRIGGER_TYPES:
        return _trigger_error(
            "set_trigger",
            "bad_arguments",
            f"Invalid trigger type '{ttype}'. Valid types: {', '.join(VALID_TRIGGER_TYPES)}",
        )
    if not reason:
        return _trigger_error("set_trigger", "bad_arguments", "Missing required argument 'reason'.")

    # Validate type-specific config
    validation_error = _validate_trigger_config("set_trigger", ttype, config)
    if validation_error:
        return validation_error
    _trigger_class, binding_error = _resolve_trigger_class(
        "set_trigger",
        arguments,
        config,
        trigger_type=ttype,
    )
    if binding_error:
        return binding_error
    lifecycle_error = _validate_trigger_lifecycle_policy("set_trigger", ttype, config, arguments)
    if lifecycle_error:
        return lifecycle_error
    delivery_error = _apply_trigger_delivery("set_trigger", config, arguments)
    if delivery_error:
        return delivery_error
    if ttype == "on_message":
        if config.get("reply_to_current_sender"):
            config.pop("from_user_name", None)
            config.pop("from_user_identity", None)
            config.pop("from_agent_name", None)
            config.pop("from_agent_id", None)
        # Snapshot the latest message timestamp so we only detect NEW messages after this point
        try:
            from app.models.audit import ChatMessage
            from app.models.chat_session import ChatSession
            from sqlalchemy import cast as sa_cast, String as SaString

            # RLS 阶段2b: chat_messages JOIN chat_sessions — both USING-only.
            # Pin the GUC to the agent's tenant so the snapshot survives the
            # non-owner role (a bare session would fail closed → no _since_ts).
            _snap_tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(_snap_tid) as _snap_db:
                _snap_q = (
                    select(ChatMessage.created_at)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == agent_id,
                        ChatMessage.created_at.isnot(None),
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                _snap_r = await _snap_db.execute(_snap_q)
                _latest_ts = _snap_r.scalar_one_or_none()
                if _latest_ts:
                    config["_since_ts"] = _latest_ts.isoformat()
        except Exception as e:
            logger.debug("Suppressed: %s", e)
    elif ttype == "webhook":
        # Auto-generate a unique token for the webhook URL
        token = secrets.token_urlsafe(8)  # ~11 chars, URL-safe
        config["token"] = token
    config = stamp_confirmed_plan_provenance(
        config,
        plan_id=arguments.get("confirmed_plan_id"),
        plan_version=arguments.get("confirmed_plan_version"),
        plan_hash=arguments.get("confirmed_plan_hash"),
        **_plan_authorization_stamp_kwargs(arguments),
    )
    config = _stamp_user_declined_plan_mode(config)

    try:
        # RLS 阶段1: reads the policy-bearing `agents` row for the trigger limit;
        # scope to the agent's tenant (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Load agent to get per-agent trigger limit
            from app.models.agent import Agent as _AgentModel

            _a_result = await db.execute(select(_AgentModel).where(_AgentModel.id == agent_id))
            _agent_obj = _a_result.scalar_one_or_none()
            agent_max_triggers = (_agent_obj.max_triggers if _agent_obj else None) or MAX_TRIGGERS_PER_AGENT

            # Check max triggers
            from sqlalchemy import func as sa_func

            result = await db.execute(
                select(sa_func.count())
                .select_from(AgentTrigger)
                .where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.is_enabled,
                )
            )
            count = result.scalar() or 0
            if count >= agent_max_triggers:
                return _trigger_error(
                    "set_trigger",
                    "quota_or_billing",
                    f"Maximum trigger limit reached ({agent_max_triggers}). Cancel some triggers first.",
                )

            # Check for duplicate name
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                if existing.is_enabled:
                    return _trigger_error(
                        "set_trigger",
                        "bad_arguments",
                        f"Trigger '{name}' already exists and is active. Use update_trigger to modify it, or cancel_trigger first.",
                    )
                # Re-enable disabled trigger with new config (preserve fire history)
                if ttype == "on_message" and config.get("reply_to_current_sender") and not _capture_reply_context():
                    return _trigger_error(
                        "set_trigger",
                        "not_configured",
                        "reply_to_current_sender requires a live channel conversation with a persisted reply target.",
                    )
                existing.type = ttype
                existing.config = {
                    **config,
                    "schedule_decision_entry": _set_trigger_schedule_decision_entry(
                        arguments,
                        trigger_id=existing.id,
                        trigger_type=ttype,
                    ),
                }
                existing.reason = reason
                existing.is_enabled = True
                existing.max_fires = _coerce_int(arguments.get("max_fires") or config.get("max_fires"))
                if arguments.get("expires_at") or config.get("expires_at"):
                    existing.expires_at = _parse_expires_at(arguments.get("expires_at") or config.get("expires_at"))
                if arguments.get("cooldown_seconds") is not None or config.get("cooldown_seconds") is not None:
                    existing.cooldown_seconds = (
                        _coerce_int(arguments.get("cooldown_seconds") or config.get("cooldown_seconds"))
                        or existing.cooldown_seconds
                    )
                existing.reply_context = _capture_reply_context()
                # Keep fire_count and last_fired_at — they are cumulative stats
                await db.commit()
                return f"✅ Trigger '{name}' re-enabled with new configuration ({ttype}, fired {existing.fire_count} times so far)"

            # Auto-capture reply channel context from current session
            reply_ctx = _capture_reply_context()
            if ttype == "on_message" and config.get("reply_to_current_sender") and not reply_ctx:
                return _trigger_error(
                    "set_trigger",
                    "not_configured",
                    "reply_to_current_sender requires a live channel conversation with a persisted reply target.",
                )
            # RLS 阶段2b: agent_triggers is USING-only — stamp tenant_id so the
            # new trigger isn't globally visible under the non-owner role.
            trigger = AgentTrigger(
                agent_id=agent_id,
                tenant_id=tid,
                name=name,
                type=ttype,
                config=config,
                reason=reason,
                max_fires=_coerce_int(arguments.get("max_fires") or config.get("max_fires")),
                expires_at=(
                    _parse_expires_at(arguments.get("expires_at") or config.get("expires_at"))
                    if arguments.get("expires_at") or config.get("expires_at")
                    else None
                ),
                cooldown_seconds=_coerce_int(arguments.get("cooldown_seconds") or config.get("cooldown_seconds")) or 60,
                reply_context=reply_ctx or None,
            )
            db.add(trigger)
            await db.flush()
            trigger.config = {
                **config,
                "schedule_decision_entry": _set_trigger_schedule_decision_entry(
                    arguments,
                    trigger_id=trigger.id,
                    trigger_type=ttype,
                ),
            }
            await db.commit()

        # Activity log
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "trigger_created",
                {
                    "name": name,
                    "type": ttype,
                    "reason": reason[:100],
                },
                agent_id=agent_id,
            )
        except Exception as e:
            logger.debug("Suppressed: %s", e)
        if ttype == "webhook":
            from app.config import get_settings

            settings = get_settings()
            base = getattr(settings, "PUBLIC_URL", "") or ""
            if not base:
                base = "https://try.hive.ai"  # fallback
            webhook_url = f"{base.rstrip('/')}/api/webhooks/t/{config['token']}"
            return f"✅ Webhook trigger '{name}' created.\n\nWebhook URL: {webhook_url}\n\nTell the user to configure this URL in their external service (e.g. GitHub, Grafana). When the service sends a POST to this URL, you will be woken up with the payload as context."

        return f"✅ Trigger '{name}' created ({ttype}). It will fire according to your config and wake you up with the reason as context."

    except Exception as e:
        return _trigger_error("set_trigger", "operation_failed", f"Failed to create trigger: {e}", retryable=True)


async def _handle_update_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    """Update an existing trigger's config or reason."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    if not name:
        return _trigger_error("update_trigger", "bad_arguments", "Missing required argument 'name'.")

    new_config = arguments.get("config")
    new_reason = arguments.get("reason")

    if (
        new_config is None
        and new_reason is None
        and arguments.get("trigger_class") is None
        and arguments.get("max_fires") is None
        and arguments.get("expires_at") is None
        and arguments.get("cooldown_seconds") is None
    ):
        return _trigger_error(
            "update_trigger",
            "bad_arguments",
            "Provide at least one of 'config', 'reason', 'trigger_class', 'max_fires', 'expires_at', or 'cooldown_seconds' to update.",
        )

    try:
        # RLS 阶段2b: agent_triggers is USING-only. Pin the GUC to the agent's
        # tenant so the SELECT+UPDATE survive the non-owner role (a bare session
        # would fail closed → "trigger not found").
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            trigger = result.scalar_one_or_none()
            if not trigger:
                return _trigger_error("update_trigger", "not_found", f"Trigger '{name}' not found.")

            changes = []
            final_config = dict(trigger.config or {})
            if new_config is not None:
                validation_error = _validate_trigger_config("update_trigger", trigger.type, new_config)
                if validation_error:
                    return validation_error
                final_config = dict(new_config)
            if arguments.get("trigger_class") is not None:
                final_config["trigger_class"] = arguments.get("trigger_class")
            final_config = stamp_confirmed_plan_provenance(
                final_config,
                plan_id=arguments.get("confirmed_plan_id"),
                plan_version=arguments.get("confirmed_plan_version"),
                plan_hash=arguments.get("confirmed_plan_hash"),
                **_plan_authorization_stamp_kwargs(arguments),
            )
            final_config = _stamp_user_declined_plan_mode(final_config)

            _trigger_class, binding_error = _resolve_trigger_class(
                "update_trigger",
                arguments,
                final_config,
                trigger_type=trigger.type,
            )
            if binding_error:
                return binding_error
            lifecycle_error = _validate_trigger_lifecycle_policy(
                "update_trigger", trigger.type, final_config, arguments
            )
            if lifecycle_error:
                return lifecycle_error

            if new_config is not None or arguments.get("trigger_class") is not None:
                old_config = trigger.config
                trigger.config = final_config
                changes.append(f"config: {old_config} → {final_config}")
            if arguments.get("max_fires") is not None:
                trigger.max_fires = _coerce_int(arguments.get("max_fires"))
                changes.append(f"max_fires: {trigger.max_fires}")
            if arguments.get("expires_at") is not None:
                trigger.expires_at = (
                    _parse_expires_at(arguments.get("expires_at")) if arguments.get("expires_at") else None
                )
                changes.append(f"expires_at: {trigger.expires_at.isoformat() if trigger.expires_at else '(cleared)'}")
            if arguments.get("cooldown_seconds") is not None:
                trigger.cooldown_seconds = _coerce_int(arguments.get("cooldown_seconds")) or trigger.cooldown_seconds
                changes.append(f"cooldown_seconds: {trigger.cooldown_seconds}")
            if new_reason is not None:
                trigger.reason = new_reason
                changes.append("reason updated")

            # Refresh reply_context if called from a channel session —
            # fixes triggers created before unified-delivery that have
            # reply_context=NULL and thus can't deliver back to the channel.
            fresh_ctx = _capture_reply_context()
            if fresh_ctx and fresh_ctx.get("channel"):
                trigger.reply_context = fresh_ctx
                changes.append(f"reply_context refreshed ({fresh_ctx.get('channel')})")

            await db.commit()

        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "trigger_updated",
                {
                    "name": name,
                    "changes": "; ".join(changes),
                },
                agent_id=agent_id,
            )
        except Exception as e:
            logger.debug("Suppressed: %s", e)

        return f"✅ Trigger '{name}' updated: {'; '.join(changes)}"

    except Exception as e:
        return _trigger_error("update_trigger", "operation_failed", f"Failed to update trigger: {e}", retryable=True)


async def _handle_cancel_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    """Cancel (disable) a trigger by name."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    if not name:
        return _trigger_error("cancel_trigger", "bad_arguments", "Missing required argument 'name'.")

    try:
        # RLS 阶段2b: agent_triggers is USING-only. Pin the GUC to the agent's
        # tenant so the SELECT+UPDATE survive the non-owner role.
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            trigger = result.scalar_one_or_none()
            if not trigger:
                return _trigger_error("cancel_trigger", "not_found", f"Trigger '{name}' not found.")
            if not trigger.is_enabled:
                return f"ℹ️ Trigger '{name}' is already disabled"

            trigger.is_enabled = False
            await db.commit()

        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log("trigger_cancelled", {"name": name}, agent_id=agent_id)
        except Exception as e:
            logger.debug("Suppressed: %s", e)

        return f"✅ Trigger '{name}' cancelled. It will no longer fire."

    except Exception as e:
        return _trigger_error("cancel_trigger", "operation_failed", f"Failed to cancel trigger: {e}", retryable=True)


async def _handle_schedule_wakeup(agent_id: uuid.UUID, arguments: dict) -> str:
    """B2 self-pace: the model schedules (or cancels) its own next wakeup.

    Creates a ``once`` trigger delivered into the SAME session (B3 rail): the
    prompt fires back into this conversation after ``delay_seconds`` (clamped
    to [SELF_PACE_WAKEUP_MIN_SECONDS, SELF_PACE_WAKEUP_MAX_SECONDS]), the model
    re-schedules each round, and ``stop=true`` ends the loop. One pending
    wakeup per session: a new call supersedes the previous one. Fires go
    through the normal trigger-daemon sequence, so preflight, failure policy,
    and budget admission all apply.
    """
    from datetime import timedelta

    from app.models.trigger import AgentTrigger

    session_id = str(arguments.get("source_session_id") or "").strip()
    if not session_id:
        return _trigger_error(
            "schedule_wakeup",
            "bad_arguments",
            "schedule_wakeup requires a live chat session (runtime supplies it).",
        )
    stop = bool(arguments.get("stop"))
    prompt = str(arguments.get("prompt") or "").strip()
    if not stop and not prompt:
        return _trigger_error(
            "schedule_wakeup",
            "bad_arguments",
            "schedule_wakeup requires a prompt for the next wakeup (or stop=true to end the loop).",
            actionable_hint='Pass {"delay_seconds": 300, "prompt": "..."} or {"stop": true}.',
        )

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        pending_rows = (
            (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == agent_id,
                        AgentTrigger.type == "once",
                        AgentTrigger.is_enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        cancelled: list[str] = []
        for row in pending_rows:
            config = dict(getattr(row, "config", None) or {})
            if config.get("self_pace") and str(config.get("source_session_id") or "") == session_id:
                row.is_enabled = False
                cancelled.append(str(getattr(row, "id", "")))

        if stop:
            await db.flush()
            await db.commit()
            return _json_payload(
                {
                    "ok": True,
                    "status": "stopped",
                    "cancelled_wakeups": cancelled,
                    "message": "Self-pace loop stopped; no further wakeups are scheduled.",
                }
            )

        try:
            delay = int(arguments.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            delay = 0
        clamped = max(SELF_PACE_WAKEUP_MIN_SECONDS, min(SELF_PACE_WAKEUP_MAX_SECONDS, delay))
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=clamped)
        trigger = AgentTrigger(
            agent_id=agent_id,
            tenant_id=tid,
            name=f"wakeup-{uuid.uuid4().hex[:8]}",
            type="once",
            config={
                "at": fire_at.isoformat(),
                "trigger_class": "scheduled_job",
                "delivery": "same_session",
                "source_session_id": session_id,
                "self_pace": True,
            },
            reason=prompt,
            is_enabled=True,
            cooldown_seconds=1,
        )
        db.add(trigger)
        await db.flush()
        payload = {
            "ok": True,
            "status": "scheduled",
            "wakeup_id": str(getattr(trigger, "id", "")),
            "delay_seconds": clamped,
            "requested_delay_seconds": delay,
            "at": fire_at.isoformat(),
            "superseded_wakeups": cancelled,
            "message": (
                f"Next wakeup in {clamped}s. You will receive your prompt in this session; "
                "re-schedule each round or call schedule_wakeup(stop=true) to end the loop."
            ),
        }
        await db.commit()
        return _json_payload(payload)


def _json_payload(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


async def _handle_list_triggers(agent_id: uuid.UUID) -> str:
    """List all active triggers for the agent."""
    import app.services.autonomous_audit as autonomous_audit
    from app.models.agent import Agent
    from app.models.trigger import AgentTrigger

    try:
        # RLS 阶段1: reads the policy-bearing `agents` row for the autonomy
        # snapshot; scope to the agent's tenant (audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(AgentTrigger)
                .where(
                    AgentTrigger.agent_id == agent_id,
                )
                .order_by(AgentTrigger.created_at.desc())
            )
            triggers = result.scalars().all()
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()

        lines = []
        if not triggers:
            lines.append("No triggers found. Use set_trigger to create one.")
        else:
            lines.extend(
                [
                    "| Name | Type | Config | Reason | Status | Fires |",
                    "|------|------|--------|--------|--------|-------|",
                ]
            )
        for t in triggers:
            status = "✅ active" if t.is_enabled else "⏸ disabled"
            config_str = str(t.config)[:50]
            reason_str = t.reason[:40] if t.reason else ""
            lines.append(f"| {t.name} | {t.type} | {config_str} | {reason_str} | {status} | {t.fire_count} |")

        if agent is not None:
            report = autonomous_audit.audit_agent_autonomy_snapshot(
                agent=agent,
                triggers=list(triggers),
            )
            findings = report.get("findings", [])
            if findings:
                lines.extend(
                    [
                        "",
                        "## Trigger Diagnostics",
                        "| Severity | Category | Trigger | Recommendation |",
                        "|----------|----------|---------|----------------|",
                    ]
                )
                trigger_names_by_id = {str(t.id): t.name for t in triggers}
                for finding in findings[:20]:
                    trigger_id = finding.get("trigger_id")
                    trigger_name = trigger_names_by_id.get(str(trigger_id), "") if trigger_id else ""
                    lines.append(
                        "| {severity} | {category} | {trigger} | {recommendation} |".format(
                            severity=finding.get("severity", ""),
                            category=finding.get("category", ""),
                            trigger=trigger_name,
                            recommendation=str(finding.get("recommendation", "")).replace("|", "/")[:160],
                        )
                    )
                if len(findings) > 20:
                    lines.append(f"| info | truncated | | {len(findings) - 20} more diagnostics omitted |")

        return "\n".join(lines)

    except Exception as e:
        return _trigger_error("list_triggers", "operation_failed", f"Failed to list triggers: {e}", retryable=True)
