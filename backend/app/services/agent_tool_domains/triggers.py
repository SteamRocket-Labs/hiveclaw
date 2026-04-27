"""Trigger management domain — CRUD for agent triggers (Aware Engine)."""

import logging
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select

from app.database import async_session
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

MAX_TRIGGERS_PER_AGENT = 20
VALID_TRIGGER_TYPES = {"cron", "once", "interval", "poll", "on_message", "webhook"}
VALID_TRIGGER_CLASSES = {"objective_task", "scheduled_job", "event_wait", "system_maintenance"}
EVENT_WAIT_TRIGGER_TYPES = {"poll", "on_message", "webhook"}
SCHEDULED_TRIGGER_TYPES = {"cron", "once", "interval"}


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
        if not any([
            config.get("from_agent_name"),
            from_agent_id,
            config.get("from_user_name"),
            from_user_identity,
        ]):
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
    focus_ref: str | None,
    *,
    trigger_type: str | None = None,
) -> tuple[str | None, str | None]:
    raw_class = (
        arguments.get("trigger_class")
        or config.get("trigger_class")
        or config.get("trigger_kind")
        or config.get("kind")
    )
    objective_id = str(arguments.get("objective_id") or config.get("objective_id") or "").strip()
    trigger_class = str(raw_class or "").strip()
    if not trigger_class:
        if focus_ref or objective_id:
            trigger_class = "objective_task"
        elif trigger_type in EVENT_WAIT_TRIGGER_TYPES:
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
            actionable_hint="Use objective_task, scheduled_job, event_wait, or system_maintenance.",
        )

    if trigger_class == "objective_task" and not (focus_ref or objective_id):
        return None, _trigger_error(
            tool_name,
            "bad_arguments",
            "trigger_class='objective_task' requires focus_ref or config.objective_id.",
            actionable_hint="Bind the objective trigger to a focus.md checklist id via focus_ref, or pass config.objective_id once the objective ledger exists.",
        )
    config["trigger_class"] = trigger_class
    if objective_id:
        config["objective_id"] = objective_id
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


async def _bind_objective_for_trigger(db, agent, config: dict, focus_ref: str | None, reason: str) -> bool:
    if str(config.get("trigger_class") or "").strip() != "objective_task":
        return False
    from app.services.objective_service import ensure_objective_for_trigger

    objective = await ensure_objective_for_trigger(
        db,
        agent,
        focus_ref=str(focus_ref).strip() if focus_ref else None,
        description=reason,
        objective_id=str(config.get("objective_id") or "").strip() or None,
    )
    if not objective:
        return False
    objective_id = str(objective.id)
    if config.get("objective_id") == objective_id:
        return False
    config["objective_id"] = objective_id
    return True


