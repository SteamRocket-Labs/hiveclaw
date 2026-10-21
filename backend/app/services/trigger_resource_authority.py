"""Resource authority helpers shared by Trigger REST and Agent tool surfaces."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect as sqlalchemy_inspect
from sqlalchemy.orm.attributes import flag_modified

from app.core.resource_authority import _root_session_owned_by_user, authorize_resource_action, filter_authorized_resources
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
# Another principal's source, plan, and approval. Reclaim and ordinary-owner
# recovery must not carry these forward. The trigger's own identity stays.
_PRIVATE_AUTHORITY_CONFIG_KEYS = frozenset(
    {
        "root_session_id",
        "source_session_id",
        "confirmed_plan_session_id",
        "context_from",
        "delivery_target_json",
        "plan_id",
        "plan_version",
        "plan_hash",
        "plan_authorization",
    }
)
_PRIVATE_AUTHORITY_METADATA_KEYS = frozenset({"plan_exempt_reason", "plan_recommendation_id"})


def trigger_authority_generation(trigger: AgentTrigger) -> int:
    value = dict(trigger.config or {}).get("_authority_generation", 0)
    if type(value) is not int or value < 0:
        raise ValueError("trigger_authority_generation_unavailable")
    return value


def trigger_fire_authority_block_reason(trigger: AgentTrigger, metadata: dict) -> str | None:
    generations = metadata.get("trigger_authority_generations")
    if generations is None:
        expected = 0  # Existing work remains valid until its first real revocation.
    elif isinstance(generations, dict) and str(trigger.id) in generations:
        expected = generations[str(trigger.id)]
    else:
        return "trigger_authority_generation_unavailable"
    try:
        current = trigger_authority_generation(trigger)
    except ValueError:
        return "trigger_authority_generation_unavailable"
    if type(expected) is not int or expected < 0 or expected > current:
        return "trigger_authority_generation_unavailable"
    return "trigger_authority_revoked" if expected != current else None


def _runtime_config_key(key: object) -> bool:
    return isinstance(key, str) and (key.startswith("_") or key in _RUNTIME_CONFIG_KEYS)


def strip_trigger_runtime_config(candidate_config: dict | None) -> dict:
    """Drop platform-owned runtime keys from an external config candidate."""

    return {key: value for key, value in dict(candidate_config or {}).items() if not _runtime_config_key(key)}


def strip_trigger_untrusted_config(candidate_config: dict | None) -> dict:
    """Caller settings cannot manufacture plan decisions or consumed leases."""
    config = strip_trigger_runtime_config(candidate_config)
    for key in ("plan_id", "plan_version", "plan_hash", "plan_authorization", "confirmed_plan_session_id", *_PRIVATE_AUTHORITY_METADATA_KEYS):
        config.pop(key, None)
    for key in ("metadata", "metadata_json"):
        if isinstance(config.get(key), dict):
            metadata = {name: value for name, value in config[key].items() if name not in _PRIVATE_AUTHORITY_METADATA_KEYS}
            if metadata:
                config[key] = metadata
            else:
                config.pop(key)
    return config


async def require_trigger_source_scope(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    config: dict,
    root_session_id: str | None = None,
) -> uuid.UUID | None:
    """A delivery setting cannot grant private work to another requester."""
    source = config.get("source_session_id")
    if source and config.get("delivery") != "same_session":
        raise HTTPException(400, "source_session_id requires delivery=same_session")
    verified_root = None
    for raw in (source, root_session_id):
        if not raw:
            continue
        try:
            session_id = uuid.UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid trigger source session") from exc
        if owner_user_id is None or not await _root_session_owned_by_user(
            db, root_session_id=session_id, agent_id=agent_id, user_id=owner_user_id,
        ):
            raise HTTPException(403, "Trigger source must belong to its requester on this employee")
        verified_root = session_id
    return verified_root


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
    return _uuid_value(dict(getattr(trigger, "config", None) or {}), "created_by")


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
    stamped.pop("_authority_generation", None)
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
    # A schedule replacement keeps its destination unless delivery changes explicitly.
    for key in ("delivery", "source_session_id"):
        if key not in candidate and key in current_config:
            candidate[key] = current_config[key]
    if candidate.get("delivery") == "new_invocation":
        candidate.pop("source_session_id", None)
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
    return (await db.execute(statement.with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()


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


def trigger_execution_block_reason(trigger: AgentTrigger, _responsible_user_id: uuid.UUID | None = None) -> str | None:
    """Return why this trigger's own authority cannot create a new effect.

    Quarantine and missing ``created_by`` block. A recorded requester who is
    not the agent owner is not stale: resource ownership is ``created_by``.
    The caller still has to revalidate that requester. This helper does not
    grant the agent owner's privilege to a missing or foreign requester.
    """

    config = dict(getattr(trigger, "config", None) or {})
    state = str(config.get("authority_state") or "").strip().lower()
    if state == "quarantined" or trigger_owner_user_id(trigger) is None:
        return "trigger_authority_quarantined"
    return None


def _without_private_authority(config: dict) -> dict:
    cleaned = {key: value for key, value in config.items() if key not in _PRIVATE_AUTHORITY_CONFIG_KEYS}
    metadata = dict(cleaned.get("metadata") or {})
    for key in _PRIVATE_AUTHORITY_METADATA_KEYS:
        metadata.pop(key, None)
    if metadata:
        cleaned["metadata"] = metadata
    else:
        cleaned.pop("metadata", None)
    return cleaned


def _assign_trigger_config(trigger: AgentTrigger, config: dict) -> None:
    trigger.config = config
    try:
        sqlalchemy_inspect(trigger)
    except NoInspectionAvailable:
        return
    flag_modified(trigger, "config")


async def quarantine_transferred_trigger_authority(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    previous_owner_id: uuid.UUID | None,
    reason: str = "agent_owner_transferred",
) -> list[uuid.UUID]:
    """Disable triggers bound to the previous responsible principal.

    Historical ``created_by`` stays in place. Triggers with no ``created_by``,
    and triggers bound to someone else, are left untouched.
    """

    if previous_owner_id is None:
        return []
    rows = list(
        (
            await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id).with_for_update())
        )
        .scalars()
        .all()
    )
    quarantined: list[uuid.UUID] = []
    for trigger in rows:
        owner_id = trigger_owner_user_id(trigger)
        if owner_id is None or str(owner_id) != str(previous_owner_id):
            continue
        config = dict(trigger.config or {})
        if str(config.get("authority_state") or "").strip().lower() == "quarantined":
            continue
        config["authority_state"] = "quarantined"
        config["_authority_generation"] = trigger_authority_generation(trigger) + 1
        config["authority_revoked_reason"] = reason
        _assign_trigger_config(trigger, config)
        trigger.is_enabled = False
        quarantined.append(trigger.id)
    return quarantined


async def quarantine_stale_trigger_authority(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    trigger_ids: list[uuid.UUID],
    reason_by_id: dict | None = None,
) -> list[uuid.UUID]:
    """Quarantine and disable the listed triggers without rewriting ``created_by``."""

    if not trigger_ids:
        return []
    reasons = {str(key): value for key, value in dict(reason_by_id or {}).items()}
    rows = list(
        (
            await db.execute(
                select(AgentTrigger)
                .where(AgentTrigger.agent_id == agent_id, AgentTrigger.id.in_(list(trigger_ids)))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    changed: list[uuid.UUID] = []
    for trigger in rows:
        config = dict(trigger.config or {})
        if str(config.get("authority_state") or "").strip().lower() != "quarantined":
            config["authority_state"] = "quarantined"
            config["_authority_generation"] = trigger_authority_generation(trigger) + 1
            config["authority_revoked_reason"] = (
                config.get("authority_revoked_reason") or reasons.get(str(trigger.id)) or "trigger_authority_stale"
            )
        _assign_trigger_config(trigger, config)
        trigger.is_enabled = False
        changed.append(trigger.id)
    return changed


def caller_may_reclaim_trigger_authority(*, caller: User, agent, principal) -> bool:
    """Current live owner or a scoped administrator may reclaim a trigger.

    An inactive or missing responsible principal blocks reclaim even for an
    administrator. The historical creator has no authority from provenance.
    """

    from app.core.permissions import is_scoped_business_admin

    if principal is None or getattr(principal, "block_reason", None) or getattr(principal, "user_id", None) is None:
        return False
    if getattr(caller, "is_active", True) is False:
        return False
    if str(getattr(caller, "id", "")) == str(principal.user_id):
        return True
    return is_scoped_business_admin(caller, resource_tenant_id=getattr(agent, "tenant_id", None))


def apply_trigger_authority_reclaim(trigger: AgentTrigger, *, live_owner_id: uuid.UUID) -> uuid.UUID | None:
    """Bind revoked authority to the live owner without enabling the trigger.

    Enabling stays on the existing ``enable_autonomous_wake`` gate. The previous
    ``created_by`` is retained as ``previous_authority_owner_id``. Source, plan,
    and approval references from the old principal are cleared. This does not
    rewrite the agent creator.
    """

    if trigger_authority_state(trigger) != "quarantined":
        raise ValueError("trigger_authority_not_revoked")
    previous = trigger_owner_user_id(trigger)
    config = dict(trigger.config or {})
    config["_authority_generation"] = trigger_authority_generation(trigger) + 1
    config["created_by"] = str(live_owner_id)
    config["authority_state"] = "owned"
    config.pop("authority_revoked_reason", None)
    if previous is not None and str(previous) != str(live_owner_id):
        config["previous_authority_owner_id"] = str(previous)
    config = _without_private_authority(config)
    _assign_trigger_config(trigger, config)
    # Channel delivery is a column, not a config key. Leaving it would keep
    # the previous principal's send target after the config refs are gone.
    trigger.reply_context = None
    trigger.is_enabled = False
    return previous


class _RecoveredTrigger:
    """Read-only view. The stored row keeps the old principal's private config."""

    def __init__(self, trigger: AgentTrigger):
        self._trigger = trigger
        self.config = _without_private_authority(dict(getattr(trigger, "config", None) or {}))
        self.reply_context = None

    def __getattr__(self, name: str):
        return getattr(self._trigger, name)


