"""Canonical authority boundary for external channel senders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import not_, or_, select, update
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
class ExternalChannelSourceReader:
    """A platform read of one channel run's sources; carries no tool authority."""

    agent_id: uuid.UUID
    session_id: uuid.UUID
    runtime_task_id: uuid.UUID
    external_principal_id: uuid.UUID
    requester_user_id: None = None


async def load_external_context_actor(db, *, tenant_id, agent_id, session, task):
    from app.services.session_v2_persistence import require_external_session_source_access

    if (
        session is None
        or task is None
        or session.tenant_id != tenant_id
        or session.agent_id != agent_id
        or task.tenant_id != tenant_id
        or task.parent_agent_id != agent_id
        or task.parent_session_id != str(session.id)
        or session.audience_kind not in {None, "private"}
        or session.user_id is not None
        or task.root_user_id is not None
        or task.machine_credential_id is not None
        or session.external_principal_id is None
        or task.root_external_principal_id != session.external_principal_id
    ):
        return None
    try:
        actor = await load_external_runtime_actor(
            db, tenant_id=tenant_id, principal_id=session.external_principal_id, expected_user_id=None
        )
        if actor.id is not None or actor.authority_bound:
            return None
        await require_external_session_source_access(db, actor=actor, agent_id=agent_id, session_id=session.id)
    except (LookupError, PermissionError):
        return None
    return actor


async def external_request_source_actor(db, *, tenant_id, request):
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    context = getattr(request, "context", None) or request
    user_id = getattr(context, "user_id", None) or getattr(context, "requester_user_id", None)
    if user_id is not None:
        return None
    nested = getattr(context, "session_context", None)
    metadata = getattr(nested, "metadata", None) or {}
    try:
        agent_id = uuid.UUID(str(context.agent_id))
        session_id = uuid.UUID(str(getattr(context, "session_id", None) or getattr(nested, "session_id", None)))
        run_id = uuid.UUID(
            str(getattr(context, "runtime_task_id", None) or metadata.get("runtime_task_id") or metadata.get("task_id"))
        )
    except (AttributeError, ValueError, TypeError):
        return None
    session = await db.get(ChatSession, session_id, populate_existing=True)
    task = await db.get(RuntimeTask, run_id, populate_existing=True)
    actor = await load_external_context_actor(db, tenant_id=tenant_id, agent_id=agent_id, session=session, task=task)
    if (
        actor is not None
        and getattr(context, "external_principal_id", actor.external_principal_id) != actor.external_principal_id
    ):
        return None
    return (session, task, actor) if actor is not None else None


