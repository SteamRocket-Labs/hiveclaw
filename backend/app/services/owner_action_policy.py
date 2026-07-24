"""Typed, versioned Owner action policy for governed tool execution.

Natural-language charter prose remains model guidance in ``soul.md``.  This
module owns the exact machine contract used by the platform before a tool
effect: a fixed set of action identifiers mapped to ``CharterZone`` values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
import logging
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_revision import ConfigRevision
from app.services.action_preflight import CharterZone
from app.services.config_versioning import canonical_content_hash, save_revision

OWNER_ACTION_POLICY_SCHEMA = "hive.owner_action_policy.v1"
OWNER_ACTION_POLICY_ENTITY_TYPE = "owner_action_policy"

ACTION_EXTERNAL_EFFECT = "tool.external_effect"
ACTION_LOCAL_READ = "tool.local_read"
ACTION_LOCAL_WRITE = "tool.local_write"
OWNER_ACTION_IDS = (
    ACTION_EXTERNAL_EFFECT,
    ACTION_LOCAL_READ,
    ACTION_LOCAL_WRITE,
)
logger = logging.getLogger(__name__)


class OwnerActionPolicyValidationError(ValueError):
    """The persisted/requested policy does not satisfy the exact v1 schema."""


class OwnerActionPolicyVersionConflict(RuntimeError):
    """The caller attempted to replace a policy revision that is no longer active."""


class OwnerActionPolicyRevisionNotFound(LookupError):
    """The requested historical policy revision does not exist in this tenant."""


@dataclass(frozen=True, slots=True)
class OwnerActionPolicy:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID
    actions: Mapping[str, CharterZone]
    version: int
    revision_id: uuid.UUID | None
    content_hash: str
    source: str
    valid: bool = True
    error_code: str | None = None

    def zone_for(self, action_id: str) -> CharterZone:
        """Resolve an exact action identifier; never inspect natural language."""
        if not self.valid:
            return self.actions.get(str(action_id), CharterZone.NEVER_DO)
        return self.actions.get(str(action_id), CharterZone.CONFIRM_FIRST)

    def trace_payload(self, action_id: str) -> dict[str, Any]:
        return {
            "schema": OWNER_ACTION_POLICY_SCHEMA,
            "action_id": str(action_id),
            "zone": self.zone_for(action_id).value,
            "version": self.version,
            "revision_id": str(self.revision_id) if self.revision_id else None,
            "content_hash": self.content_hash,
            "source": self.source,
            "valid": self.valid,
            "error_code": self.error_code,
        }

    def response_payload(self, *, can_manage: bool) -> dict[str, Any]:
        return {
            "schema": OWNER_ACTION_POLICY_SCHEMA,
            "actions": {action_id: self.zone_for(action_id).value for action_id in OWNER_ACTION_IDS},
            "version": self.version,
            "revision_id": str(self.revision_id) if self.revision_id else None,
            "content_hash": self.content_hash,
            "source": self.source,
            "valid": self.valid,
            "error_code": self.error_code,
            "can_manage": can_manage,
        }


def validate_owner_action_policy_actions(
    actions: Mapping[str, str | CharterZone],
) -> dict[str, CharterZone]:
    keys = {str(key) for key in actions}
    expected = set(OWNER_ACTION_IDS)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise OwnerActionPolicyValidationError(
            f"owner action policy requires exact action ids; missing={missing}, extra={extra}"
        )

    normalized: dict[str, CharterZone] = {}
    for action_id in OWNER_ACTION_IDS:
        try:
            normalized[action_id] = CharterZone(str(actions[action_id]))
        except (TypeError, ValueError) as exc:
            raise OwnerActionPolicyValidationError(
                f"invalid charter zone for {action_id}: {actions[action_id]!r}"
            ) from exc
    return normalized


def _policy_content(actions: Mapping[str, str | CharterZone]) -> dict[str, Any]:
    normalized = validate_owner_action_policy_actions(actions)
    return {
        "schema": OWNER_ACTION_POLICY_SCHEMA,
        "actions": {action_id: normalized[action_id].value for action_id in OWNER_ACTION_IDS},
    }


def _actions_from_content(content: Mapping[str, Any]) -> dict[str, CharterZone]:
    if set(content) != {"schema", "actions"}:
        raise OwnerActionPolicyValidationError("owner action policy content has unexpected fields")
    if content.get("schema") != OWNER_ACTION_POLICY_SCHEMA:
        raise OwnerActionPolicyValidationError("owner action policy schema is unsupported")
    actions = content.get("actions")
    if not isinstance(actions, Mapping):
        raise OwnerActionPolicyValidationError("owner action policy actions must be an object")
    return validate_owner_action_policy_actions(actions)


def build_owner_action_policy(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actions: Mapping[str, str | CharterZone],
    version: int = 0,
    revision_id: uuid.UUID | None = None,
    content_hash: str | None = None,
    source: str = "runtime",
) -> OwnerActionPolicy:
    content = _policy_content(actions)
    return OwnerActionPolicy(
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions=_actions_from_content(content),
        version=version,
        revision_id=revision_id,
        content_hash=content_hash or canonical_content_hash(content),
        source=source,
    )


def build_default_owner_action_policy(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> OwnerActionPolicy:
    return build_owner_action_policy(
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions={
            ACTION_EXTERNAL_EFFECT: CharterZone.CONFIRM_FIRST,
            ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
            ACTION_LOCAL_WRITE: CharterZone.FULL_AUTHORITY,
        },
        source="builtin_default",
    )


def build_unavailable_owner_action_policy(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    error_code: str,
) -> OwnerActionPolicy:
    """Preserve unrelated read-only capability while effects fail closed."""
    actions = {
        ACTION_EXTERNAL_EFFECT: CharterZone.NEVER_DO,
        ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
        ACTION_LOCAL_WRITE: CharterZone.NEVER_DO,
    }
    content = _policy_content(actions)
    return OwnerActionPolicy(
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions=_actions_from_content(content),
        version=0,
        revision_id=None,
        content_hash=canonical_content_hash(content),
        source="unavailable",
        valid=False,
        error_code=str(error_code or "policy_dependency_unavailable"),
    )


def render_owner_action_policy_prompt_section(policy: OwnerActionPolicy) -> str:
    """Render the complete exact policy into the model's authorized frame."""
    lines = [
        "## Owner Action Policy",
        (
            f'<owner_action_policy schema="{OWNER_ACTION_POLICY_SCHEMA}" '
            f'version="{policy.version}" content_hash="{policy.content_hash}" '
            f'valid="{str(policy.valid).lower()}">'
        ),
        (
            "This is the platform-enforced tool-effect boundary for this agent. "
            "Use these exact action identifiers; natural-language examples do not change these zones."
        ),
    ]
    lines.extend(f"- {action_id}: {policy.zone_for(action_id).value}" for action_id in OWNER_ACTION_IDS)
    if not policy.valid:
        lines.append(
            f"- recovery_required: {policy.error_code or 'invalid_policy_contract'}; "
            "effectful actions remain blocked until a manager saves or rolls back a valid revision."
        )
    lines.append("</owner_action_policy>")
    return "\n".join(lines)


