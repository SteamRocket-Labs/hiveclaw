"""Resource authority helpers shared by Trigger REST and Agent tool surfaces."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resource_authority import authorize_resource_action, filter_authorized_resources
from app.models.trigger import AgentTrigger
from app.models.user import User

_AUTHORITY_KEYS = ("created_by", "root_session_id", "authority_state")
_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "failure_count",
        "last_failure_at",
        "last_failure",
        "backoff_until",
    }
)


def _runtime_config_key(key: object) -> bool:
    return isinstance(key, str) and (key.startswith("_") or key in _RUNTIME_CONFIG_KEYS)


def strip_trigger_runtime_config(candidate_config: dict | None) -> dict:
    """Drop platform-owned runtime keys from an external config candidate."""

    return {key: value for key, value in dict(candidate_config or {}).items() if not _runtime_config_key(key)}


def _uuid_value(config: dict, *keys: str) -> uuid.UUID | None:
    for key in keys:
        raw = config.get(key)
        if not raw:
            continue
        try:
            return uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def trigger_owner_user_id(trigger: AgentTrigger) -> uuid.UUID | None:
    return _uuid_value(dict(trigger.config or {}), "created_by")


def trigger_root_session_id(trigger: AgentTrigger) -> uuid.UUID | None:
    return _uuid_value(
        dict(trigger.config or {}),
        "root_session_id",
        "confirmed_plan_session_id",
        "source_session_id",
    )


def trigger_authority_state(trigger: AgentTrigger) -> str:
    config = dict(trigger.config or {})
    state = str(config.get("authority_state") or "").strip().lower()
    if state in {"owned", "quarantined"}:
        return state
    return "owned" if trigger_owner_user_id(trigger) else "quarantined"


def stamp_trigger_authority(
    config: dict | None,
    *,
    owner_user_id: uuid.UUID | None,
    root_session_id: uuid.UUID | str | None,
) -> dict:
    """Overwrite caller-selected authority fields with trusted runtime values."""

    stamped = dict(config or {})
    if owner_user_id is None:
        stamped.pop("created_by", None)
        stamped.pop("root_session_id", None)
        stamped["authority_state"] = "quarantined"
        return stamped
    stamped["created_by"] = str(owner_user_id)
    stamped["authority_state"] = "owned"
    if root_session_id:
        stamped["root_session_id"] = str(root_session_id)
    else:
        stamped.pop("root_session_id", None)
    return stamped


def preserve_trigger_authority(current: AgentTrigger, candidate_config: dict | None) -> dict:
    """A content update cannot alter authority or daemon-owned runtime state."""

    current_config = dict(current.config or {})
    candidate = strip_trigger_runtime_config(candidate_config)
    for key, value in current_config.items():
        if _runtime_config_key(key):
            candidate[key] = value
    for key in _AUTHORITY_KEYS:
        if key in current_config:
            candidate[key] = current_config[key]
        else:
            candidate.pop(key, None)
    return candidate


async def lock_trigger_for_update(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    trigger_type: str | None = None,
) -> AgentTrigger | None:
    """Reload the canonical trigger row under the mutation transaction lock."""

    statement = select(AgentTrigger).where(
        AgentTrigger.id == trigger_id,
        AgentTrigger.agent_id == agent_id,
    )
    if trigger_type is not None:
        statement = statement.where(AgentTrigger.type == trigger_type)
    return (await db.execute(statement.with_for_update())).scalar_one_or_none()


def require_trigger_not_fire_inflight(trigger: AgentTrigger) -> None:
    marker = dict(trigger.config or {}).get("_fire_inflight")
    if not isinstance(marker, dict):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "ok": False,
            "status": "trigger_fire_inflight",
            "runtime_task_id": str(marker.get("runtime_task_id") or "") or None,
        },
    )


async def load_trigger_requester(db: AsyncSession, user_id: uuid.UUID | None) -> User | None:
    if user_id is None:
        return None
    return (await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))).scalar_one_or_none()


async def authorize_trigger_action(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    trigger: AgentTrigger,
    action: str,
    agent_access=None,
    operator_view: bool = False,
    operator_reason: str | None = None,
):
    return await authorize_resource_action(
        db,
        user,
        agent_id=agent_id,
        resource_kind="trigger",
        resource_id=trigger.id,
        action=action,
        owner_user_id=trigger_owner_user_id(trigger),
        root_session_id=trigger_root_session_id(trigger),
        authority_state=trigger_authority_state(trigger),
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
        agent_access=agent_access,
    )


async def filter_authorized_triggers(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    agent_access=None,
    operator_view: bool = False,
    operator_reason: str | None = None,
):
    return await filter_authorized_resources(
        db,
        user,
        agent_id=agent_id,
        resource_kind="trigger",
        action="read",
        resources=triggers,
        owner_user_id_of=trigger_owner_user_id,
        root_session_id_of=trigger_root_session_id,
        authority_state_of=trigger_authority_state,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