async def append_trigger_authority_recovery(
    db: AsyncSession,
    user: User,
    *,
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    visible: list,
    agent_access,
):
    """Add quarantined recovery rows for the live owner or a scoped admin.

    Ordinary owners receive a redacted view. Scoped admins already pass the
    normal filter with full visibility; rows they can already see are not
    duplicated. A collaborator who cannot reclaim sees nothing extra.
    """

    if agent_access is None:
        return list(visible)
    from app.core.resource_authority import ResourceAuthorityDecision
    from app.services.agent_identity_lifecycle import resolve_live_effect_principal

    agent = agent_access[0]
    principal = await resolve_live_effect_principal(db, agent)
    if not caller_may_reclaim_trigger_authority(caller=user, agent=agent, principal=principal):
        return list(visible)
    live_owner = str(getattr(user, "id", "")) == str(principal.user_id)
    recovered = list(visible)
    seen = {getattr(item, "id", None) for item, _decision in recovered}
    access_level = agent_access[1] if len(agent_access) > 1 else "manage"
    for trigger in triggers:
        trigger_id = getattr(trigger, "id", None)
        if trigger_id in seen or str(getattr(trigger, "agent_id", agent_id)) != str(agent_id):
            continue
        if trigger_authority_state(trigger) != "quarantined":
            continue
        recovered.append(
            (
                _RecoveredTrigger(trigger) if live_owner else trigger,
                ResourceAuthorityDecision(
                    agent=agent,
                    access_level=access_level,
                    resource_kind="trigger",
                    resource_id=trigger_id,
                    action="read",
                    authority_source="trigger_authority_recovery",
                ),
            )
        )
        seen.add(trigger_id)
    return recovered


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