def _unavailable_owner_action_policy_prompt_section(reason_code: str) -> str:
    return "\n".join(
        [
            "## Owner Action Policy",
            (
                f'<owner_action_policy schema="{OWNER_ACTION_POLICY_SCHEMA}" '
                f'status="unavailable" reason="{reason_code}">'
            ),
            "The policy dependency is unavailable. Internal read-only reasoning may continue, but effectful tools are blocked.",
            f"- {ACTION_EXTERNAL_EFFECT}: never_do",
            f"- {ACTION_LOCAL_READ}: full_authority",
            f"- {ACTION_LOCAL_WRITE}: never_do",
            "</owner_action_policy>",
        ]
    )


async def resolve_owner_action_policy_prompt_section(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
) -> str:
    """Load/backfill policy for the live model-input path without hiding outages."""
    try:
        resolved_tenant_id = uuid.UUID(str(tenant_id))
    except (TypeError, ValueError, AttributeError):
        return _unavailable_owner_action_policy_prompt_section("tenant_unavailable")

    try:
        from app.database import async_session, tenant_scoped_session

        async with tenant_scoped_session(
            resolved_tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="agent_prompt_owner_action_policy",
        ) as policy_db:
            policy = await load_owner_action_policy(
                policy_db,
                agent_id=agent_id,
                tenant_id=resolved_tenant_id,
                create_default=True,
            )
        return render_owner_action_policy_prompt_section(policy)
    except Exception as exc:
        logger.warning(
            "Owner action policy unavailable for prompt assembly (agent=%s, tenant=%s): %s",
            agent_id,
            resolved_tenant_id,
            exc,
        )
        return _unavailable_owner_action_policy_prompt_section("policy_dependency_unavailable")


def _invalid_owner_action_policy(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    revision: Any,
) -> OwnerActionPolicy:
    # Corrupt policy state is an authority failure.  Read-only local discovery
    # remains usable; every effectful/unknown action fails closed and cannot be
    # unlocked by an ordinary approval ticket.
    return OwnerActionPolicy(
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions={
            ACTION_EXTERNAL_EFFECT: CharterZone.NEVER_DO,
            ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
            ACTION_LOCAL_WRITE: CharterZone.NEVER_DO,
        },
        version=int(getattr(revision, "version", 0) or 0),
        revision_id=getattr(revision, "id", None),
        content_hash=str(getattr(revision, "content_hash", "") or ""),
        source=str(getattr(revision, "change_source", "") or "persisted"),
        valid=False,
        error_code="invalid_policy_contract",
    )


