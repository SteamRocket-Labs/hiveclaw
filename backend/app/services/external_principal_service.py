"""Canonical authority boundary for external channel senders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import not_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_principal import ExternalPrincipal, ExternalPrincipalBindingEvent
from app.models.user import User


_EXTERNAL_PRINCIPAL_NAMESPACE = uuid.UUID("179e52f5-37e9-4da8-9626-6e1eb0b80c80")
LEGACY_SYNTHETIC_EXTERNAL_EMAIL_SUFFIXES = (
    "@slack.local",
    "@telegram.local",
    "@discord.local",
    "@teams.local",
    "@wecom.local",
    "@wechat.local",
    "@dingtalk.local",
)


def platform_member_user_predicate(email_column=User.email):
    """Exclude retired synthetic channel contacts from member/license surfaces."""

    return not_(or_(*(email_column.endswith(suffix) for suffix in LEGACY_SYNTHETIC_EXTERNAL_EMAIL_SUFFIXES)))


class ExternalPrincipalRevokedError(PermissionError):
    """The installation identity exists but its authority was explicitly revoked."""


class ExternalPrincipalAuthorityError(PermissionError):
    """A requested platform-user binding does not match current trusted state."""


@dataclass(frozen=True, slots=True)
class ChannelRuntimeActor:
    """Runtime actor snapshot; ``id`` is present only for an explicit User binding."""

    id: uuid.UUID | None
    external_principal_id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    display_name: str
    role: str
    department_id: uuid.UUID | None
    authority_bound: bool
    is_active: bool
    authority_snapshot_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalPrincipalResolution:
    principal: ExternalPrincipal
    actor: ChannelRuntimeActor


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _identity_parts(
    *,
    tenant_id: uuid.UUID | str,
    provider: str,
    installation_ref: str,
    subject_id: str,
) -> tuple[uuid.UUID, str, str, str, uuid.UUID]:
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    provider_value = str(provider or "").strip().lower()
    installation_value = str(installation_ref or "").strip()
    subject_value = str(subject_id or "").strip()
    if not provider_value or not installation_value or not subject_value:
        raise ValueError("external principal requires provider, installation_ref, and subject_id")
    if len(provider_value) > 40 or len(installation_value) > 200 or len(subject_value) > 512:
        raise ValueError("external principal identity exceeds the storage contract")
    key = "|".join((str(tenant_uuid), provider_value, installation_value, subject_value))
    return (
        tenant_uuid,
        provider_value,
        installation_value,
        subject_value,
        uuid.uuid5(
            _EXTERNAL_PRINCIPAL_NAMESPACE,
            key,
        ),
    )


async def _runtime_actor(
    db: AsyncSession,
    principal: ExternalPrincipal,
    *,
    expected_user_id: uuid.UUID | str | None | object = ...,
) -> ChannelRuntimeActor:
    linked_user_id = principal.linked_user_id
    if expected_user_id is not ...:
        expected_uuid = _uuid(expected_user_id, field="expected_user_id") if expected_user_id else None
        if expected_uuid != linked_user_id:
            linked_user_id = None

    user = None
    if linked_user_id is not None:
        user = (
            await db.execute(
                select(User).where(
                    User.id == linked_user_id,
                    User.tenant_id == principal.tenant_id,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()

    bound = user is not None and principal.status == "active"
    return ChannelRuntimeActor(
        id=user.id if bound else None,
        external_principal_id=principal.id,
        tenant_id=principal.tenant_id,
        username=(user.username if bound else f"{principal.provider}:{principal.subject_id}"),
        display_name=(user.display_name if bound else principal.display_name),
        role=(user.role if bound else "external"),
        department_id=(user.department_id if bound else None),
        authority_bound=bound,
        is_active=principal.status == "active",
        authority_snapshot_at=datetime.now(UTC),
    )


async def resolve_or_create_external_principal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    provider: str,
    installation_ref: str,
    subject_id: str,
    display_name: str,
    profile: dict[str, Any] | None = None,
    channel_config_id: uuid.UUID | str | None = None,
) -> ExternalPrincipalResolution:
    """Resolve one provider subject without manufacturing a platform User."""

    tenant_uuid, provider_value, installation_value, subject_value, principal_id = _identity_parts(
        tenant_id=tenant_id,
        provider=provider,
        installation_ref=installation_ref,
        subject_id=subject_id,
    )
    config_uuid = _uuid(channel_config_id, field="channel_config_id") if channel_config_id else None
    await db.execute(
        insert(ExternalPrincipal)
        .values(
            id=principal_id,
            tenant_id=tenant_uuid,
            provider=provider_value,
            installation_ref=installation_value,
            channel_config_id=config_uuid,
            subject_id=subject_value,
            display_name=str(display_name or "External user")[:200],
            profile_json=dict(profile or {}),
            status="active",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(constraint="uq_external_principals_tenant_provider_installation_subject")
    )
    principal = (
        await db.execute(
            select(ExternalPrincipal)
            .where(
                ExternalPrincipal.tenant_id == tenant_uuid,
                ExternalPrincipal.provider == provider_value,
                ExternalPrincipal.installation_ref == installation_value,
                ExternalPrincipal.subject_id == subject_value,
            )
            .with_for_update()
        )
    ).scalar_one()
    if principal.status != "active":
        raise ExternalPrincipalRevokedError("external principal installation is revoked")
    if config_uuid and principal.channel_config_id not in (None, config_uuid):
        raise ExternalPrincipalAuthorityError("external principal channel configuration drifted")
    principal.channel_config_id = principal.channel_config_id or config_uuid
    principal.display_name = str(display_name or principal.display_name or "External user")[:200]
    principal.profile_json = {**dict(principal.profile_json or {}), **dict(profile or {})}
    principal.last_seen_at = datetime.now(UTC)
    actor = await _runtime_actor(db, principal)

    from app.services.channel_ingress_context import bind_channel_ingress_external_principal

    bind_channel_ingress_external_principal(principal.id)
    return ExternalPrincipalResolution(principal=principal, actor=actor)


async def load_external_runtime_actor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    principal_id: uuid.UUID | str,
    expected_user_id: uuid.UUID | str | None,
) -> ChannelRuntimeActor:
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    principal_uuid = _uuid(principal_id, field="principal_id")
    principal = (
        await db.execute(
            select(ExternalPrincipal).where(
                ExternalPrincipal.id == principal_uuid,
                ExternalPrincipal.tenant_id == tenant_uuid,
            )
        )
    ).scalar_one_or_none()
    if principal is None:
        raise LookupError("external principal not found")
    if principal.status != "active":
        raise ExternalPrincipalRevokedError("external principal is revoked")
    return await _runtime_actor(db, principal, expected_user_id=expected_user_id)


async def link_external_principal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    principal_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | str,
    reason: str,
) -> ExternalPrincipalResolution:
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    principal_uuid = _uuid(principal_id, field="principal_id")
    user_uuid = _uuid(user_id, field="user_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    principal = (
        await db.execute(
            select(ExternalPrincipal)
            .where(
                ExternalPrincipal.id == principal_uuid,
                ExternalPrincipal.tenant_id == tenant_uuid,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if principal is None:
        raise LookupError("external principal not found")
    if principal.status != "active":
        raise ExternalPrincipalRevokedError("external principal is revoked")
    user = (
        await db.execute(
            select(User).where(
                User.id == user_uuid,
                User.tenant_id == tenant_uuid,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise ExternalPrincipalAuthorityError("binding target must be an active user in the same tenant")
    previous = principal.linked_user_id
    if previous != user.id:
        principal.linked_user_id = user.id
        principal.linked_at = datetime.now(UTC)
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="linked",
                previous_user_id=previous,
                new_user_id=user.id,
                actor_user_id=actor_uuid,
                reason=str(reason or "explicit binding")[:1000],
            )
        )
    return ExternalPrincipalResolution(principal=principal, actor=await _runtime_actor(db, principal))


async def unlink_external_principal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    principal_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | str,
    reason: str,
) -> ExternalPrincipalResolution:
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    principal_uuid = _uuid(principal_id, field="principal_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    principal = (
        await db.execute(
            select(ExternalPrincipal)
            .where(
                ExternalPrincipal.id == principal_uuid,
                ExternalPrincipal.tenant_id == tenant_uuid,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if principal is None:
        raise LookupError("external principal not found")
    previous = principal.linked_user_id
    if previous is not None:
        principal.linked_user_id = None
        principal.linked_at = None
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="unlinked",
                previous_user_id=previous,
                new_user_id=None,
                actor_user_id=actor_uuid,
                reason=str(reason or "explicit unlink")[:1000],
            )
        )
    return ExternalPrincipalResolution(principal=principal, actor=await _runtime_actor(db, principal))


async def revoke_external_principals_for_installation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    provider: str,
    installation_ref: str,
    actor_user_id: uuid.UUID | str | None,
    reason: str,
) -> int:
    tenant_uuid, provider_value, installation_value, _subject, _id = _identity_parts(
        tenant_id=tenant_id,
        provider=provider,
        installation_ref=installation_ref,
        subject_id="_installation_scope_",
    )
    actor_uuid = _uuid(actor_user_id, field="actor_user_id") if actor_user_id else None
    principals = list(
        (
            await db.execute(
                select(ExternalPrincipal)
                .where(
                    ExternalPrincipal.tenant_id == tenant_uuid,
                    ExternalPrincipal.provider == provider_value,
                    ExternalPrincipal.installation_ref == installation_value,
                    ExternalPrincipal.status == "active",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for principal in principals:
        previous = principal.linked_user_id
        principal.status = "revoked"
        principal.revoked_at = now
        principal.linked_user_id = None
        principal.linked_at = None
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="revoked",
                previous_user_id=previous,
                new_user_id=None,
                actor_user_id=actor_uuid,
                reason=str(reason or "channel installation revoked")[:1000],
                metadata_json={"provider": provider_value, "installation_ref": installation_value},
            )
        )
    return len(principals)


async def revoke_channel_config_external_principals(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    config: Any,
    actor_user_id: uuid.UUID | str | None,
    reason: str,
) -> int:
    """Retire every principal owned by one concrete channel installation."""

    channel_type = str(getattr(config, "channel_type", "") or "").strip().lower()
    provider = {"microsoft_teams": "teams"}.get(channel_type, channel_type)
    if provider not in {
        "slack",
        "telegram",
        "discord",
        "teams",
        "wecom",
        "dingtalk",
        "wechat_personal",
    }:
        raise ValueError(f"unsupported external principal channel type: {channel_type or '<empty>'}")
    config_id = getattr(config, "id", None)
    agent_id = getattr(config, "agent_id", None)
    installation_ref = str(config_id or f"{provider}:{agent_id}")
    return await revoke_external_principals_for_installation(
        db,
        tenant_id=tenant_id,
        provider=provider,
        installation_ref=installation_ref,
        actor_user_id=actor_user_id,
        reason=reason,
    )


__all__ = [
    "ChannelRuntimeActor",
    "LEGACY_SYNTHETIC_EXTERNAL_EMAIL_SUFFIXES",
    "ExternalPrincipalAuthorityError",
    "ExternalPrincipalResolution",
    "ExternalPrincipalRevokedError",
    "link_external_principal",
    "load_external_runtime_actor",
    "platform_member_user_predicate",
    "resolve_or_create_external_principal",
    "revoke_channel_config_external_principals",
    "revoke_external_principals_for_installation",
    "unlink_external_principal",
]