@dataclass(frozen=True, slots=True)
class ExternalPrincipalResolution:
    principal: ExternalPrincipal
    actor: ChannelRuntimeActor
    # True only when THIS operation invalidated a channel self identity (the
    # config was bound to the principal's non-null previous binding). Routes
    # use it to decide whether live transport clients must stop; repeated or
    # never-bound unlinks leave it False so a healthy channel keeps running.
    channel_identity_invalidated: bool = False


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
                select(User)
                .where(
                    User.id == linked_user_id,
                    User.tenant_id == principal.tenant_id,
                    User.is_active.is_(True),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    bound = bool(
        user is not None
        and principal.status == "active"
        and principal.binding_method
        in {"wechat_qr", "feishu_qr", "feishu_tenant_identity", "company_channel_challenge"}
        and principal.binding_verified_at is not None
    )
    if bound and principal.binding_method == "feishu_tenant_identity":
        from app.models.tenant_channel_config import TenantChannelConfig
        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.tenant_feishu import resolve_tenant_feishu_identity

        config = (
            await db.get(TenantChannelConfig, principal.tenant_channel_config_id, populate_existing=True)
            if principal.tenant_channel_config_id
            else None
        )
        proof = await resolve_tenant_feishu_identity(db, config=config, sender={"open_id": principal.subject_id})
        bound = bool(
            config is not None
            and principal.installation_ref == channel_installation_ref(config, fallback="")
            and proof is not None
            and proof[0].id == user.id
        )
    if bound and principal.binding_method == "company_channel_challenge":
        from app.services.company_channel_identity import company_binding_proof

        bound = await company_binding_proof(db, principal=principal, user_id=user.id) is not None
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
    tenant_channel_config_id: uuid.UUID | str | None = None,
) -> ExternalPrincipalResolution:
    """Resolve one provider subject without manufacturing a platform User."""

    tenant_uuid, provider_value, installation_value, subject_value, principal_id = _identity_parts(
        tenant_id=tenant_id,
        provider=provider,
        installation_ref=installation_ref,
        subject_id=subject_id,
    )
    config_uuid = _uuid(channel_config_id, field="channel_config_id") if channel_config_id else None
    tenant_config_uuid = (
        _uuid(tenant_channel_config_id, field="tenant_channel_config_id") if tenant_channel_config_id else None
    )
    if config_uuid and tenant_config_uuid:
        raise ExternalPrincipalAuthorityError("external principal has two installation kinds")
    await db.execute(
        insert(ExternalPrincipal)
        .values(
            id=principal_id,
            tenant_id=tenant_uuid,
            provider=provider_value,
            installation_ref=installation_value,
            channel_config_id=config_uuid,
            tenant_channel_config_id=tenant_config_uuid,
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
    if (
        tenant_config_uuid
        and principal.tenant_channel_config_id not in (None, tenant_config_uuid)
        or config_uuid
        and principal.tenant_channel_config_id
        or tenant_config_uuid
        and principal.channel_config_id
    ):
        raise ExternalPrincipalAuthorityError("external principal installation kind drifted")
    principal.tenant_channel_config_id = principal.tenant_channel_config_id or tenant_config_uuid
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
            select(ExternalPrincipal)
            .where(
                ExternalPrincipal.id == principal_uuid,
                ExternalPrincipal.tenant_id == tenant_uuid,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if principal is None:
        raise LookupError("external principal not found")
    if principal.status != "active":
        raise ExternalPrincipalRevokedError("external principal is revoked")
    return await _runtime_actor(db, principal, expected_user_id=expected_user_id)


async def _synchronize_external_principal_session_users(
    db: AsyncSession,
    *,
    principal: ExternalPrincipal,
    user: User | None,
) -> None:
    """Keep identity binding and all existing channel sessions in one transaction."""

    from fastapi import HTTPException

    from app.core.permissions import check_agent_access
    from app.models.audit import ChatMessage
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession

    sessions = list(
        (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.tenant_id == principal.tenant_id,
                    ChatSession.external_principal_id == principal.id,
                )
                .with_for_update()
            )
        ).scalars()
    )
    if user is not None:
        agent_ids = {session.agent_id for session in sessions}
        if principal.channel_config_id is not None:
            config = await db.get(ChannelConfig, principal.channel_config_id)
            if config is None or config.tenant_id != principal.tenant_id:
                raise ExternalPrincipalAuthorityError("external principal installation is unavailable")
            agent_ids.add(config.agent_id)
        for agent_id in agent_ids:
            try:
                await check_agent_access(db, user, agent_id)
            except (HTTPException, PermissionError) as exc:
                raise ExternalPrincipalAuthorityError(
                    "binding target does not have access to the channel Agent"
                ) from exc

    target_user_id = user.id if user is not None else None
    for session in sessions:
        session.user_id = target_user_id
        session.actor_type = "external_principal"
    await db.execute(
        update(ChatMessage)
        .where(
            ChatMessage.tenant_id == principal.tenant_id,
            ChatMessage.external_principal_id == principal.id,
        )
        .values(user_id=target_user_id)
    )


async def _link_verified_external_principal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    principal_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | str,
    reason: str,
    binding_method: str,
) -> ExternalPrincipalResolution:
    """Persist a provider-proven self binding from a trusted connect callback.

    This helper is intentionally private: there is no admin/API assignment
    path. The authenticated actor must be the target user and the proof method
    must match the concrete provider installation. Administrators configure a
    managed Agent's channel through the same provider-authenticated connect
    flow using their own identity (PDEC-013); an employee identity is never
    impersonated here.
    """

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
    expected_method = {"wechat_personal": "wechat_qr", "feishu": "feishu_qr"}.get(principal.provider)
    if principal.provider in {"dingtalk", "wecom"} and principal.tenant_channel_config_id is not None:
        from app.services.company_channel_identity import company_binding_proof

        if await company_binding_proof(db, principal=principal, user_id=user_uuid) is None:
            raise ExternalPrincipalAuthorityError("company binding lacks a current self-issued transport proof")
        expected_method = "company_channel_challenge"
    if getattr(principal, "tenant_channel_config_id", None) is not None and principal.provider == "feishu":
        from app.models.tenant_channel_config import TenantChannelConfig
        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.tenant_feishu import resolve_tenant_feishu_identity

        config = await db.get(TenantChannelConfig, principal.tenant_channel_config_id)
        proof = await resolve_tenant_feishu_identity(db, config=config, sender={"open_id": principal.subject_id})
        if (
            proof is None
            or proof[0].id != user_uuid
            or principal.installation_ref != channel_installation_ref(config, fallback="")
        ):
            raise ExternalPrincipalAuthorityError("enterprise binding lacks current app-scoped identity proof")
        expected_method = "feishu_tenant_identity"
    if expected_method is None or binding_method != expected_method:
        raise ExternalPrincipalAuthorityError("external identity binding lacks matching provider proof")
    if actor_uuid != user_uuid:
        raise ExternalPrincipalAuthorityError("external identity must bind the authenticated user")
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
        principal.binding_method = binding_method
        principal.binding_verified_at = principal.linked_at
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="linked",
                previous_user_id=previous,
                new_user_id=user.id,
                actor_user_id=actor_uuid,
                reason=str(reason or "explicit binding"),
                metadata_json={"binding_method": binding_method},
            )
        )
    elif principal.binding_method != binding_method or principal.binding_verified_at is None:
        principal.binding_method = binding_method
        principal.binding_verified_at = datetime.now(UTC)
    await _synchronize_external_principal_session_users(db, principal=principal, user=user)
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
    config = None
    if principal.channel_config_id is not None:
        from app.models.channel_config import ChannelConfig

        config = await db.get(ChannelConfig, principal.channel_config_id)
        if config is not None and config.tenant_id != tenant_uuid:
            raise ExternalPrincipalAuthorityError("external principal channel configuration tenant mismatch")
    if previous is not None:
        principal.linked_user_id = None
        principal.linked_at = None
        principal.binding_method = None
        principal.binding_verified_at = None
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="unlinked",
                previous_user_id=previous,
                new_user_id=None,
                actor_user_id=actor_uuid,
                reason=str(reason or "explicit unlink"),
            )
        )
    # Only a principal that actually held a non-null binding can invalidate a
    # channel self identity, and only when the config was bound to that same
    # user. A never-bound guest and a manually configured channel both carry
    # ``None``; reading that as a match would deconfigure an unrelated
    # healthy channel.
    channel_identity_invalidated = False
    if config is not None and previous is not None and config.self_identity_user_id == previous:
        config.self_identity_user_id = None
        config.self_identity_verified_at = None
        if principal.provider in {"wechat_personal", "feishu"}:
            config.is_connected = False
        if principal.provider == "feishu":
            config.is_configured = False
            config.extra_config = {
                **dict(config.extra_config or {}),
                "connection_status": "identity_rebind_required",
                "identity_status": "rebind_required",
            }
        channel_identity_invalidated = True
    await _synchronize_external_principal_session_users(db, principal=principal, user=None)
    return ExternalPrincipalResolution(
        principal=principal,
        actor=await _runtime_actor(db, principal),
        channel_identity_invalidated=channel_identity_invalidated,
    )


