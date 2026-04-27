"""Wake-gate and per-job runtime option helpers for autonomous triggers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMModel
from app.models.objective import AgentObjective
from app.services.focus_state import normalize_focus_task_id

ACTIVE_OBJECTIVE_STATUSES = {"active", "open", "running"}


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


async def _load_objective_for_trigger(db: AsyncSession, agent_id: uuid.UUID, trigger: Any) -> Any | None:
    cfg = _config(trigger)
    objective_id = str(cfg.get("objective_id") or "").strip()
    if objective_id:
        try:
            result = await db.execute(
                select(AgentObjective).where(
                    AgentObjective.id == uuid.UUID(objective_id),
                    AgentObjective.agent_id == agent_id,
                )
            )
            return result.scalar_one_or_none()
        except ValueError:
            return None
    focus_ref = str(getattr(trigger, "focus_ref", "") or "").strip()
    if focus_ref:
        result = await db.execute(
            select(AgentObjective).where(
                AgentObjective.agent_id == agent_id,
                AgentObjective.objective_key == normalize_focus_task_id(focus_ref),
            )
        )
        return result.scalar_one_or_none()
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
        if trigger_class(trigger) == "objective_task":
            objective = await _load_objective_for_trigger(db, getattr(agent, "id"), trigger)
            if not objective:
                continue
            metadata = dict(getattr(objective, "metadata_json", None) or {})
            status = str(getattr(objective, "status", "") or "").lower()
            if metadata.get("requires_approval") or status == "proposed":
                return TriggerPreflightResult(
                    False,
                    "objective_requires_approval",
                    f"Objective '{getattr(objective, 'objective_key', '')}' is not approved for autonomous wake.",
                    {"objective_id": str(getattr(objective, "id", "")), "objective_status": status},
                )
            if status not in ACTIVE_OBJECTIVE_STATUSES:
                return TriggerPreflightResult(
                    False,
                    "objective_not_active",
                    f"Objective '{getattr(objective, 'objective_key', '')}' status is {status}.",
                    {"objective_id": str(getattr(objective, "id", "")), "objective_status": status},
                )

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
            lines = [f"Context from {getattr(session, 'title', None) or getattr(session, 'external_conv_id', None) or session.id}:"]
            for message in messages:
                content = str(getattr(message, "content", "") or "").strip()
                if content:
                    lines.append(f"- {getattr(message, 'role', 'message')}: {content[:600]}")
            if len(lines) > 1:
                sections.append("\n".join(lines))
        except Exception:
            continue
    return "\n\n".join(sections)[:6000]