async def _handle_set_trigger(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new trigger for the agent."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    ttype = arguments.get("type", "").strip()
    config = arguments.get("config", {})
    reason = arguments.get("reason", "").strip()
    focus_ref = arguments.get("focus_ref", "") or arguments.get("agenda_ref", "")  # backward compat

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
        focus_ref,
        trigger_type=ttype,
    )
    if binding_error:
        return binding_error
    lifecycle_error = _validate_trigger_lifecycle_policy("set_trigger", ttype, config, arguments)
    if lifecycle_error:
        return lifecycle_error
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
            async with async_session() as _snap_db:
                _snap_q = select(ChatMessage.created_at).join(
                    ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                ).where(
                    ChatSession.agent_id == agent_id,
                    ChatMessage.created_at.isnot(None),
                ).order_by(ChatMessage.created_at.desc()).limit(1)
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

    try:
        async with async_session() as db:
            # Load agent to get per-agent trigger limit
            from app.models.agent import Agent as _AgentModel
            _a_result = await db.execute(select(_AgentModel).where(_AgentModel.id == agent_id))
            _agent_obj = _a_result.scalar_one_or_none()
            agent_max_triggers = (_agent_obj.max_triggers if _agent_obj else None) or MAX_TRIGGERS_PER_AGENT

            # Check max triggers
            from sqlalchemy import func as sa_func
            result = await db.execute(
                select(sa_func.count()).select_from(AgentTrigger).where(
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
                await _bind_objective_for_trigger(
                    db,
                    _agent_obj or type("AgentRef", (), {"id": agent_id, "tenant_id": None})(),
                    config,
                    focus_ref,
                    reason,
                )
                existing.type = ttype
                existing.config = config
                existing.reason = reason
                existing.focus_ref = focus_ref or None
                existing.is_enabled = True
                existing.max_fires = _coerce_int(arguments.get("max_fires") or config.get("max_fires"))
                if arguments.get("expires_at") or config.get("expires_at"):
                    existing.expires_at = _parse_expires_at(arguments.get("expires_at") or config.get("expires_at"))
                if arguments.get("cooldown_seconds") is not None or config.get("cooldown_seconds") is not None:
                    existing.cooldown_seconds = _coerce_int(
                        arguments.get("cooldown_seconds") or config.get("cooldown_seconds")
                    ) or existing.cooldown_seconds
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
            await _bind_objective_for_trigger(
                db,
                _agent_obj or type("AgentRef", (), {"id": agent_id, "tenant_id": None})(),
                config,
                focus_ref,
                reason,
            )

            trigger = AgentTrigger(
                agent_id=agent_id,
                name=name,
                type=ttype,
                config=config,
                reason=reason,
                focus_ref=focus_ref or None,
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
            await db.commit()

        # Activity log
        try:
            from app.services.audit_logger import write_audit_log
            await write_audit_log("trigger_created", {
                "name": name, "type": ttype, "reason": reason[:100],
            }, agent_id=agent_id)
        except Exception as e:
            logger.debug("Suppressed: %s", e)
        if ttype == "webhook":
            from app.config import get_settings
            settings = get_settings()
            base = getattr(settings, 'PUBLIC_URL', '') or ''
            if not base:
                base = 'https://try.hive.ai'  # fallback
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
    if "focus_ref" in arguments:
        new_focus_ref = arguments.get("focus_ref")
    elif "agenda_ref" in arguments:
        new_focus_ref = arguments.get("agenda_ref")
    else:
        new_focus_ref = None

    if (
        new_config is None
        and new_reason is None
        and new_focus_ref is None
        and arguments.get("trigger_class") is None
        and arguments.get("objective_id") is None
        and arguments.get("max_fires") is None
        and arguments.get("expires_at") is None
        and arguments.get("cooldown_seconds") is None
    ):
        return _trigger_error(
            "update_trigger",
            "bad_arguments",
            "Provide at least one of 'config', 'reason', 'focus_ref', or 'trigger_class' to update.",
        )

    try:
        async with async_session() as db:
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
            final_focus_ref = getattr(trigger, "focus_ref", None)
            if new_config is not None:
                validation_error = _validate_trigger_config("update_trigger", trigger.type, new_config)
                if validation_error:
                    return validation_error
                final_config = dict(new_config)
            if arguments.get("trigger_class") is not None:
                final_config["trigger_class"] = arguments.get("trigger_class")
            if arguments.get("objective_id") is not None:
                final_config["objective_id"] = arguments.get("objective_id")
            if new_focus_ref is not None:
                final_focus_ref = str(new_focus_ref).strip() or None

            _trigger_class, binding_error = _resolve_trigger_class(
                "update_trigger",
                arguments,
                final_config,
                final_focus_ref,
                trigger_type=trigger.type,
            )
            if binding_error:
                return binding_error
            lifecycle_error = _validate_trigger_lifecycle_policy("update_trigger", trigger.type, final_config, arguments)
            if lifecycle_error:
                return lifecycle_error

            objective_bound = False
            if _trigger_class == "objective_task":
                from app.models.agent import Agent as _AgentModel

                agent_result = await db.execute(select(_AgentModel).where(_AgentModel.id == agent_id))
                agent_obj = agent_result.scalar_one_or_none()
                objective_bound = await _bind_objective_for_trigger(
                    db,
                    agent_obj or type("AgentRef", (), {"id": agent_id, "tenant_id": None})(),
                    final_config,
                    final_focus_ref,
                    new_reason or trigger.reason,
                )

            if (
                new_config is not None
                or arguments.get("trigger_class") is not None
                or arguments.get("objective_id") is not None
                or objective_bound
            ):
                old_config = trigger.config
                trigger.config = final_config
                changes.append(f"config: {old_config} → {final_config}")
            if arguments.get("max_fires") is not None:
                trigger.max_fires = _coerce_int(arguments.get("max_fires"))
                changes.append(f"max_fires: {trigger.max_fires}")
            if arguments.get("expires_at") is not None:
                trigger.expires_at = _parse_expires_at(arguments.get("expires_at")) if arguments.get("expires_at") else None
                changes.append(f"expires_at: {trigger.expires_at.isoformat() if trigger.expires_at else '(cleared)'}")
            if arguments.get("cooldown_seconds") is not None:
                trigger.cooldown_seconds = _coerce_int(arguments.get("cooldown_seconds")) or trigger.cooldown_seconds
                changes.append(f"cooldown_seconds: {trigger.cooldown_seconds}")
            if new_focus_ref is not None:
                trigger.focus_ref = final_focus_ref
                changes.append(f"focus_ref: {final_focus_ref or '(cleared)'}")
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
            await write_audit_log("trigger_updated", {
                "name": name, "changes": "; ".join(changes),
            }, agent_id=agent_id)
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
        async with async_session() as db:
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


async def _handle_list_triggers(agent_id: uuid.UUID) -> str:
    """List all active triggers for the agent."""
    import app.services.autonomous_audit as autonomous_audit
    from app.models.agent import Agent
    from app.models.trigger import AgentTrigger

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                ).order_by(AgentTrigger.created_at.desc())
            )
            triggers = result.scalars().all()
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()

        lines = []
        if not triggers:
            lines.append("No triggers found. Use set_trigger to create one.")
        else:
            lines.extend([
                "| Name | Type | Config | Reason | Status | Fires |",
                "|------|------|--------|--------|--------|-------|",
            ])
        for t in triggers:
            status = "✅ active" if t.is_enabled else "⏸ disabled"
            config_str = str(t.config)[:50]
            reason_str = t.reason[:40] if t.reason else ""
            lines.append(f"| {t.name} | {t.type} | {config_str} | {reason_str} | {status} | {t.fire_count} |")

        if agent is not None:
            focus_text, focus_error = autonomous_audit._read_focus_text(agent_id)
            report = autonomous_audit.audit_agent_autonomy_snapshot(
                agent=agent,
                focus_text=focus_text,
                triggers=list(triggers),
                focus_read_error=focus_error,
            )
            findings = report.get("findings", [])
            if findings:
                lines.extend([
                    "",
                    "## Trigger Diagnostics",
                    "| Severity | Category | Focus | Trigger | Recommendation |",
                    "|----------|----------|-------|---------|----------------|",
                ])
                trigger_names_by_id = {str(t.id): t.name for t in triggers}
                for finding in findings[:20]:
                    trigger_id = finding.get("trigger_id")
                    trigger_name = trigger_names_by_id.get(str(trigger_id), "") if trigger_id else ""
                    lines.append(
                        "| {severity} | {category} | {focus_ref} | {trigger} | {recommendation} |".format(
                            severity=finding.get("severity", ""),
                            category=finding.get("category", ""),
                            focus_ref=finding.get("focus_ref") or "",
                            trigger=trigger_name,
                            recommendation=str(finding.get("recommendation", "")).replace("|", "/")[:160],
                        )
                    )
                if len(findings) > 20:
                    lines.append(f"| info | truncated | | | {len(findings) - 20} more diagnostics omitted |")

        return "\n".join(lines)

    except Exception as e:
        return _trigger_error("list_triggers", "operation_failed", f"Failed to list triggers: {e}", retryable=True)
