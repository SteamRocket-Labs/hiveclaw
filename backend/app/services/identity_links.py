"""Manual directory-identity repair and the company-knowledge audience overlay.

A link relates one stored directory subject to one account. It does not grant
access, change a role or password, bind an external principal, or rewrite an
identity row.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy import write_audit_event
from app.database import enter_rls_bypass
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgMember
from app.models.organization_identity import IdentityCutoverJournal, OrgMemberUserLink
from app.models.user import User
from app.services.company_knowledge_identity import company_knowledge_grant_audiences

SUBJECT_KINDS = ("provider_user_id", "provider_open_id", "provider_union_id")
_KIND_FIELDS = {
    "provider_open_id": ("feishu_open_id", "open_id"),
    "provider_union_id": ("unionid",),
    "provider_user_id": ("feishu_user_id", "external_id"),
}
_BINDING_FIELD = {
    "provider_user_id": "provider_user_id",
    "provider_open_id": "provider_open_id",
    "provider_union_id": "provider_union_id",
}


class IdentityLinkRejected(Exception):
    """A repair that must not write. The HTTP layer returns this as a typed body."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.reason = code
        self.message = message
        self.retryable = retryable
        self.http_status = 409

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "message": self.message,
            "retryable": self.retryable,
            "applied": False,
            "link": None,
        }


@dataclass
class IdentityLinkOutcome:
    applied: bool
    code: str
    reason: str
    message: str
    retryable: bool
    link: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "code": self.code,
            "reason": self.reason,
            "message": self.message,
            "retryable": self.retryable,
            "link": self.link,
        }


@dataclass(frozen=True)
class SubjectMap:
    values: dict[str, str]
    ambiguous_kinds: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguous_kinds)


@dataclass(frozen=True)
class _Binding:
    id: uuid.UUID
    provider_id: uuid.UUID
    user_id: uuid.UUID
    provider_user_id: str | None
    provider_open_id: str | None
    provider_union_id: str | None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def member_subject_map(member: Any) -> SubjectMap:
    """Copy rules for one directory person. Disagreeing aliases are not a match."""

    values: dict[str, str] = {}
    ambiguous: list[str] = []
    for kind, fields in _KIND_FIELDS.items():
        found: list[str] = []
        for field in fields:
            cleaned = _clean(getattr(member, field, None))
            if cleaned is not None and cleaned not in found:
                found.append(cleaned)
        if len(found) > 1:
            ambiguous.append(kind)
        elif len(found) == 1:
            values[kind] = found[0]
    return SubjectMap(values, tuple(ambiguous))