def _policy_from_revision(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    revision: Any,
) -> OwnerActionPolicy:
    try:
        actions = _actions_from_content(dict(revision.content or {}))
    except (TypeError, ValueError, OwnerActionPolicyValidationError):
        return _invalid_owner_action_policy(
            agent_id=agent_id,
            tenant_id=tenant_id,
            revision=revision,
        )
    return OwnerActionPolicy(
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions=actions,
        version=int(revision.version),
        revision_id=revision.id,
        content_hash=str(revision.content_hash),
        source=str(revision.change_source or "persisted"),
    )


async def _active_policy_revision(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    for_update: bool = False,
) -> ConfigRevision | None:
    query = (
        select(ConfigRevision)
        .where(
            ConfigRevision.entity_type == OWNER_ACTION_POLICY_ENTITY_TYPE,
            ConfigRevision.entity_id == agent_id,
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.is_active == True,  # noqa: E712
        )
        .order_by(ConfigRevision.version.desc())
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def load_owner_action_policy(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    create_default: bool,
) -> OwnerActionPolicy:
    revision = await _active_policy_revision(
        db,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )
    if revision is None:
        default = build_default_owner_action_policy(agent_id=agent_id, tenant_id=tenant_id)
        if not create_default:
            return default
        try:
            async with db.begin_nested():
                revision = await save_revision(
                    db,
                    entity_type=OWNER_ACTION_POLICY_ENTITY_TYPE,
                    entity_id=agent_id,
                    tenant_id=tenant_id,
                    content=_policy_content(default.actions),
                    change_source="migration",
                    change_message="Backfill default typed Owner action policy for legacy agent.",
                )
        except IntegrityError:
            # Two workers may encounter the same legacy agent before either has
            # committed v1.  The unique revision key selects one winner; the
            # loser rolls back only its savepoint and consumes that winner.
            revision = await _active_policy_revision(
                db,
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
            if revision is None:
                raise
    return _policy_from_revision(
        agent_id=agent_id,
        tenant_id=tenant_id,
        revision=revision,
    )


async def save_owner_action_policy(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actions: Mapping[str, str | CharterZone],
    changed_by_user_id: uuid.UUID,
    expected_version: int | None = None,
    change_message: str = "Owner action policy updated.",
) -> OwnerActionPolicy:
    content = _policy_content(actions)
    current = await _active_policy_revision(
        db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        for_update=True,
    )
    current_version = int(current.version) if current is not None else 0
    if expected_version is not None and expected_version != current_version:
        raise OwnerActionPolicyVersionConflict(
            f"expected policy version {expected_version}, active version is {current_version}"
        )
    revision = await save_revision(
        db,
        entity_type=OWNER_ACTION_POLICY_ENTITY_TYPE,
        entity_id=agent_id,
        tenant_id=tenant_id,
        content=content,
        change_source="user",
        changed_by_user_id=changed_by_user_id,
        change_message=change_message,
    )
    return _policy_from_revision(
        agent_id=agent_id,
        tenant_id=tenant_id,
        revision=revision,
    )


async def rollback_owner_action_policy(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    target_version: int,
    changed_by_user_id: uuid.UUID,
    expected_version: int | None,
    reason: str,
) -> OwnerActionPolicy:
    current = await _active_policy_revision(
        db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        for_update=True,
    )
    current_version = int(current.version) if current is not None else 0
    if expected_version is not None and expected_version != current_version:
        raise OwnerActionPolicyVersionConflict(
            f"expected policy version {expected_version}, active version is {current_version}"
        )

    result = await db.execute(
        select(ConfigRevision).where(
            ConfigRevision.entity_type == OWNER_ACTION_POLICY_ENTITY_TYPE,
            ConfigRevision.entity_id == agent_id,
            ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.version == target_version,
        )
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise OwnerActionPolicyRevisionNotFound(f"Owner action policy version {target_version} was not found")
    content = _policy_content(_actions_from_content(dict(target.content or {})))
    revision = await save_revision(
        db,
        entity_type=OWNER_ACTION_POLICY_ENTITY_TYPE,
        entity_id=agent_id,
        tenant_id=tenant_id,
        content=content,
        change_source="rollback",
        changed_by_user_id=changed_by_user_id,
        change_message=reason,
        rollback_of_revision_id=target.id,
        force_revision=True,
    )
    return _policy_from_revision(
        agent_id=agent_id,
        tenant_id=tenant_id,
        revision=revision,
    )
