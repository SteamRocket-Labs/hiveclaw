"""Wake-gate and per-job runtime option helpers for autonomous triggers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMModel
from app.services import plan_mode_core
from app.services.plan_mode_gate import get_plan_mode_gate


@dataclass(slots=True)
class TriggerPreflightResult:
    ok: bool
    skip_reason: str | None = None
    result_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _config(trigger: Any) -> dict[str, Any]:
    return dict(getattr(trigger, "config", None) or {})


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def trigger_class(trigger: Any) -> str:
    return str(_config(trigger).get("trigger_class") or "").strip()


def collect_trigger_runtime_options(triggers: list[Any]) -> dict[str, Any]:
    """Merge explicit per-job runtime options from trigger config.

    The options are intentionally explicit. They do not infer toolsets or
    context; scheduled jobs must declare these fields if they need them.
    """
    allowed_tools: list[str] = []
    excluded_tools: list[str] = []
    context_from: list[Any] = []
    workdir = None
    execution_classes: list[str] = []
    for trigger in triggers:
        cfg = _config(trigger)
        cls = str(cfg.get("trigger_class") or "").strip()
        if cls:
            execution_classes.append(cls)
        for key, target in (("toolset", allowed_tools), ("allowed_tool_names", allowed_tools)):
            value = cfg.get(key)
            if isinstance(value, list):
                target.extend(str(item).strip() for item in value if str(item).strip())
        value = cfg.get("excluded_tool_names")
        if isinstance(value, list):
            excluded_tools.extend(str(item).strip() for item in value if str(item).strip())
        ctx = cfg.get("context_from")
        if isinstance(ctx, list):
            context_from.extend(ctx)
        elif ctx:
            context_from.append(ctx)
        if cfg.get("workdir"):
            workdir = str(cfg.get("workdir"))
    return {
        "execution_class": execution_classes[0] if execution_classes else None,
        "trigger_classes": sorted(set(execution_classes)),
        "allowed_tool_names": tuple(dict.fromkeys(allowed_tools)),
        "excluded_tool_names": tuple(dict.fromkeys(excluded_tools)),
        "context_from": context_from,
        "workdir": workdir,
    }


def _model_ids_from_triggers(triggers: list[Any]) -> list[str]:
    ids: list[str] = []
    for trigger in triggers:
        model_id = str(_config(trigger).get("model_id") or "").strip()
        if model_id:
            ids.append(model_id)
    return list(dict.fromkeys(ids))


async def select_trigger_model(
    db: AsyncSession,
    agent: Any,
    triggers: list[Any],
) -> tuple[Any | None, dict[str, Any], str | None]:
    """Resolve per-job model pinning, falling back to the agent primary model."""
    model_ids = _model_ids_from_triggers(triggers)
    if len(model_ids) > 1:
        return None, {"model_ids": model_ids}, "conflicting_model_pin"

    if model_ids:
        try:
            model_uuid = uuid.UUID(model_ids[0])
        except ValueError:
            return None, {"model_id": model_ids[0]}, "invalid_model_pin"
        result = await db.execute(
            select(LLMModel).where(LLMModel.id == model_uuid, LLMModel.tenant_id == getattr(agent, "tenant_id", None))
        )
        model = result.scalar_one_or_none()
        if not model:
            return None, {"model_id": model_ids[0]}, "model_pin_not_found"
        return model, {"model_id": str(model_uuid), "model_source": "trigger_config"}, None

    primary_model_id = getattr(agent, "primary_model_id", None)
    if not primary_model_id:
        return None, {}, "no_model"
    result = await db.execute(
        select(LLMModel).where(LLMModel.id == primary_model_id, LLMModel.tenant_id == getattr(agent, "tenant_id", None))
    )
    model = result.scalar_one_or_none()
    if not model:
        return None, {"model_id": str(primary_model_id)}, "model_not_found"
    return model, {"model_id": str(primary_model_id), "model_source": "agent_primary"}, None


async def _plan_gate_block_for_triggers(
    db: AsyncSession,
    *,
    agent: Any,
    triggers: list[Any],
) -> TriggerPreflightResult | None:
    """Plan Mode fail-closed backstop (§9.0) for autonomous triggers.

    For each enabled *autonomous* trigger (cron/interval/once/poll, excluding
    platform-internal classes), require proof it came from a confirmed plan
    (``config.plan_id`` -> a
    ``confirmed`` AgentPlanRequest) or carries a cutover exemption
    (``config.metadata.plan_exempt_reason``). The shared :class:`PlanModeGate`
    makes the decision so this backstop and the early-intercept layer answer
    identically.

    Returns a blocking :class:`TriggerPreflightResult` for the first trigger that
    lacks a confirmed plan, or ``None`` when every autonomous trigger is cleared.
    Fail-closed: if the gate raises, the caller treats it as a skip.
    """
    gate = get_plan_mode_gate()
    for trigger in triggers:
        if not plan_mode_core.trigger_is_autonomous(
            trigger_type=getattr(trigger, "type", None),
            trigger_class=trigger_class(trigger),
        ):
            continue
        cfg = _config(trigger)
        plan_id = str(cfg.get("plan_id") or "").strip() or None
        plan_authorization = cfg.get("plan_authorization")
        if plan_id and isinstance(plan_authorization, dict):
            from app.services.plan_authorization_lease import (
                PlanAuthorizationLeaseError,
                verify_consumed_plan_authorization_lease,
            )

            try:
                await verify_consumed_plan_authorization_lease(
                    db=db,
                    tenant_id=getattr(agent, "tenant_id", None),
                    agent_id=getattr(agent, "id"),
                    plan_id=plan_id,
                    evidence=plan_authorization,
                )
                continue
            except PlanAuthorizationLeaseError as exc:
                return TriggerPreflightResult(
                    False,
                    "plan_required",
                    f"Trigger '{getattr(trigger, 'name', '')}' has invalid plan authorization evidence.",
                    {
                        "trigger_id": str(getattr(trigger, "id", "")),
                        "trigger_name": getattr(trigger, "name", None),
                        "plan_gate_reason": f"plan_authorization_{exc.code}",
                    },
                )
        decision = await gate.check(
            db,
            agent_id=getattr(agent, "id"),
            action_kind="create_enabled_trigger",
            action_ref=getattr(trigger, "id", None),
            confirmed_plan_id=plan_id,
            plan_version=cfg.get("plan_version"),
            plan_hash=cfg.get("plan_hash"),
            # The trigger config carries any cutover exemption under
            # ``config.metadata.plan_exempt_reason``; hand the whole trigger
            # shape to the gate so it can probe it.
            action_artifact={"config": cfg},
        )
        if not decision.allowed:
            return TriggerPreflightResult(
                False,
                "plan_required",
                (
                    f"Trigger '{getattr(trigger, 'name', '')}' has no confirmed plan; "
                    "create and confirm a plan before enabling this autonomous wake."
                ),
                {
                    "trigger_id": str(getattr(trigger, "id", "")),
                    "trigger_name": getattr(trigger, "name", None),
                    "plan_gate_reason": decision.reason,
                },
            )
    return None


async def evaluate_trigger_preflight(
    db: AsyncSession,
    *,
    agent: Any,
    model: Any | None,
    triggers: list[Any],
    now: datetime | None = None,
) -> TriggerPreflightResult:
    now = now or datetime.now(timezone.utc)
    if not model:
        return TriggerPreflightResult(False, "no_model", "No model available for trigger wake.")
    if getattr(agent, "status", None) in {"expired", "stopped", "error", "archived"}:
        return TriggerPreflightResult(
            False,
            "agent_not_runnable",
            f"Agent status is {getattr(agent, 'status', None)}.",
            {"agent_status": getattr(agent, "status", None)},
        )

    runtime_options = collect_trigger_runtime_options(triggers)
    for trigger in triggers:
        cfg = _config(trigger)
        backoff_until = _parse_datetime(cfg.get("backoff_until"))
        if backoff_until and now < backoff_until:
            return TriggerPreflightResult(
                False,
                "trigger_backoff_active",
                f"Trigger '{getattr(trigger, 'name', '')}' is in backoff until {backoff_until.isoformat()}.",
                {"trigger_id": str(getattr(trigger, "id", "")), "trigger_name": getattr(trigger, "name", None)},
            )
        if trigger_class(trigger) == "event_wait":
            max_fires = getattr(trigger, "max_fires", None) or cfg.get("max_fires")
            expires_at = getattr(trigger, "expires_at", None) or cfg.get("expires_at")
            if not max_fires and not expires_at:
                return TriggerPreflightResult(
                    False,
                    "event_wait_missing_lifecycle",
                    "event_wait trigger requires max_fires or expires_at.",
                    {"trigger_name": getattr(trigger, "name", None)},
                )

    # Plan Mode fail-closed backstop (§9.0): block autonomous triggers lacking a
    # confirmed plan / cutover exemption.
    plan_block = await _plan_gate_block_for_triggers(db, agent=agent, triggers=triggers)
    if plan_block is not None:
        return plan_block

    return TriggerPreflightResult(True, metadata=runtime_options)


async def load_context_from(db: AsyncSession, *, agent_id: uuid.UUID, context_refs: list[Any]) -> str:
    """Load explicit context references for scheduled jobs.

    Supported forms:
    - "objective:<uuid>" or {"type": "objective", "id": "..."}
    - "session:<uuid>" or {"type": "session", "id": "..."}
    - {"external_conv_id": "objective:<uuid>"}
    """
    if not context_refs:
        return ""
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    sections: list[str] = []
    for ref in context_refs[:8]:
        ref_type = None
        ref_id = None
        external_conv_id = None
        if isinstance(ref, str):
            if ref.startswith("objective:"):
                ref_type = "objective"
                ref_id = ref.removeprefix("objective:")
                external_conv_id = ref
            elif ref.startswith("session:"):
                ref_type = "session"
                ref_id = ref.removeprefix("session:")
            else:
                external_conv_id = ref
        elif isinstance(ref, dict):
            ref_type = str(ref.get("type") or "").strip() or None
            ref_id = str(ref.get("id") or ref.get("objective_id") or ref.get("session_id") or "").strip() or None
            external_conv_id = str(ref.get("external_conv_id") or "").strip() or None
        try:
            session = None
            if external_conv_id:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.external_conv_id == external_conv_id,
                    )
                )
                session = result.scalar_one_or_none()
            elif ref_type == "objective" and ref_id:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.external_conv_id == f"objective:{ref_id}",
                    )
                )
                session = result.scalar_one_or_none()
            elif ref_type == "session" and ref_id:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.id == uuid.UUID(ref_id),
                    )
                )
                session = result.scalar_one_or_none()
            if not session:
                continue
            result = await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role.in_(("user", "assistant")),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(6)
            )
            messages = list(reversed(result.scalars().all()))
            lines = [
                f"Context from {getattr(session, 'title', None) or getattr(session, 'external_conv_id', None) or session.id}:"
            ]
            for message in messages:
                content = str(getattr(message, "content", "") or "").strip()
                if content:
                    lines.append(f"- {getattr(message, 'role', 'message')}: {content[:600]}")
            if len(lines) > 1:
                sections.append("\n".join(lines))
        except Exception:
            continue
    return "\n\n".join(sections)[:6000]