def link_version(link: Any) -> str:
    payload = "|".join(
        [
            str(link.id),
            str(link.status),
            str(link.user_id),
            str(link.provider_id),
            str(link.subject_kind),
            str(link.match_subject),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _advisory_key(namespace: str, *parts: object) -> int:
    raw = "\n".join((namespace, *[str(part) for part in parts])).encode()
    digest = hashlib.blake2s(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _bindings_matching(bindings: list[_Binding], provider_id: uuid.UUID, kind: str, subject: str) -> list[_Binding]:
    field = _BINDING_FIELD[kind]
    return [
        row
        for row in bindings
        if row.provider_id == provider_id and _clean(getattr(row, field)) == subject
    ]


def _legacy_feishu_provenance(member: Any) -> bool:
    """A null provider_id is Feishu only when a stored Feishu subject field exists."""

    for field in ("feishu_open_id", "open_id", "unionid", "feishu_user_id"):
        if _clean(getattr(member, field, None)) is not None:
            return True
    return False


def _namespace_reason(member: Any, provider: Any) -> str | None:
    """Return a rejection when stored provenance does not prove this provider.

    An assigned provider_id must be that provider. A null provider_id keeps the
    legacy Feishu path and does not authorize any other provider type.
    """

    member_provider_id = getattr(member, "provider_id", None)
    if member_provider_id is not None:
        if member_provider_id != getattr(provider, "id", None):
            return "provider_namespace"
        return None
    if _legacy_feishu_provenance(member) and getattr(provider, "provider_type", None) == "feishu":
        return None
    return "unproven_provider"


def recheck_stored_link(
    link: Any,
    *,
    member: Any | None,
    user: Any | None,
    provider: Any | None,
    bindings: list[_Binding],
) -> tuple[bool, str]:
    """A stored row is authority only while the current records still agree."""

    if link.status != "active":
        return False, str(link.status)
    if user is None or getattr(user, "tenant_id", None) is None or user.tenant_id != link.tenant_id:
        return False, "cross_tenant"
    if getattr(user, "is_active", None) is not True:
        return False, "inactive_user"
    if (
        provider is None
        or getattr(provider, "tenant_id", None) is None
        or provider.tenant_id != link.tenant_id
        or getattr(provider, "is_active", None) is not True
    ):
        return False, "unproven_provider"
    if member is None or getattr(member, "tenant_id", None) != link.tenant_id:
        return False, "stale_subject"
    if getattr(member, "id", None) != link.org_member_id:
        return False, "conflict"
    namespace = _namespace_reason(member, provider)
    if namespace is not None:
        return False, namespace
    subjects = member_subject_map(member)
    if subjects.ambiguous:
        return False, "ambiguous_subject"
    current = subjects.values.get(link.subject_kind)
    if current is None or current != link.match_subject:
        return False, "stale_subject"
    for kind, value in subjects.values.items():
        matches = _bindings_matching(bindings, link.provider_id, kind, value)
        if len(matches) != 1 and matches:
            return False, "conflict"
        if matches and matches[0].user_id != link.user_id:
            return False, "conflict"
    requested = _bindings_matching(bindings, link.provider_id, link.subject_kind, link.match_subject)
    if len(requested) > 1:
        return False, "conflict"
    expected_identity = requested[0].id if requested else None
    if link.external_identity_id != expected_identity:
        return False, "stale_subject"
    return True, "active"


def history_reason(reasons: list[str]) -> str:
    if "conflict" in reasons:
        return "conflict"
    if "inactive_user" in reasons or "authenticated_user_inactive" in reasons:
        return "authenticated_user_inactive"
    if "stale_subject" in reasons or "ambiguous_subject" in reasons:
        return "stale_subject"
    if "unproven_provider" in reasons or "provider_namespace" in reasons:
        return "unproven_provider"
    if "cross_tenant" in reasons:
        return "cross_tenant"
    if reasons and all(reason == "revoked" for reason in reasons):
        return "revoked"
    return "unregistered"


def audiences_with_persistent_links(
    users: list[Any],
    members: list[Any],
    links: list[Any],
    recheck,
) -> dict[str, list[dict[str, str]]]:
    """Keep the pure matcher for people who have never had a link row."""

    history_ids = {link.org_member_id for link in links}
    legacy = company_knowledge_grant_audiences(
        users, [member for member in members if member.id not in history_ids]
    )
    grantable = {item["user_id"]: item for item in legacy["grantable_users"]}
    unlinked = list(legacy["visible_unlinked"])
    ambiguous_users: set[str] = set()
    for member in members:
        if member.id not in history_ids:
            continue
        member_links = [link for link in links if link.org_member_id == member.id]
        valid_users: list[str] = []
        reasons: list[str] = []
        for link in member_links:
            valid, reason = recheck(link, member)
            if valid:
                valid_users.append(str(link.user_id))
            else:
                reasons.append(reason)
        distinct = list(dict.fromkeys(valid_users))
        if len(distinct) == 1:
            continue
        if len(distinct) > 1:
            ambiguous_users.update(distinct)
            reason = "ambiguous_identity"
        else:
            reason = history_reason(reasons)
        unlinked.append(
            {
                "org_member_id": str(member.id),
                "label": str(getattr(member, "name", "") or "Directory person"),
                "reason": reason,
            }
        )
    for user_id in ambiguous_users:
        grantable.pop(user_id, None)
    grantable_rows = sorted(grantable.values(), key=lambda item: (item["label"], item["user_id"]))
    unlinked.sort(key=lambda item: (item["label"], item["org_member_id"]))
    return {"grantable_users": grantable_rows, "visible_unlinked": unlinked}


def _reject(code: str, message: str, *, retryable: bool = False) -> None:
    raise IdentityLinkRejected(code, message, retryable=retryable)


def _reject_unproven_namespace(member: Any, provider: Any) -> None:
    namespace = _namespace_reason(member, provider)
    if namespace == "provider_namespace":
        _reject("provider_namespace", "The directory person belongs to a different provider.")
    if namespace is not None:
        _reject("unproven_provider", "The directory person's stored provider is not proven for this link.")


def _public_link(link: OrgMemberUserLink, *, valid: bool, reason: str) -> dict[str, Any]:
    evidence = link.evidence_json or {}
    return {
        "id": str(link.id),
        "org_member_id": str(link.org_member_id),
        "user_id": str(link.user_id),
        "provider_id": str(link.provider_id) if link.provider_id else None,
        "subject_kind": link.subject_kind,
        "status": link.status,
        "stored_status": link.status,
        "version": link_version(link),
        "source": evidence.get("source"),
        "created_by_user_id": str(link.created_by_user_id) if link.created_by_user_id else None,
        "valid": valid,
        "reason": reason,
    }


def _snapshot(link: OrgMemberUserLink) -> dict[str, Any]:
    evidence = link.evidence_json or {}
    return {
        "org_member_id": str(link.org_member_id),
        "user_id": str(link.user_id),
        "provider_id": str(link.provider_id) if link.provider_id else None,
        "subject_kind": link.subject_kind,
        "match_subject": link.match_subject,
        "status": link.status,
        "external_identity_id": str(link.external_identity_id) if link.external_identity_id else None,
        "source": evidence.get("source"),
    }


def _member_reason(subjects: SubjectMap, links: list[tuple[bool, str, uuid.UUID]]) -> tuple[bool, str]:
    if subjects.ambiguous:
        return False, "ambiguous_subject"
    valid_accounts = list(dict.fromkeys(str(user_id) for valid, _reason, user_id in links if valid))
    if len(valid_accounts) == 1:
        return True, "active"
    if len(valid_accounts) > 1:
        return False, "ambiguous_identity"
    if links:
        return False, history_reason([reason for valid, reason, _user_id in links if not valid])
    if not subjects.values:
        return False, "missing_provider_subject"
    return False, "unregistered"


async def _lock(session: AsyncSession, keys: list[int]) -> None:
    for key in sorted(set(keys)):
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def _bindings_for_providers(
    session: AsyncSession,
    provider_ids: list[uuid.UUID],
    *,
    actor_id: uuid.UUID | None,
) -> list[_Binding]:
    if not provider_ids:
        return []
    async with enter_rls_bypass(
        session,
        reason="identity link subject binding check",
        actor_id=str(actor_id) if actor_id else None,
    ):
        rows = (
            await session.execute(
                select(
                    ExternalIdentity.id,
                    ExternalIdentity.provider_id,
                    ExternalIdentity.user_id,
                    ExternalIdentity.provider_user_id,
                    ExternalIdentity.provider_open_id,
                    ExternalIdentity.provider_union_id,
                ).where(ExternalIdentity.provider_id.in_(provider_ids))
            )
        ).all()
    return [
        _Binding(
            id=row.id,
            provider_id=row.provider_id,
            user_id=row.user_id,
            provider_user_id=row.provider_user_id,
            provider_open_id=row.provider_open_id,
            provider_union_id=row.provider_union_id,
        )
        for row in rows
    ]


async def _hidden_user(session: AsyncSession, user_id: uuid.UUID, actor_id: uuid.UUID) -> tuple[bool, uuid.UUID | None, bool]:
    async with enter_rls_bypass(session, reason="identity link account tenant check", actor_id=str(actor_id)):
        row = (
            await session.execute(select(User.id, User.tenant_id, User.is_active).where(User.id == user_id))
        ).one_or_none()
    if row is None:
        return False, None, False
    return True, row.tenant_id, row.is_active is True


async def _hidden_provider(
    session: AsyncSession, provider_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[bool, uuid.UUID | None, bool]:
    async with enter_rls_bypass(session, reason="identity link provider tenant check", actor_id=str(actor_id)):
        row = (
            await session.execute(
                select(IdentityProvider.id, IdentityProvider.tenant_id, IdentityProvider.is_active).where(
                    IdentityProvider.id == provider_id
                )
            )
        ).one_or_none()
    if row is None:
        return False, None, False
    return True, row.tenant_id, row.is_active is True


async def _peek_member(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> OrgMember | None:
    return (
        await session.execute(select(OrgMember).where(OrgMember.id == member_id, OrgMember.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def _revalidate_for_update(session: AsyncSession, statement, cached: Any | None):
    """Reload the locked row over the identity-map copy from before the lock."""

    if cached is not None:
        session.expire(cached)
    return (
        await session.execute(
            statement.with_for_update(),
            execution_options={"populate_existing": True},
        )
    ).scalar_one_or_none()


async def _lock_member(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> OrgMember | None:
    cached = await session.get(OrgMember, member_id)
    return await _revalidate_for_update(
        session,
        select(OrgMember).where(OrgMember.id == member_id, OrgMember.tenant_id == tenant_id),
        cached,
    )


async def _reject_missing_member(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    async with enter_rls_bypass(session, reason="identity link directory tenant check", actor_id=str(actor_id)):
        other = (await session.execute(select(OrgMember.tenant_id).where(OrgMember.id == member_id))).scalar_one_or_none()
    if other is not None and other != tenant_id:
        _reject("cross_tenant", "That directory person cannot be linked in this company.")
    _reject("member_not_in_tenant", "That directory person is not in this company.")


async def _require_user(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, actor_id: uuid.UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is not None and user.tenant_id == tenant_id:
        return user
    found, other_tenant, _active = await _hidden_user(session, user_id, actor_id)
    if not found or other_tenant is None or other_tenant != tenant_id:
        _reject("cross_tenant", "That account cannot be linked in this company.")
    _reject("cross_tenant", "That account cannot be linked in this company.")
    raise AssertionError("unreachable")


async def _require_provider(
    session: AsyncSession, tenant_id: uuid.UUID, provider_id: uuid.UUID, actor_id: uuid.UUID
) -> IdentityProvider:
    provider = (
        await session.execute(select(IdentityProvider).where(IdentityProvider.id == provider_id))
    ).scalar_one_or_none()
    if provider is None or provider.tenant_id != tenant_id:
        found, other_tenant, _active = await _hidden_provider(session, provider_id, actor_id)
        if not found or other_tenant != tenant_id:
            _reject("unproven_provider", "That provider is not a proven provider for this company.")
        _reject("unproven_provider", "That provider is not a proven provider for this company.")
    if provider.tenant_id is None or provider.tenant_id != tenant_id or provider.is_active is not True:
        _reject("unproven_provider", "That provider is not a proven provider for this company.")
    return provider


def _lock_keys(
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    provider_id: uuid.UUID,
    subjects: SubjectMap,
) -> list[int]:
    keys = [_advisory_key("member", tenant_id, member_id)]
    for kind, value in subjects.values.items():
        keys.append(_advisory_key("subject", tenant_id, provider_id, kind, value))
    return keys


async def _active_links(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    subject_kind: str,
    match_subject: str,
    org_member_id: uuid.UUID,
) -> list[OrgMemberUserLink]:
    rows = (
        await session.execute(
            select(OrgMemberUserLink).where(
                OrgMemberUserLink.tenant_id == tenant_id,
                OrgMemberUserLink.status == "active",
                OrgMemberUserLink.provider_id == provider_id,
                OrgMemberUserLink.subject_kind == subject_kind,
                (
                    (OrgMemberUserLink.match_subject == match_subject)
                    | (OrgMemberUserLink.org_member_id == org_member_id)
                ),
            )
        )
    ).scalars().all()
    return list(rows)


def _same_row(
    link: OrgMemberUserLink,
    *,
    org_member_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_id: uuid.UUID,
    subject_kind: str,
    match_subject: str,
    status: str,
) -> bool:
    return (
        link.org_member_id == org_member_id
        and link.user_id == user_id
        and link.provider_id == provider_id
        and link.subject_kind == subject_kind
        and link.match_subject == match_subject
        and link.status == status
    )


def _decide_status(
    *,
    user: User,
    provider_id: uuid.UUID,
    subject_kind: str,
    subjects: SubjectMap,
    bindings: list[_Binding],
    user_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None]:
    conflict = False
    matched_id: uuid.UUID | None = None
    for kind, value in subjects.values.items():
        matches = _bindings_matching(bindings, provider_id, kind, value)
        if len(matches) > 1 or any(match.user_id != user_id for match in matches):
            conflict = True
        if kind == subject_kind and len(matches) == 1 and matches[0].user_id == user_id:
            matched_id = matches[0].id
    if conflict:
        return "conflict", matched_id if matched_id is not None else None
    if user.is_active is not True:
        return "inactive_user", matched_id
    return "active", matched_id


async def _write_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member: OrgMember,
    user: User,
    provider: IdentityProvider,
    subject_kind: str,
    match_subject: str,
    status: str,
    external_identity_id: uuid.UUID | None,
    action: str,
    previous: OrgMemberUserLink | None = None,
) -> OrgMemberUserLink:
    now = datetime.now(timezone.utc)
    link = OrgMemberUserLink(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider.id,
        subject_kind=subject_kind,
        match_subject=match_subject,
        external_identity_id=external_identity_id,
        status=status,
        evidence_json={"source": "manual", "observed_at": now.isoformat()},
        created_by_user_id=actor_id,
    )
    try:
        async with session.begin_nested():
            if previous is not None:
                before = _snapshot(previous)
                previous.status = "revoked"
                session.add(
                    IdentityCutoverJournal(
                        tenant_id=tenant_id,
                        object_kind="org_member_user_link",
                        object_id=previous.id,
                        before_json=before,
                        after_json=_snapshot(previous),
                        applied_at=now,
                    )
                )
                await session.flush()
            session.add(link)
            session.add(
                IdentityCutoverJournal(
                    tenant_id=tenant_id,
                    object_kind="org_member_user_link",
                    object_id=link.id,
                    before_json=None,
                    after_json=_snapshot(link),
                    applied_at=now,
                )
            )
            await session.flush()
            await write_audit_event(
                session,
                event_type="org.identity_link",
                actor_type="user",
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                resource_type="org_member_user_link",
                resource_id=link.id,
                details={
                    "org_member_id": str(link.org_member_id),
                    "user_id": str(link.user_id),
                    "provider_id": str(link.provider_id) if link.provider_id else None,
                    "subject_kind": link.subject_kind,
                    "status": link.status,
                    **({"previous_link_id": str(previous.id)} if previous is not None else {}),
                },
            )
    except IntegrityError:
        _reject(
            "active_link_conflict",
            "An active link already uses this directory person or subject. Reload and retry.",
            retryable=True,
        )
    return link


def _outcome_for_existing(link: OrgMemberUserLink, *, created: bool) -> IdentityLinkOutcome:
    if link.status == "active":
        message = "The link was saved." if created else "This link is already stored."
        return IdentityLinkOutcome(True, "active", "active", message, False, _public_link(link, valid=True, reason="active"))
    if link.status == "conflict":
        message = "An existing identity binding disagrees. The conflict was stored and no identity was overwritten."
    else:
        message = "The account is inactive, so the link was stored as inactive and is not active."
    return IdentityLinkOutcome(
        False,
        link.status,
        link.status,
        message,
        False,
        _public_link(link, valid=False, reason=link.status),
    )


async def _prepare_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member: OrgMember,
    user_id: uuid.UUID,
    provider_id: uuid.UUID,
    subject_kind: str,
) -> tuple[User, IdentityProvider, SubjectMap, str, list[int]]:
    if subject_kind not in SUBJECT_KINDS:
        _reject("subject_kind", "The subject kind is not one of the stored provider subjects.")
    user = await _require_user(session, tenant_id, user_id, actor_id)
    provider = await _require_provider(session, tenant_id, provider_id, actor_id)
    _reject_unproven_namespace(member, provider)
    subjects = member_subject_map(member)
    if subjects.ambiguous or subject_kind not in subjects.values:
        if subjects.ambiguous:
            _reject("ambiguous_subject", "The directory person has inconsistent provider subjects.")
        _reject("missing_provider_subject", "The directory person has no stored subject of that kind.")
    return user, provider, subjects, subjects.values[subject_kind], _lock_keys(tenant_id, member.id, provider.id, subjects)


async def _locked_write_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_id: uuid.UUID,
    user: User,
    provider: IdentityProvider,
    member: OrgMember,
) -> tuple[User, IdentityProvider]:
    """Re-read account, provider, and namespace after the advisory lock is held."""

    current_user = await _revalidate_for_update(session, select(User).where(User.id == user_id), user)
    if current_user is None or current_user.tenant_id != tenant_id:
        await _require_user(session, tenant_id, user_id, actor_id)
        raise AssertionError("unreachable")
    current_provider = await _revalidate_for_update(
        session,
        select(IdentityProvider).where(IdentityProvider.id == provider_id),
        provider,
    )
    if (
        current_provider is None
        or current_provider.tenant_id is None
        or current_provider.tenant_id != tenant_id
        or current_provider.is_active is not True
    ):
        await _require_provider(session, tenant_id, provider_id, actor_id)
        raise AssertionError("unreachable")
    _reject_unproven_namespace(member, current_provider)
    return current_user, current_provider


async def create_manual_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    org_member_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_id: uuid.UUID,
    subject_kind: str,
) -> IdentityLinkOutcome:
    member = await _peek_member(session, tenant_id, org_member_id)
    if member is None:
        await _reject_missing_member(session, tenant_id, org_member_id, actor_id)
    user, provider, subjects, match_subject, keys = await _prepare_target(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        member=member,
        user_id=user_id,
        provider_id=provider_id,
        subject_kind=subject_kind,
    )
    await _lock(session, keys)
    member = await _lock_member(session, tenant_id, org_member_id)
    if member is None:
        await _reject_missing_member(session, tenant_id, org_member_id, actor_id)
    rebound = member_subject_map(member)
    if rebound.values != subjects.values or rebound.ambiguous:
        _reject("stale_mapping", "The directory person changed before the link was saved. Reload and retry.", retryable=True)
    user, provider = await _locked_write_target(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        user_id=user_id,
        provider_id=provider_id,
        user=user,
        provider=provider,
        member=member,
    )
    active = await _active_links(
        session,
        tenant_id=tenant_id,
        provider_id=provider.id,
        subject_kind=subject_kind,
        match_subject=match_subject,
        org_member_id=member.id,
    )
    identical = next(
        (
            row
            for row in active
            if _same_row(
                row,
                org_member_id=member.id,
                user_id=user.id,
                provider_id=provider.id,
                subject_kind=subject_kind,
                match_subject=match_subject,
                status="active",
            )
        ),
        None,
    )
    if identical is not None:
        bindings = await _bindings_for_providers(session, [provider.id], actor_id=actor_id)
        valid, reason = recheck_stored_link(
            identical, member=member, user=user, provider=provider, bindings=bindings
        )
        if valid:
            return IdentityLinkOutcome(
                True,
                "active",
                "active",
                "This link is already stored.",
                False,
                _public_link(identical, valid=True, reason="active"),
            )
        return IdentityLinkOutcome(
            False,
            reason,
            reason,
            "The stored link no longer matches the current directory subject.",
            False,
            _public_link(identical, valid=False, reason=reason),
        )
    if active:
        _reject(
            "active_link_conflict",
            "An active link already uses this directory person or subject. Reload and retry.",
            retryable=True,
        )
    bindings = await _bindings_for_providers(session, [provider.id], actor_id=actor_id)
    status, external_identity_id = _decide_status(
        user=user,
        provider_id=provider.id,
        subject_kind=subject_kind,
        subjects=rebound,
        bindings=bindings,
        user_id=user.id,
    )
    existing = (
        await session.execute(
            select(OrgMemberUserLink).where(
                OrgMemberUserLink.tenant_id == tenant_id,
                OrgMemberUserLink.org_member_id == member.id,
                OrgMemberUserLink.user_id == user.id,
                OrgMemberUserLink.provider_id == provider.id,
                OrgMemberUserLink.subject_kind == subject_kind,
                OrgMemberUserLink.match_subject == match_subject,
                OrgMemberUserLink.status == status,
            )
        )
    ).scalars().first()
    if existing is not None:
        return _outcome_for_existing(existing, created=False)
    if status == "conflict" and external_identity_id is not None:
        requested = _bindings_matching(bindings, provider.id, subject_kind, match_subject)
        if not requested or requested[0].user_id != user.id:
            external_identity_id = None
    link = await _write_link(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        member=member,
        user=user,
        provider=provider,
        subject_kind=subject_kind,
        match_subject=match_subject,
        status=status,
        external_identity_id=external_identity_id,
        action="identity_link.create",
    )
    return _outcome_for_existing(link, created=True)


def _expected_matches(
    link: OrgMemberUserLink,
    *,
    expected_version: str,
    expected_user_id: uuid.UUID,
    expected_provider_id: uuid.UUID,
    expected_subject_kind: str,
    expected_status: str,
) -> bool:
    return (
        link_version(link) == expected_version
        and link.user_id == expected_user_id
        and link.provider_id == expected_provider_id
        and link.subject_kind == expected_subject_kind
        and link.status == expected_status
    )


async def _locked_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    link_id: uuid.UUID,
    expected_version: str,
    expected_user_id: uuid.UUID,
    expected_provider_id: uuid.UUID,
    expected_subject_kind: str,
    expected_status: str,
) -> OrgMemberUserLink:
    current = (
        await session.execute(
            select(OrgMemberUserLink).where(
                OrgMemberUserLink.id == link_id, OrgMemberUserLink.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if current is None:
        _reject("link_not_in_tenant", "That link is not in this company.")
    keys = [
        _advisory_key("link", current.id),
        _advisory_key("member", tenant_id, current.org_member_id),
    ]
    if current.provider_id is not None and current.subject_kind and current.match_subject:
        keys.append(
            _advisory_key("subject", tenant_id, current.provider_id, current.subject_kind, current.match_subject)
        )
    await _lock(session, keys)
    locked = await _revalidate_for_update(
        session,
        select(OrgMemberUserLink).where(
            OrgMemberUserLink.id == link_id, OrgMemberUserLink.tenant_id == tenant_id
        ),
        current,
    )
    if locked is None or not _expected_matches(
        locked,
        expected_version=expected_version,
        expected_user_id=expected_user_id,
        expected_provider_id=expected_provider_id,
        expected_subject_kind=expected_subject_kind,
        expected_status=expected_status,
    ):
        _reject("stale_mapping", "The link changed before this update. Reload and retry.", retryable=True)
    return locked


async def revoke_identity_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    link_id: uuid.UUID,
    expected_version: str,
    expected_user_id: uuid.UUID,
    expected_provider_id: uuid.UUID,
    expected_subject_kind: str,
    expected_status: str,
) -> IdentityLinkOutcome:
    link = await _locked_link(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        link_id=link_id,
        expected_version=expected_version,
        expected_user_id=expected_user_id,
        expected_provider_id=expected_provider_id,
        expected_subject_kind=expected_subject_kind,
        expected_status=expected_status,
    )
    if link.status == "revoked":
        return IdentityLinkOutcome(
            False,
            "revoked",
            "revoked",
            "This link is already revoked.",
            False,
            _public_link(link, valid=False, reason="revoked"),
        )
    before = _snapshot(link)
    link.status = "revoked"
    now = datetime.now(timezone.utc)
    try:
        async with session.begin_nested():
            session.add(
                IdentityCutoverJournal(
                    tenant_id=tenant_id,
                    object_kind="org_member_user_link",
                    object_id=link.id,
                    before_json=before,
                    after_json=_snapshot(link),
                    applied_at=now,
                )
            )
            await session.flush()
            await write_audit_event(
                session,
                event_type="org.identity_link",
                actor_type="user",
                actor_id=actor_id,
                tenant_id=tenant_id,
                action="identity_link.revoke",
                resource_type="org_member_user_link",
                resource_id=link.id,
                details={
                    "org_member_id": str(link.org_member_id),
                    "user_id": str(link.user_id),
                    "provider_id": str(link.provider_id) if link.provider_id else None,
                    "subject_kind": link.subject_kind,
                    "status": link.status,
                },
            )
    except IntegrityError:
        _reject("active_link_conflict", "The link could not be revoked. Reload and retry.", retryable=True)
    return IdentityLinkOutcome(
        True,
        "revoked",
        "revoked",
        "The link was revoked.",
        False,
        _public_link(link, valid=False, reason="revoked"),
    )


async def replace_identity_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    link_id: uuid.UUID,
    expected_version: str,
    expected_user_id: uuid.UUID,
    expected_provider_id: uuid.UUID,
    expected_subject_kind: str,
    expected_status: str,
    user_id: uuid.UUID,
    provider_id: uuid.UUID,
    subject_kind: str,
) -> IdentityLinkOutcome:
    peeked = (
        await session.execute(
            select(OrgMemberUserLink).where(
                OrgMemberUserLink.id == link_id, OrgMemberUserLink.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if peeked is None:
        _reject("link_not_in_tenant", "That link is not in this company.")
    member = await _peek_member(session, tenant_id, peeked.org_member_id)
    if member is None:
        await _reject_missing_member(session, tenant_id, peeked.org_member_id, actor_id)
    user, provider, subjects, match_subject, new_keys = await _prepare_target(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        member=member,
        user_id=user_id,
        provider_id=provider_id,
        subject_kind=subject_kind,
    )
    keys = [
        _advisory_key("link", peeked.id),
        _advisory_key("member", tenant_id, peeked.org_member_id),
        *new_keys,
    ]
    if peeked.provider_id is not None and peeked.subject_kind and peeked.match_subject:
        keys.append(
            _advisory_key("subject", tenant_id, peeked.provider_id, peeked.subject_kind, peeked.match_subject)
        )
    await _lock(session, keys)
    current = await _revalidate_for_update(
        session,
        select(OrgMemberUserLink).where(
            OrgMemberUserLink.id == link_id, OrgMemberUserLink.tenant_id == tenant_id
        ),
        peeked,
    )
    if current is None or not _expected_matches(
        current,
        expected_version=expected_version,
        expected_user_id=expected_user_id,
        expected_provider_id=expected_provider_id,
        expected_subject_kind=expected_subject_kind,
        expected_status=expected_status,
    ):
        _reject("stale_mapping", "The link changed before this update. Reload and retry.", retryable=True)
    member = await _lock_member(session, tenant_id, current.org_member_id)
    if member is None:
        await _reject_missing_member(session, tenant_id, current.org_member_id, actor_id)
    rebound = member_subject_map(member)
    if rebound.values != subjects.values or rebound.ambiguous:
        _reject("stale_mapping", "The directory person changed before the link was saved. Reload and retry.", retryable=True)
    user, provider = await _locked_write_target(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        user_id=user_id,
        provider_id=provider_id,
        user=user,
        provider=provider,
        member=member,
    )
    bindings = await _bindings_for_providers(session, [provider.id], actor_id=actor_id)
    if _same_row(
        current,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider.id,
        subject_kind=subject_kind,
        match_subject=match_subject,
        status="active",
    ):
        valid, reason = recheck_stored_link(current, member=member, user=user, provider=provider, bindings=bindings)
        if valid:
            return _outcome_for_existing(current, created=False)
        return IdentityLinkOutcome(
            False,
            reason,
            reason,
            "The stored link no longer matches the current directory subject.",
            False,
            _public_link(current, valid=False, reason=reason),
        )
    status, external_identity_id = _decide_status(
        user=user,
        provider_id=provider.id,
        subject_kind=subject_kind,
        subjects=subjects,
        bindings=bindings,
        user_id=user.id,
    )
    if status != "active":
        if status == "conflict":
            _reject("conflict", "An existing identity binding disagrees. The current link was not changed.")
        _reject("inactive_user", "The account is inactive. The current link was not changed.")
    active = await _active_links(
        session,
        tenant_id=tenant_id,
        provider_id=provider.id,
        subject_kind=subject_kind,
        match_subject=match_subject,
        org_member_id=member.id,
    )
    blockers = [row for row in active if row.id != current.id]
    if blockers:
        _reject(
            "active_link_conflict",
            "An active link already uses this directory person or subject. Reload and retry.",
            retryable=True,
        )
    previous = None if current.status == "revoked" else current
    link = await _write_link(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        member=member,
        user=user,
        provider=provider,
        subject_kind=subject_kind,
        match_subject=match_subject,
        status="active",
        external_identity_id=external_identity_id,
        action="identity_link.replace",
        previous=previous,
    )
    return _outcome_for_existing(link, created=True)


async def list_identity_links(session: AsyncSession, tenant_id: uuid.UUID, *, actor_id: uuid.UUID) -> dict[str, Any]:
    members = list(
        (
            await session.execute(
                select(OrgMember).where(OrgMember.tenant_id == tenant_id).order_by(OrgMember.name, OrgMember.id)
            )
        ).scalars().all()
    )
    users = list((await session.execute(select(User).where(User.tenant_id == tenant_id))).scalars().all())
    providers = list(
        (
            await session.execute(
                select(IdentityProvider).where(IdentityProvider.tenant_id == tenant_id).order_by(IdentityProvider.name)
            )
        ).scalars().all()
    )
    member_ids = [member.id for member in members]
    links = []
    if member_ids:
        links = list(
            (
                await session.execute(
                    select(OrgMemberUserLink).where(
                        OrgMemberUserLink.tenant_id == tenant_id,
                        OrgMemberUserLink.org_member_id.in_(member_ids),
                    )
                )
            ).scalars().all()
        )
    users_by_id = {user.id: user for user in users}
    providers_by_id = {provider.id: provider for provider in providers}
    provider_ids = sorted({link.provider_id for link in links if link.provider_id is not None})
    bindings = await _bindings_for_providers(session, provider_ids, actor_id=actor_id)
    rows = []
    for member in members:
        subjects = member_subject_map(member)
        member_links = [link for link in links if link.org_member_id == member.id]
        public = []
        checked: list[tuple[bool, str, uuid.UUID]] = []
        for link in member_links:
            valid, reason = recheck_stored_link(
                link,
                member=member,
                user=users_by_id.get(link.user_id),
                provider=providers_by_id.get(link.provider_id) if link.provider_id else None,
                bindings=bindings,
            )
            checked.append((valid, reason, link.user_id))
            public.append(_public_link(link, valid=valid, reason=reason))
        valid, reason = _member_reason(subjects, checked)
        rows.append(
            {
                "org_member_id": str(member.id),
                "label": member.name,
                "subject_kinds": [] if subjects.ambiguous else sorted(subjects.values),
                "reason": reason,
                "valid": valid,
                "links": public,
            }
        )
    return {
        "members": rows,
        "accounts": [
            {
                "user_id": str(user.id),
                "label": user.display_name or user.username,
                "is_active": user.is_active is True,
            }
            for user in sorted(users, key=lambda item: ((item.display_name or item.username), str(item.id)))
        ],
        "providers": [
            {
                "id": str(provider.id),
                "provider_type": provider.provider_type,
                "name": provider.name,
                "is_active": provider.is_active is True,
            }
            for provider in providers
        ],
    }


async def persistent_grant_audiences(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    users: list[Any],
    members: list[Any],
) -> dict[str, list[dict[str, str]]]:
    member_ids = [member.id for member in members]
    links: list[OrgMemberUserLink] = []
    if member_ids:
        links = list(
            (
                await session.execute(
                    select(OrgMemberUserLink).where(
                        OrgMemberUserLink.tenant_id == tenant_id,
                        OrgMemberUserLink.org_member_id.in_(member_ids),
                    )
                )
            ).scalars().all()
        )
    if not links:
        return company_knowledge_grant_audiences(users, members)
    users_by_id = {user.id: user for user in users}
    provider_ids = sorted({link.provider_id for link in links if link.status == "active" and link.provider_id})
    providers = []
    if provider_ids:
        providers = list(
            (
                await session.execute(select(IdentityProvider).where(IdentityProvider.id.in_(provider_ids)))
            ).scalars().all()
        )
    providers_by_id = {provider.id: provider for provider in providers}
    bindings = await _bindings_for_providers(session, provider_ids, actor_id=None)
    members_by_id = {member.id: member for member in members}

    def recheck(link: OrgMemberUserLink, member: Any) -> tuple[bool, str]:
        return recheck_stored_link(
            link,
            member=members_by_id.get(link.org_member_id, member),
            user=users_by_id.get(link.user_id),
            provider=providers_by_id.get(link.provider_id) if link.provider_id else None,
            bindings=bindings,
        )

    return audiences_with_persistent_links(users, members, links, recheck)