async def bind_authenticated_self_channel_principal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    config: Any,
    provider_subject_id: str,
    user_id: uuid.UUID | str,
    actor_user_id: uuid.UUID | str,
) -> ExternalPrincipalResolution:
    """Bind a provider-authenticated personal account during channel connect.

    This contract only accepts provider-authenticated QR connect callbacks.
    Future senders still receive independent ExternalPrincipal rows; only the
    scanner subject inherits the authenticated Hive user's authority.
    """

    from fastapi import HTTPException

    from app.core.permissions import check_agent_access
    from app.models.chat_session import ChatSession

    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    target_user_id = _uuid(user_id, field="user_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    config_id = getattr(config, "id", None)
    agent_id = getattr(config, "agent_id", None)
    channel_type = str(getattr(config, "channel_type", "") or "").strip().lower()
    subject_id = str(provider_subject_id or "").strip()
    provider_and_method = {
        "wechat_personal": ("wechat_personal", "wechat_qr", "Authenticated WeChat Personal QR connection"),
        "feishu": ("feishu", "feishu_qr", "Authenticated Feishu/Lark QR registration"),
    }.get(channel_type)
    if provider_and_method is None:
        raise ExternalPrincipalAuthorityError("channel does not prove a self identity")
    if not config_id or not agent_id or not subject_id:
        raise ExternalPrincipalAuthorityError("self channel identity requires config, agent, and provider subject")
    if getattr(config, "tenant_id", None) != tenant_uuid:
        raise ExternalPrincipalAuthorityError("self channel configuration tenant mismatch")
    if target_user_id != actor_uuid:
        # The only live configuration lane is the provider-authenticated QR
        # connect callback of the user themselves. An administrator configures
        # a managed Agent's channel through the same flow with the
        # administrator's own provider-authenticated identity; binding an
        # employee's identity on their behalf stays a typed refusal.
        raise ExternalPrincipalAuthorityError("self channel must bind the authenticated installer")

    user = await db.scalar(
        select(User).where(
            User.id == target_user_id,
            User.tenant_id == tenant_uuid,
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise ExternalPrincipalAuthorityError("self channel binding target must be an active same-tenant user")
    try:
        _agent, access_level = await check_agent_access(db, user, agent_id)
    except (HTTPException, PermissionError) as exc:
        raise ExternalPrincipalAuthorityError("self channel installation requires Agent manage access") from exc
    if access_level != "manage":
        raise ExternalPrincipalAuthorityError("self channel installation requires Agent manage access")

    provider, binding_method, reason = provider_and_method
    resolved = await resolve_or_create_external_principal(
        db,
        tenant_id=tenant_uuid,
        provider=provider,
        installation_ref=str(config_id),
        channel_config_id=config_id,
        subject_id=subject_id,
        display_name=user.display_name or user.username,
        profile={
            "identity_source": "authenticated_channel_connect",
            "binding_method": binding_method,
        },
    )
    linked = await _link_verified_external_principal(
        db,
        tenant_id=tenant_uuid,
        principal_id=resolved.principal.id,
        user_id=user.id,
        actor_user_id=actor_uuid,
        reason=reason,
        binding_method=binding_method,
    )

    now = datetime.now(UTC)
    config.self_identity_user_id = user.id
    config.self_identity_verified_at = now

    if provider != "wechat_personal":
        await db.flush()
        return linked

    prior_principal_ids = list(
        (
            await db.execute(
                select(ExternalPrincipal.id).where(
                    ExternalPrincipal.tenant_id == tenant_uuid,
                    ExternalPrincipal.provider == provider,
                    ExternalPrincipal.subject_id == subject_id,
                    ExternalPrincipal.id != linked.principal.id,
                )
            )
        ).scalars()
    )
    session_filter = ChatSession.external_conv_id == f"wechat_p2p_{subject_id}"
    if prior_principal_ids:
        session_filter = or_(session_filter, ChatSession.external_principal_id.in_(prior_principal_ids))
    sessions = list(
        (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.tenant_id == tenant_uuid,
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel == "wechat_personal",
                    session_filter,
                )
            )
        ).scalars()
    )
    for session in sessions:
        existing_principal_id = session.external_principal_id
        if existing_principal_id not in (None, linked.principal.id):
            existing = await db.get(ExternalPrincipal, existing_principal_id)
            if existing is not None and existing.status == "active":
                raise ExternalPrincipalAuthorityError("active WeChat session belongs to another installation")
        session.external_principal_id = linked.principal.id
        session.user_id = user.id

    await db.flush()
    return linked


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
        await _synchronize_external_principal_session_users(db, principal=principal, user=None)
        principal.status = "revoked"
        principal.revoked_at = now
        principal.linked_user_id = None
        principal.linked_at = None
        principal.binding_method = None
        principal.binding_verified_at = None
        db.add(
            ExternalPrincipalBindingEvent(
                tenant_id=tenant_uuid,
                external_principal_id=principal.id,
                action="revoked",
                previous_user_id=previous,
                new_user_id=None,
                actor_user_id=actor_uuid,
                reason=str(reason or "channel installation revoked"),
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
        "feishu",
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
    "bind_authenticated_self_channel_principal",
    "load_external_runtime_actor",
    "platform_member_user_predicate",
    "resolve_or_create_external_principal",
    "revoke_channel_config_external_principals",
    "revoke_external_principals_for_installation",
    "unlink_external_principal",
]
