"""Administrator backfill, department mapping, user cutover, and compare-and-restore.

A stored directory person becomes linked only when the current matcher still
calls that person trusted. A department link is an explicit choice or an
approved create, never a name pairing. User.department_id moves only onto the
mapped department when it is empty or already there. An explicit repair that
clears a stored directory observation journals that fact with the department
change. Rollback writes a new journal row only when the current department
still equals the selected after state, restores an observation only while it
still equals that after state, and leaves a newer observation in place.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import OrgDepartment, OrgMember
from app.models.organization_identity import DepartmentDirectoryLink, IdentityCutoverJournal, OrgMemberUserLink
from app.models.user import Department, User
from app.services.identity_dry_run import _directory_observation, _foreign_blocks, build_identity_dry_run_report
from app.services.identity_links import (
    IdentityLinkRejected,
    _advisory_key,
    _lock,
    _record_identity_audit,
    _revalidate_for_update,
    _snapshot,
    _with_journal_ids,
    apply_observed_subject_link,
    begin_deferred_identity_audit,
    commit_deferred_identity_audit,
    discard_deferred_identity_audit,
    identity_audit_mark,
    lock_directory_sync,
    member_subject_map,
    recheck_stored_link,
    rewind_deferred_identity_audit,
)
from app.services.identity_links import _bindings_for_providers

_EMPTY_SNAPSHOT = {"complete": True, "departments": [], "members": []}
_LINK_SAVED = "The link was saved."
_LINK_STORED = "This link is already stored."


class _RowBlocked(Exception):
    def __init__(self, outcome: dict[str, Any]) -> None:
        super().__init__(outcome.get("reason") or "conflict")
        self.outcome = outcome


def _blank_counts() -> dict[str, dict[str, int]]:
    return {
        "link_backfill": {"applied": 0, "noop": 0, "skipped": 0, "conflict": 0, "unavailable": 0},
        "department_mappings": {"created": 0, "mapped": 0, "revoked": 0, "noop": 0, "skipped": 0, "conflict": 0, "unavailable": 0},
        "user_assignments": {"changed": 0, "noop": 0, "skipped": 0, "conflict": 0, "unavailable": 0},
    }


def _result(schema: str, tenant_id: uuid.UUID, counts: dict, rows: list[dict]) -> dict[str, Any]:
    mutated = any(
        counts[group][key]
        for group, keys in (
            ("link_backfill", ("applied",)),
            ("department_mappings", ("created", "mapped", "revoked")),
            ("user_assignments", ("changed",)),
        )
        for key in keys
    )
    return {
        "schema": schema,
        "tenant_id": str(tenant_id),
        "status": "complete",
        "applied": mutated,
        "counts": counts,
        "rows": rows,
    }


def _audit_open(session: AsyncSession) -> bool:
    return isinstance(session.info.get("identity_audit_gate"), list)


class _AuditScope:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.owned = False
        self.committed = False

    async def __aenter__(self) -> _AuditScope:
        self.owned = not _audit_open(self.session)
        if self.owned:
            begin_deferred_identity_audit(self.session)
        return self

    async def commit(self) -> None:
        if self.owned and not self.committed:
            await commit_deferred_identity_audit(self.session)
            self.committed = True

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.owned and not self.committed:
            discard_deferred_identity_audit(self.session)


def _department_snapshot(link: DepartmentDirectoryLink) -> dict[str, Any]:
    return {
        "org_department_id": str(link.org_department_id),
        "department_id": str(link.department_id) if link.department_id else None,
        "provider": link.provider,
        "provider_id": str(link.provider_id) if link.provider_id else None,
        "provider_department_id": link.provider_department_id,
        "status": link.status,
    }


def _user_snapshot(user: User, *, link_id: uuid.UUID | None, member_id: uuid.UUID | None) -> dict[str, Any]:
    return {
        "department_id": str(user.department_id) if user.department_id else None,
        "department_directory_link_id": str(link_id) if link_id else None,
        "org_member_id": str(member_id) if member_id else None,
    }


async def _journal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    object_kind: str,
    object_id: uuid.UUID,
    before: dict | None,
    after: dict,
    action: str,
    actor_id: uuid.UUID | None,
    details: dict,
) -> IdentityCutoverJournal:
    now = datetime.now(timezone.utc)
    row = IdentityCutoverJournal(
        tenant_id=tenant_id,
        object_kind=object_kind,
        object_id=object_id,
        before_json=before,
        after_json=after,
        applied_at=now,
    )
    session.add(row)
    await session.flush()
    await _record_identity_audit(
        session,
        event_type="org.identity_cutover",
        actor_type="user",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action=action,
        resource_type=object_kind,
        resource_id=object_id,
        details=_with_journal_ids(details, [row]),
    )
    return row


async def _stored_report(session: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID | None) -> dict:
    return await build_identity_dry_run_report(
        session,
        tenant_id=tenant_id,
        snapshot=_EMPTY_SNAPSHOT,
        actor_id=actor_id,
        source_revision="stored_backfill",
    )


def _trusted_rows(report: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in report["rows"]:
        if row.get("object_kind") == "org_member" and row.get("disposition") == "trusted":
            origin = (row.get("provenance") or {}).get("origin")
            if origin == "stored" and row.get("user_id"):
                found[str(row["object_id"])] = row
    return found


async def preview_stored_backfill(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> dict[str, Any]:
    """Recompute which stored people are uniquely trusted. This writes nothing."""

    report = await _stored_report(session, tenant_id, actor_id)
    trusted = _trusted_rows(report)
    rows = []
    for row in report["rows"]:
        if row.get("object_kind") != "org_member":
            continue
        if (row.get("provenance") or {}).get("origin") != "stored":
            continue
        if row.get("disposition") == "department_conflict":
            continue
        object_id = str(row["object_id"])
        selectable = object_id in trusted and row.get("disposition") == "trusted"
        rows.append(
            {
                "org_member_id": object_id,
                "disposition": row["disposition"],
                "reason": row["reason"],
                "user_id": row.get("user_id"),
                "subject_kinds": list((row.get("provenance") or {}).get("subject_kinds") or []),
                "selectable": selectable,
            }
        )
    counts = {key: sum(1 for item in rows if item["disposition"] == key) for key in (
        "trusted", "conflict", "unregistered", "inactive_user", "cross_tenant", "email_only_candidate"
    )}
    return {
        "schema": "hive.identity_backfill_preview.v1",
        "tenant_id": str(tenant_id),
        "applied": False,
        "counts": counts,
        "rows": rows,
    }


async def _member_in_tenant(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> OrgMember | None:
    return (
        await session.execute(
            select(OrgMember).where(OrgMember.id == member_id, OrgMember.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def _proven_provider(session: AsyncSession, tenant_id: uuid.UUID):
    from app.models.identity import IdentityProvider

    providers = (
        await session.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id == tenant_id,
                IdentityProvider.is_active.is_(True),
            )
        )
    ).scalars().all()
    if len(providers) != 1:
        return None
    return providers[0]


async def apply_stored_backfill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    org_member_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """Link the selected stored people when they are still uniquely trusted."""

    await lock_directory_sync(session, tenant_id)
    counts = _blank_counts()
    rows: list[dict] = []
    async with _AuditScope(session) as audit:
        report = await _stored_report(session, tenant_id, actor_id)
        trusted = _trusted_rows(report)
        provider = await _proven_provider(session, tenant_id)
        for member_id in sorted(set(org_member_ids), key=str):
            rows.append(
                await _backfill_member(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    member_id=member_id,
                    trusted=trusted.get(str(member_id)),
                    provider=provider,
                    report=report,
                    counts=counts,
                )
            )
        await audit.commit()
    return _result("hive.identity_backfill.v1", tenant_id, counts, rows)


async def _backfill_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member_id: uuid.UUID,
    trusted: dict | None,
    provider,
    report: dict,
    counts: dict,
) -> dict[str, Any]:
    member = await _member_in_tenant(session, tenant_id, member_id)
    base = {"object_kind": "org_member", "object_id": str(member_id), "user_id": None, "links": []}
    if member is None:
        counts["link_backfill"]["unavailable"] += 1
        return {**base, "outcome": "unavailable", "reason": "member_not_in_tenant"}
    if trusted is None or provider is None:
        bucket, reason = ("conflict", "provider_unproven") if provider is None else _current_block(report, member_id)
        counts["link_backfill"][bucket] += 1
        current = _stored_member_fact(report, member_id)
        user_id = None if current is None else current.get("user_id")
        return {**base, "user_id": user_id, "outcome": bucket, "reason": reason}
    user = await session.get(User, uuid.UUID(str(trusted["user_id"])))
    if user is None or user.tenant_id != tenant_id:
        counts["link_backfill"]["unavailable"] += 1
        return {**base, "outcome": "unavailable", "reason": "cross_tenant"}
    subjects = member_subject_map(member)
    if subjects.ambiguous or not subjects.values:
        counts["link_backfill"]["conflict"] += 1
        reason = "ambiguous_subject" if subjects.ambiguous else "missing_provider_subject"
        return {**base, "outcome": "conflict", "reason": reason, "user_id": str(user.id)}
    kinds = [kind for kind in ("provider_open_id", "provider_user_id", "provider_union_id") if kind in subjects.values]
    audit_mark = identity_audit_mark(session)
    try:
        async with session.begin_nested():
            written = []
            for kind in kinds:
                outcome = await apply_observed_subject_link(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    member=member,
                    user=user,
                    provider=provider,
                    subject_kind=kind,
                    source="dry_run",
                )
                if not outcome.applied or outcome.code != "active":
                    raise _RowBlocked(
                        {
                            "outcome": "conflict" if outcome.code in {"conflict", "revoked"} else "skipped",
                            "reason": outcome.reason or outcome.code,
                        }
                    )
                state = "applied" if outcome.message == _LINK_SAVED else "noop"
                if outcome.message not in {_LINK_SAVED, _LINK_STORED}:
                    state = "applied" if outcome.message != _LINK_STORED else "noop"
                written.append({"subject_kind": kind, "outcome": state, "link_id": (outcome.link or {}).get("id")})
    except _RowBlocked as blocked:
        rewind_deferred_identity_audit(session, audit_mark)
        bucket = "conflict" if blocked.outcome["outcome"] == "conflict" else "skipped"
        counts["link_backfill"][bucket] += 1
        return {**base, "user_id": str(user.id), **blocked.outcome}
    except (IdentityLinkRejected, IntegrityError) as exc:
        rewind_deferred_identity_audit(session, audit_mark)
        counts["link_backfill"]["conflict"] += 1
        return {**base, "user_id": str(user.id), "outcome": "conflict", "reason": getattr(exc, "code", "conflict")}
    for item in written:
        counts["link_backfill"]["applied" if item["outcome"] == "applied" else "noop"] += 1
    member_outcome = "applied" if any(item["outcome"] == "applied" for item in written) else "noop"
    return {
        **base,
        "user_id": str(user.id),
        "outcome": member_outcome,
        "reason": "active",
        "links": written,
    }


def _stored_member_fact(report: dict, member_id: uuid.UUID) -> dict | None:
    rows = [
        row
        for row in report["rows"]
        if row.get("object_kind") == "org_member"
        and str(row.get("object_id")) == str(member_id)
        and row.get("disposition") != "department_conflict"
        and (row.get("provenance") or {}).get("origin") in {"stored", "stored_and_incoming"}
    ]
    if not rows:
        return None
    return next((row for row in rows if row.get("disposition") != "trusted"), rows[0])


def _current_block(report: dict, member_id: uuid.UUID) -> tuple[str, str]:
    current = _stored_member_fact(report, member_id)
    if current is None:
        return "skipped", "stale_preview"
    disposition = str(current.get("disposition") or "")
    reason = str(current.get("reason") or "stale_preview")
    if disposition == "conflict":
        return "conflict", reason
    if disposition == "cross_tenant":
        return "unavailable", reason
    return "skipped", reason


async def _directory_department_identity(session, tenant_id, org, *, for_effect=False) -> dict:
    """Resolve the stored mirror's namespace; directory names never prove identity."""
    from app.models.identity import IdentityProvider
    from app.models.tenant_channel_config import TenantChannelConfig

    provider_id = getattr(org, "provider_id", None)
    remote_id = getattr(org, "provider_department_id", None)
    identity = {"provider": "feishu", "provider_id": None,
        "provider_department_id": (getattr(org, "feishu_id", None) or "").strip(), "reason": None}
    if org is None or org.tenant_id != tenant_id:
        identity["reason"] = "org_department_not_in_tenant"
    elif provider_id is None and remote_id is None:
        if not identity["provider_department_id"]:
            identity["reason"] = "missing_provider_department"
    else:
        identity.update(provider=None, provider_id=str(provider_id) if provider_id else None,
            provider_department_id=remote_id)
        provider = await session.scalar(select(IdentityProvider).where(IdentityProvider.id == provider_id,
            IdentityProvider.tenant_id == tenant_id)) if provider_id else None
        if provider is None or provider.provider_type not in {"dingtalk", "wecom"} or org.feishu_id or not remote_id:
            identity["reason"] = "provider_namespace"
            return identity
        identity["provider"] = provider.provider_type
        if for_effect:
            from app.api.tenant_channels import _lock_config
            await _lock_config(session, tenant_id, provider.provider_type)
            provider = await session.scalar(select(IdentityProvider).where(IdentityProvider.id == provider_id,
                IdentityProvider.tenant_id == tenant_id).with_for_update().execution_options(populate_existing=True))
            if provider is None or provider.provider_type != identity["provider"]:
                identity["reason"] = "provider_namespace"
                return identity
        config = await session.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == tenant_id,
            TenantChannelConfig.channel_type == provider.provider_type).execution_options(populate_existing=True))
        if not provider.is_active or config is None or not config.is_configured:
            identity["reason"] = "directory_connection_inactive"
        elif provider.config != {"connection_id": str(config.id), "app_id": config.app_id}:
            identity["reason"] = "directory_namespace_changed"
    return identity


def _department_link_matches(link, identity):
    return all(getattr(link, field) == (uuid.UUID(identity[field]) if field == "provider_id" and identity[field]
        else identity[field]) for field in ("provider", "provider_id", "provider_department_id"))


async def _department_link_reason(session, tenant_id, link, org=None):
    if org is None:
        org = await session.scalar(select(OrgDepartment).where(OrgDepartment.id == link.org_department_id,
            OrgDepartment.tenant_id == tenant_id))
    identity = await _directory_department_identity(session, tenant_id, org, for_effect=True)
    return identity["reason"] or (None if _department_link_matches(link, identity) else "provider_namespace")


async def list_department_directory(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> dict[str, Any]:
    """Show stored directory departments, explicit choices, and unapplied proposals."""

    org_departments = list(
        (
            await session.execute(
                select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id).order_by(OrgDepartment.name)
            )
        ).scalars().all()
    )
    departments = list(
        (
            await session.execute(select(Department).where(Department.tenant_id == tenant_id).order_by(Department.name))
        ).scalars().all()
    )
    links = list(
        (
            await session.execute(
                select(DepartmentDirectoryLink).where(DepartmentDirectoryLink.tenant_id == tenant_id)
            )
        ).scalars().all()
    )
    names = {department.name for department in departments}
    active = [link for link in links if link.status == "active"]
    by_org = {link.org_department_id: link for link in active}
    rows = []
    for org in org_departments:
        link = by_org.get(org.id)
        identity = await _directory_department_identity(session, tenant_id, org)
        link_reason = identity["reason"] or ("provider_namespace" if link and not _department_link_matches(link, identity) else None)
        parent_ready, parent_reason, _parent_department = await _parent_expectation(session, tenant_id, org, org_departments, by_org)
        proposal = None
        if link is None:
            if not (org.name or "").strip():
                proposal = {"action": "create", "selectable": False, "reason": "missing_department_name"}
            elif identity["reason"]:
                proposal = {"action": "create", "selectable": False, "reason": identity["reason"]}
            elif org.name in names:
                proposal = {"action": "create", "selectable": False, "reason": "name_collision"}
            elif not parent_ready:
                proposal = {"action": "create", "selectable": False, "reason": parent_reason}
            else:
                proposal = {"action": "create", "selectable": True, "reason": "create"}
        rows.append(
            {
                "org_department_id": str(org.id),
                "name": org.name,
                "provider": identity["provider"],
                "provider_id": identity["provider_id"],
                "provider_department_id": identity["provider_department_id"],
                "reason": link_reason,
                "parent_org_department_id": str(org.parent_id) if org.parent_id else None,
                "link": None
                if link is None
                else {
                    "id": str(link.id),
                    "department_id": str(link.department_id) if link.department_id else None,
                    "status": link.status,
                    "valid": link_reason is None,
                    "provider_department_id": link.provider_department_id,
                },
                "proposal": proposal,
                "same_name_department_ids": [str(item.id) for item in departments if item.name == org.name],
            }
        )
    return {
        "schema": "hive.department_directory.v1",
        "tenant_id": str(tenant_id),
        "applied": False,
        "org_departments": rows,
        "departments": [
            {"id": str(item.id), "name": item.name, "parent_id": str(item.parent_id) if item.parent_id else None}
            for item in departments
        ],
        "links": [
            {
                "id": str(link.id),
                "org_department_id": str(link.org_department_id),
                "department_id": str(link.department_id) if link.department_id else None,
                "status": link.status,
                "provider_department_id": link.provider_department_id,
            }
            for link in links
        ],
    }


async def _parent_expectation(session, tenant_id, org, org_departments, active_by_org, *, for_effect=False):
    if org is None:
        return False, "missing_parent", None
    if org.parent_id is None:
        return True, "root", None
    parent = next((item for item in org_departments if item.id == org.parent_id), None)
    if parent is None:
        return False, "missing_parent", None
    parent_link = active_by_org.get(parent.id)
    if parent_link is None or parent_link.department_id is None:
        return False, "missing_parent", None
    identity = await _directory_department_identity(session, tenant_id, parent, for_effect=for_effect)
    child = await _directory_department_identity(session, tenant_id, org, for_effect=for_effect)
    if identity["reason"] or child["reason"]:
        return False, identity["reason"] or child["reason"], None
    if (identity["provider"], identity["provider_id"]) != (child["provider"], child["provider_id"]) or not _department_link_matches(parent_link, identity):
        return False, "provider_namespace", None
    return True, "parent", parent_link.department_id


def _walk_parents(departments_by_id: dict[uuid.UUID, Department], start: uuid.UUID | None) -> str | None:
    seen: set[uuid.UUID] = set()
    current = start
    while current is not None:
        if current in seen:
            return "cycle"
        seen.add(current)
        row = departments_by_id.get(current)
        if row is None:
            return "missing_parent"
        current = row.parent_id
    return None


async def apply_department_selections(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create, link, repair, or revoke department mappings from explicit selections."""

    await lock_directory_sync(session, tenant_id)
    counts = _blank_counts()
    rows: list[dict] = []
    async with _AuditScope(session) as audit:
        keys = []
        for selection in selections:
            org_id = selection.get("org_department_id")
            department_id = selection.get("department_id")
            link_id = selection.get("link_id")
            if org_id is not None:
                keys.append(_advisory_key("org-department", tenant_id, org_id))
            if department_id is not None:
                keys.append(_advisory_key("department", tenant_id, department_id))
            if link_id is not None:
                keys.append(_advisory_key("department-link", tenant_id, link_id))
        await _lock(session, keys)
        for selection in sorted(selections, key=lambda item: str(item.get("org_department_id") or item.get("link_id"))):
            rows.append(
                await _apply_department_selection(
                    session, tenant_id=tenant_id, actor_id=actor_id, selection=selection, counts=counts
                )
            )
        await audit.commit()
    return _result("hive.department_directory_apply.v1", tenant_id, counts, rows)


async def _apply_department_selection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    selection: dict[str, Any],
    counts: dict,
) -> dict[str, Any]:
    action = selection["action"]
    audit_mark = identity_audit_mark(session)
    try:
        async with session.begin_nested():
            if action == "revoke":
                return await _revoke_department_link(
                    session, tenant_id=tenant_id, actor_id=actor_id, link_id=selection["link_id"], counts=counts
                )
            if action == "repair":
                return await _repair_department_link(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    link_id=selection["link_id"],
                    department_id=selection["department_id"],
                    counts=counts,
                )
            return await _link_or_create_department(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                org_department_id=selection["org_department_id"],
                action=action,
                department_id=selection.get("department_id"),
                counts=counts,
            )
    except _RowBlocked as blocked:
        rewind_deferred_identity_audit(session, audit_mark)
        bucket = blocked.outcome["outcome"]
        if bucket not in counts["department_mappings"]:
            bucket = "conflict"
        counts["department_mappings"][bucket] += 1
        return blocked.outcome
    except IntegrityError:
        rewind_deferred_identity_audit(session, audit_mark)
        counts["department_mappings"]["conflict"] += 1
        return {
            "object_kind": "org_department",
            "object_id": str(selection.get("org_department_id") or selection.get("link_id")),
            "outcome": "conflict",
            "reason": "already_mapped",
        }


def _blocked(outcome: str, reason: str, **fields) -> None:
    raise _RowBlocked({"object_kind": "org_department", "outcome": outcome, "reason": reason, **fields})


async def _active_department_links(session: AsyncSession, tenant_id: uuid.UUID) -> list[DepartmentDirectoryLink]:
    return list(
        (
            await session.execute(
                select(DepartmentDirectoryLink).where(
                    DepartmentDirectoryLink.tenant_id == tenant_id,
                    DepartmentDirectoryLink.status == "active",
                )
            )
        ).scalars().all()
    )


async def _link_or_create_department(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    org_department_id: uuid.UUID,
    action: str,
    department_id: uuid.UUID | None,
    counts: dict,
) -> dict[str, Any]:
    org = (
        await session.execute(
            select(OrgDepartment)
            .where(OrgDepartment.id == org_department_id, OrgDepartment.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if org is None:
        _blocked("unavailable", "org_department_not_in_tenant", object_id=str(org_department_id))
    identity = await _directory_department_identity(session, tenant_id, org, for_effect=True)
    if identity["reason"]:
        _blocked("conflict", identity["reason"], object_id=str(org.id))
    provider_department_id = identity["provider_department_id"]
    blocked = await _foreign_blocks(
        session,
        tenant_id,
        actor_id,
        {
            "open_ids": [],
            "dept_ids": [provider_department_id],
            "emails": [],
            "ext_open": [],
            "ext_user": [],
            "ext_union": [],
            "provider_id": None,
        },
    ) if identity["provider"] == "feishu" else set()
    if ("dept", provider_department_id) in blocked:
        _blocked("conflict", "global_unique_blocked", object_id=str(org.id))
    org_departments = list(
        (await session.execute(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id))).scalars().all()
    )
    departments = list(
        (await session.execute(select(Department).where(Department.tenant_id == tenant_id))).scalars().all()
    )
    departments_by_id = {item.id: item for item in departments}
    active = await _active_department_links(session, tenant_id)
    by_org = {link.org_department_id: link for link in active}
    if org.id in by_org:
        _blocked("conflict", "already_mapped", object_id=str(org.id), link_id=str(by_org[org.id].id))
    if any(link.provider == identity["provider"] and link.provider_department_id == provider_department_id for link in active):
        _blocked("conflict", "provider_department_collision", object_id=str(org.id))
    ready, parent_reason, expected_parent = await _parent_expectation(session, tenant_id, org, org_departments, by_org, for_effect=True)
    if not ready:
        _blocked("conflict", parent_reason, object_id=str(org.id))
    cycle = _walk_parents(departments_by_id, expected_parent)
    if cycle is not None:
        _blocked("conflict", cycle, object_id=str(org.id))
    created = False
    if action == "create":
        if department_id is not None:
            _blocked("conflict", "create_rejects_supplied_department", object_id=str(org.id))
        name = (org.name or "").strip()
        if not name:
            _blocked("conflict", "missing_department_name", object_id=str(org.id))
        if any(item.name == name for item in departments):
            _blocked("conflict", "name_collision", object_id=str(org.id))
        department = Department(tenant_id=tenant_id, name=name, parent_id=expected_parent, sort_order=0)
        session.add(department)
        await session.flush()
        created = True
    else:
        if department_id is None:
            _blocked("conflict", "missing_department", object_id=str(org.id))
        department = departments_by_id.get(department_id)
        if department is None or department.tenant_id != tenant_id:
            _blocked("unavailable", "department_not_in_tenant", object_id=str(org.id))
        if any(link.provider == identity["provider"] and link.department_id == department.id for link in active):
            _blocked("conflict", "already_mapped", object_id=str(org.id))
        if department.parent_id != expected_parent:
            _blocked("conflict", "parent_conflict", object_id=str(org.id))
        if _walk_parents(departments_by_id, department.id) == "cycle":
            _blocked("conflict", "cycle", object_id=str(org.id))
    link = DepartmentDirectoryLink(
        tenant_id=tenant_id,
        org_department_id=org.id,
        department_id=department.id,
        provider=identity["provider"],
        provider_id=uuid.UUID(identity["provider_id"]) if identity["provider_id"] else None,
        provider_department_id=provider_department_id,
        status="active",
    )
    session.add(link)
    await session.flush()
    after = _department_snapshot(link)
    await _journal(
        session,
        tenant_id=tenant_id,
        object_kind="department_directory_link",
        object_id=link.id,
        before=None,
        after=after,
        action="department_directory_link.create",
        actor_id=actor_id,
        details={
            "org_department_id": str(org.id),
            "department_id": str(department.id),
            "provider_department_id": provider_department_id,
            "status": "active",
            "created_department": created,
        },
    )
    counts["department_mappings"]["created" if created else "mapped"] += 1
    return {
        "object_kind": "org_department",
        "object_id": str(org.id),
        "outcome": "created" if created else "mapped",
        "reason": "create" if created else "link",
        "link_id": str(link.id),
        "department_id": str(department.id),
        "department_name": department.name,
        "provider_department_id": provider_department_id,
    }


async def _fresh_locked(session: AsyncSession, model, identity: uuid.UUID, statement):
    """Re-read a locked row over the identity-map copy, as identity links do."""

    return await _revalidate_for_update(session, statement, await session.get(model, identity))


async def _department_link(
    session: AsyncSession, tenant_id: uuid.UUID, link_id: uuid.UUID
) -> DepartmentDirectoryLink | None:
    return await _fresh_locked(
        session,
        DepartmentDirectoryLink,
        link_id,
        select(DepartmentDirectoryLink).where(
            DepartmentDirectoryLink.id == link_id,
            DepartmentDirectoryLink.tenant_id == tenant_id,
        ),
    )


async def _repair_department_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    link_id: uuid.UUID,
    department_id: uuid.UUID,
    counts: dict,
) -> dict[str, Any]:
    link = await _department_link(session, tenant_id, link_id)
    if link is None:
        _blocked("unavailable", "link_not_in_tenant", object_id=str(link_id))
    if link.status != "active":
        _blocked("conflict", link.status, object_id=str(link.org_department_id), link_id=str(link.id))
    reason = await _department_link_reason(session, tenant_id, link)
    if reason:
        _blocked("conflict", reason, object_id=str(link.org_department_id), link_id=str(link.id))
    if link.department_id == department_id:
        counts["department_mappings"]["noop"] += 1
        return {
            "object_kind": "org_department",
            "object_id": str(link.org_department_id),
            "outcome": "noop",
            "reason": "already_mapped",
            "link_id": str(link.id),
            "department_id": str(department_id),
        }
    department = await _fresh_locked(
        session,
        Department,
        department_id,
        select(Department).where(Department.id == department_id, Department.tenant_id == tenant_id),
    )
    if department is None:
        _blocked("unavailable", "department_not_in_tenant", object_id=str(link.org_department_id))
    active = await _active_department_links(session, tenant_id)
    if any(item.provider == link.provider and item.department_id == department.id and item.id != link.id for item in active):
        _blocked("conflict", "already_mapped", object_id=str(link.org_department_id))
    org = (
        await session.execute(
            select(OrgDepartment).where(OrgDepartment.id == link.org_department_id, OrgDepartment.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    org_departments = list(
        (await session.execute(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id))).scalars().all()
    )
    departments = list(
        (await session.execute(select(Department).where(Department.tenant_id == tenant_id))).scalars().all()
    )
    by_org = {item.org_department_id: item for item in active if item.id != link.id}
    ready, parent_reason, expected_parent = await _parent_expectation(session, tenant_id, org, org_departments, by_org, for_effect=True)
    if org is None or not ready:
        _blocked("conflict", parent_reason if org is not None else "missing_parent", object_id=str(link.org_department_id))
    if department.parent_id != expected_parent:
        _blocked("conflict", "parent_conflict", object_id=str(link.org_department_id))
    if _walk_parents({item.id: item for item in departments}, department.id) == "cycle":
        _blocked("conflict", "cycle", object_id=str(link.org_department_id))
    before = _department_snapshot(link)
    link.department_id = department.id
    await session.flush()
    after = _department_snapshot(link)
    await _journal(
        session,
        tenant_id=tenant_id,
        object_kind="department_directory_link",
        object_id=link.id,
        before=before,
        after=after,
        action="department_directory_link.repair",
        actor_id=actor_id,
        details={"org_department_id": str(link.org_department_id), "department_id": str(department.id), "status": "active"},
    )
    counts["department_mappings"]["mapped"] += 1
    return {
        "object_kind": "org_department",
        "object_id": str(link.org_department_id),
        "outcome": "mapped",
        "reason": "repair",
        "link_id": str(link.id),
        "department_id": str(department.id),
        "department_name": department.name,
    }


async def _revoke_department_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    link_id: uuid.UUID,
    counts: dict,
) -> dict[str, Any]:
    link = await _department_link(session, tenant_id, link_id)
    if link is None:
        _blocked("unavailable", "link_not_in_tenant", object_id=str(link_id))
    if link.status == "revoked":
        counts["department_mappings"]["noop"] += 1
        return {
            "object_kind": "org_department",
            "object_id": str(link.org_department_id),
            "outcome": "noop",
            "reason": "revoked",
            "link_id": str(link.id),
        }
    before = _department_snapshot(link)
    link.status = "revoked"
    await session.flush()
    after = _department_snapshot(link)
    await _journal(
        session,
        tenant_id=tenant_id,
        object_kind="department_directory_link",
        object_id=link.id,
        before=before,
        after=after,
        action="department_directory_link.revoke",
        actor_id=actor_id,
        details={"org_department_id": str(link.org_department_id), "status": "revoked"},
    )
    counts["department_mappings"]["revoked"] += 1
    return {
        "object_kind": "org_department",
        "object_id": str(link.org_department_id),
        "outcome": "revoked",
        "reason": "revoked",
        "link_id": str(link.id),
    }


async def apply_user_department_cutover(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    org_member_ids: list[uuid.UUID] | None,
    withheld_members: dict[uuid.UUID, str | None] | None = None,
) -> dict[str, Any]:
    """Move linked users onto the mapped department when that is safe."""

    await lock_directory_sync(session, tenant_id)
    counts = _blank_counts()
    rows: list[dict] = []
    withheld = withheld_members or {}
    async with _AuditScope(session) as audit:
        if org_member_ids is None:
            member_ids = list(
                (
                    await session.execute(
                        select(OrgMemberUserLink.org_member_id).where(
                            OrgMemberUserLink.tenant_id == tenant_id,
                            OrgMemberUserLink.status == "active",
                        )
                    )
                ).scalars().all()
            )
        else:
            member_ids = list(org_member_ids)
        plans: list[dict] = []
        reported: set[uuid.UUID] = set()
        for member_id in sorted(set(member_ids), key=str):
            member = await _member_in_tenant(session, tenant_id, member_id)
            observation = _directory_observation(member)
            unambiguous = observation is not None and len(observation) == 1
            ambiguous = observation is not None and len(observation) > 1
            # A length-1 observation is a real replacement, so a withheld map
            # built before that write must not keep the old ambiguity.
            if ambiguous or (member_id in withheld and not unambiguous):
                reported.add(member_id)
                user_id = withheld.get(member_id)
                if user_id is None:
                    linked = await _member_cutover_plan(session, tenant_id, member_id)
                    user_id = linked.get("user_id")
                rows.append(
                    await _withheld_department_conflict(
                        session,
                        tenant_id=tenant_id,
                        member_id=member_id,
                        user_id=str(user_id) if user_id else None,
                        counts=counts,
                    )
                )
                continue
            plan = await _member_cutover_plan(session, tenant_id, member_id)
            if plan.get("outcome"):
                counts["user_assignments"][plan["outcome"]] += 1
                rows.append(plan)
            else:
                plans.append(plan)
        by_user: dict[uuid.UUID, list[dict]] = defaultdict(list)
        for plan in plans:
            by_user[plan["user_id"]].append(plan)
        for user_id in sorted(by_user, key=str):
            rows.extend(
                await _assign_user_department(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    user_id=user_id,
                    plans=by_user[user_id],
                    counts=counts,
                )
            )
        for member_id in sorted(set(withheld) - reported, key=str):
            member = await _member_in_tenant(session, tenant_id, member_id)
            observation = _directory_observation(member)
            if observation is not None and len(observation) == 1:
                continue
            rows.append(
                await _withheld_department_conflict(
                    session,
                    tenant_id=tenant_id,
                    member_id=member_id,
                    user_id=withheld[member_id],
                    counts=counts,
                )
            )
        await audit.commit()
    return _result("hive.department_cutover.v1", tenant_id, counts, rows)


async def _member_cutover_plan(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> dict:
    member = await _member_in_tenant(session, tenant_id, member_id)
    if member is None:
        return {
            "object_kind": "org_member",
            "object_id": str(member_id),
            "outcome": "unavailable",
            "reason": "member_not_in_tenant",
        }
    links = list(
        (
            await session.execute(
                select(OrgMemberUserLink).where(
                    OrgMemberUserLink.tenant_id == tenant_id,
                    OrgMemberUserLink.org_member_id == member.id,
                    OrgMemberUserLink.status == "active",
                )
            )
        ).scalars().all()
    )
    if not links:
        return {
            "object_kind": "org_member",
            "object_id": str(member.id),
            "outcome": "skipped",
            "reason": "unlinked",
        }
    provider_ids = {link.provider_id for link in links if link.provider_id}
    from app.models.identity import IdentityProvider

    providers = list(
        (
            await session.execute(select(IdentityProvider).where(IdentityProvider.id.in_(provider_ids)))
        ).scalars().all()
    ) if provider_ids else []
    users = list(
        (
            await session.execute(select(User).where(User.id.in_({link.user_id for link in links})))
        ).scalars().all()
    )
    users_by_id = {user.id: user for user in users}
    providers_by_id = {provider.id: provider for provider in providers}
    bindings = await _bindings_for_providers(session, list(provider_ids), actor_id=None)
    valid_users = []
    invalid_reason = None
    for link in links:
        valid, reason = recheck_stored_link(
            link,
            member=member,
            user=users_by_id.get(link.user_id),
            provider=providers_by_id.get(link.provider_id) if link.provider_id else None,
            bindings=bindings,
        )
        if valid:
            valid_users.append(link.user_id)
        else:
            invalid_reason = reason
    distinct_users = list(dict.fromkeys(valid_users))
    if len(distinct_users) != 1:
        return {
            "object_kind": "org_member",
            "object_id": str(member.id),
            "outcome": "skipped" if not distinct_users else "conflict",
            "reason": (invalid_reason or "unlinked") if not distinct_users else "ambiguous_identity",
        }
    if member.department_id is None:
        return {
            "object_kind": "org_member",
            "object_id": str(member.id),
            "outcome": "skipped",
            "reason": "missing_directory_department",
            "user_id": str(distinct_users[0]),
        }
    department_link = (
        await session.execute(
            select(DepartmentDirectoryLink).where(
                DepartmentDirectoryLink.tenant_id == tenant_id,
                DepartmentDirectoryLink.org_department_id == member.department_id,
                DepartmentDirectoryLink.status == "active",
            )
        )
    ).scalar_one_or_none()
    if department_link is None or department_link.department_id is None:
        return {
            "object_kind": "org_member",
            "object_id": str(member.id),
            "outcome": "skipped",
            "reason": "unmapped",
            "user_id": str(distinct_users[0]),
        }
    reason = await _department_link_reason(session, tenant_id, department_link)
    member_provider = providers_by_id.get(member.provider_id)
    if not reason and (
        department_link.provider_id is not None and department_link.provider_id != member.provider_id
        or department_link.provider_id is None and member_provider is not None and member_provider.provider_type != "feishu"
    ):
        reason = "provider_namespace"
    if reason:
        return {"object_kind": "org_member", "object_id": str(member.id), "outcome": "conflict",
            "reason": reason, "user_id": str(distinct_users[0])}
    return {
        "member_id": member.id,
        "user_id": distinct_users[0],
        "department_id": department_link.department_id,
        "link_id": department_link.id,
        "member_name": member.name,
        "member_title": member.title,
        "member_phone": member.phone,
        "member_avatar": member.avatar_url,
    }


async def _withheld_department_conflict(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    user_id: str | None,
    counts: dict,
) -> dict[str, Any]:
    """A retained directory primary is not authority to choose one department."""

    member = await _member_in_tenant(session, tenant_id, member_id)
    counts["user_assignments"]["conflict"] += 1
    return {
        "object_kind": "user",
        "object_id": user_id or str(member_id),
        "org_member_id": str(member_id),
        "user_id": user_id,
        "person_label": None if member is None else member.name,
        "outcome": "conflict",
        "reason": "multiple_directory_departments",
    }


async def _department_label(session: AsyncSession, tenant_id: uuid.UUID, department_id: uuid.UUID | None) -> str | None:
    if department_id is None:
        return None
    department = await session.get(Department, department_id)
    if department is None or department.tenant_id != tenant_id:
        return None
    return department.name


async def _assign_user_department(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    user_id: uuid.UUID,
    plans: list[dict],
    counts: dict,
) -> list[dict]:
    await _lock(session, [_advisory_key("user-department", tenant_id, user_id)])
    user = await _fresh_locked(
        session,
        User,
        user_id,
        select(User).where(User.id == user_id, User.tenant_id == tenant_id),
    )
    targets = {plan["department_id"] for plan in plans}
    refreshed = []
    for plan in plans:
        again = await _member_cutover_plan(session, tenant_id, plan["member_id"])
        if again.get("outcome"):
            refreshed.append(again)
        elif again.get("user_id") != user_id or again.get("department_id") not in targets:
            refreshed.append(
                {
                    "object_kind": "org_member",
                    "object_id": str(plan["member_id"]),
                    "outcome": "conflict",
                    "reason": "stale_preview",
                    "user_id": str(user_id),
                }
            )
        else:
            refreshed.append(again)
    if user is None or user.is_active is not True:
        reason = "cross_tenant" if user is None else "inactive_user"
        outcome = "unavailable" if user is None else "skipped"
        for plan in plans:
            counts["user_assignments"][outcome] += 1
        return [
            {
                "object_kind": "user",
                "object_id": str(user_id),
                "org_member_id": str(plan["member_id"]),
                "outcome": outcome,
                "reason": reason,
            }
            for plan in plans
        ]
    blocked = [item for item in refreshed if item.get("outcome")]
    if blocked or len({item["department_id"] for item in refreshed if item.get("department_id")}) != 1:
        reason = "multiple_directory_departments" if not blocked else blocked[0]["reason"]
        outcome = "conflict" if reason == "multiple_directory_departments" else (blocked[0]["outcome"] if blocked else "conflict")
        counts["user_assignments"][outcome if outcome in counts["user_assignments"] else "conflict"] += 1
        return [
            {
                "object_kind": "user",
                "object_id": str(user.id),
                "org_member_id": str(plan["member_id"]),
                "outcome": outcome if outcome in {"conflict", "skipped", "unavailable"} else "conflict",
                "reason": reason,
            }
            for plan in plans
        ]
    target = refreshed[0]["department_id"]
    link_id = refreshed[0]["link_id"]
    sibling_ids = list(
        (
            await session.execute(
                select(OrgMemberUserLink.org_member_id).where(
                    OrgMemberUserLink.tenant_id == tenant_id,
                    OrgMemberUserLink.user_id == user.id,
                    OrgMemberUserLink.status == "active",
                )
            )
        ).scalars().all()
    )
    sibling_targets: set[uuid.UUID] = set()
    for sibling_id in sibling_ids:
        sibling = await _member_cutover_plan(session, tenant_id, sibling_id)
        if sibling.get("department_id"):
            sibling_targets.add(sibling["department_id"])
    if sibling_targets != {target}:
        reason = "multiple_directory_departments" if len(sibling_targets) > 1 else "stale_preview"
        counts["user_assignments"]["conflict"] += 1
        return [
            {
                "object_kind": "user",
                "object_id": str(user.id),
                "org_member_id": str(plan["member_id"]),
                "outcome": "conflict",
                "reason": reason,
            }
            for plan in plans
        ]
    department = await _fresh_locked(
        session,
        Department,
        target,
        select(Department).where(Department.id == target, Department.tenant_id == tenant_id),
    )
    if department is None:
        counts["user_assignments"]["unavailable"] += 1
        return [
            {
                "object_kind": "user",
                "object_id": str(user.id),
                "outcome": "unavailable",
                "reason": "department_not_in_tenant",
            }
        ]
    if user.department_id is not None and user.department_id != target:
        counts["user_assignments"]["conflict"] += 1
        current_name = await _department_label(session, tenant_id, user.department_id)
        mapped_name = await _department_label(session, tenant_id, target)
        return [
            {
                "object_kind": "user",
                "object_id": str(user.id),
                "org_member_id": str(plan["member_id"]),
                "person_label": plan.get("member_name"),
                "outcome": "conflict",
                "reason": "department_id_points_elsewhere",
                "department_id": str(user.department_id),
                "department_name": current_name,
                "mapped_department_id": str(target),
                "mapped_department_name": mapped_name,
            }
            for plan in plans
        ]
    changed = user.department_id is None
    if changed:
        before = _user_snapshot(user, link_id=None, member_id=None)
        user.department_id = target
        await session.flush()
        after = _user_snapshot(user, link_id=link_id, member_id=plans[0]["member_id"])
        await _journal(
            session,
            tenant_id=tenant_id,
            object_kind="user_department",
            object_id=user.id,
            before=before,
            after=after,
            action="user_department.cutover",
            actor_id=actor_id,
            details={"user_id": str(user.id), "department_id": str(target), "department_directory_link_id": str(link_id)},
        )
        counts["user_assignments"]["changed"] += 1
    else:
        counts["user_assignments"]["noop"] += 1
    preserved = await _member_in_tenant(session, tenant_id, plans[0]["member_id"])
    if preserved is None or (
        preserved.name != plans[0]["member_name"]
        or preserved.title != plans[0]["member_title"]
        or preserved.phone != plans[0]["member_phone"]
        or preserved.avatar_url != plans[0]["member_avatar"]
    ):
        raise RuntimeError("department cutover changed the stored directory profile")
    return [
        {
            "object_kind": "user",
            "object_id": str(user.id),
            "org_member_id": str(plan["member_id"]),
            "person_label": plan.get("member_name"),
            "outcome": "changed" if changed else "noop",
            "reason": "department_assigned" if changed else "already_assigned",
            "department_id": str(target),
            "department_name": department.name,
        }
        for plan in plans
    ]


async def repair_user_department(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    department_id: uuid.UUID,
) -> dict[str, Any]:
    """Explicitly set one user's department. This is a new journaled write."""

    await lock_directory_sync(session, tenant_id)
    counts = _blank_counts()
    async with _AuditScope(session) as audit:
        await _lock(
            session,
            [
                _advisory_key("user-department", tenant_id, user_id),
                _advisory_key("department", tenant_id, department_id),
            ],
        )
        user = await _fresh_locked(
            session,
            User,
            user_id,
            select(User).where(User.id == user_id, User.tenant_id == tenant_id),
        )
        department = await _fresh_locked(
            session,
            Department,
            department_id,
            select(Department).where(Department.id == department_id, Department.tenant_id == tenant_id),
        )
        if user is None:
            counts["user_assignments"]["unavailable"] += 1
            row = {"object_kind": "user", "object_id": str(user_id), "outcome": "unavailable", "reason": "user_not_in_tenant"}
        elif department is None:
            counts["user_assignments"]["unavailable"] += 1
            row = {"object_kind": "user", "object_id": str(user.id), "outcome": "unavailable", "reason": "department_not_in_tenant"}
        else:
            members = await _linked_directory_members(session, tenant_id, user.id)
            before_observations = _directory_observation_facts(members)
            department_changed = user.department_id != department.id
            observation_changed = any(
                item["observed_directory_department_ids"] is not None for item in before_observations
            )
            if not department_changed and not observation_changed:
                counts["user_assignments"]["noop"] += 1
                row = {
                    "object_kind": "user",
                    "object_id": str(user.id),
                    "outcome": "noop",
                    "reason": "already_assigned",
                    "department_id": str(department.id),
                    "department_name": department.name,
                }
            else:
                before = _user_department_fact(user, observations=before_observations)
                if department_changed:
                    user.department_id = department.id
                for member in members:
                    if member.observed_directory_department_ids is not None:
                        member.observed_directory_department_ids = None
                await session.flush()
                after = _user_department_fact(user, observations=_directory_observation_facts(members))
                await _journal(
                    session,
                    tenant_id=tenant_id,
                    object_kind="user_department",
                    object_id=user.id,
                    before=before,
                    after=after,
                    action="user_department.repair",
                    actor_id=actor_id,
                    details={"user_id": str(user.id), "department_id": str(department.id)},
                )
                counts["user_assignments"]["changed"] += 1
                row = {
                    "object_kind": "user",
                    "object_id": str(user.id),
                    "outcome": "changed",
                    "reason": "department_repaired",
                    "department_id": str(department.id),
                    "department_name": department.name,
                }
        await audit.commit()
    return _result("hive.department_assignment_repair.v1", tenant_id, counts, [row])


async def list_cutover_journal(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(IdentityCutoverJournal)
                .where(IdentityCutoverJournal.tenant_id == tenant_id)
                .order_by(IdentityCutoverJournal.applied_at.desc(), IdentityCutoverJournal.id.desc())
            )
        ).scalars().all()
    )
    actors = await _journal_actors(session, tenant_id, rows)
    return {
        "schema": "hive.identity_cutover_journal.v1",
        "tenant_id": str(tenant_id),
        "applied": False,
        "rows": [_public_journal(row, actors.get(row.id)) for row in rows],
    }


def _public_journal(row: IdentityCutoverJournal, actor: dict[str, str | None] | None = None) -> dict[str, Any]:
    known = actor or {}
    return {
        "id": str(row.id),
        "object_kind": row.object_kind,
        "object_id": str(row.object_id),
        "before": row.before_json,
        "after": row.after_json,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "actor_id": known.get("actor_id"),
        "actor_label": known.get("actor_label"),
    }


def _cited_journal_ids(details: object) -> set[str]:
    """Rows this audit wrote. ``journal_id`` only cites the row a rollback restored."""

    if not isinstance(details, dict):
        return set()
    raw = details.get("journal_ids")
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


async def _journal_actors(session: AsyncSession, tenant_id: uuid.UUID, rows: list[IdentityCutoverJournal]) -> dict:
    """Match a journal row only when one same-transaction audit cites its id."""

    if not rows:
        return {}
    from app.models.security_audit import SecurityAuditEvent

    events = list(
        (
            await session.execute(
                select(SecurityAuditEvent).where(
                    SecurityAuditEvent.tenant_id == tenant_id,
                    SecurityAuditEvent.event_type.in_(("org.identity_cutover", "org.identity_link")),
                    SecurityAuditEvent.resource_id.in_([row.object_id for row in rows]),
                )
            )
        ).scalars().all()
    )
    actor_ids = {event.actor_id for event in events if event.actor_id is not None}
    labels = {}
    if actor_ids:
        users = (
            await session.execute(select(User).where(User.tenant_id == tenant_id, User.id.in_(actor_ids)))
        ).scalars().all()
        labels = {user.id: user.display_name or user.username for user in users}
    by_journal: dict[str, list] = defaultdict(list)
    for event in events:
        for journal_id in _cited_journal_ids(event.details):
            by_journal[journal_id].append(event)
    found = {}
    for row in rows:
        matches = []
        seen: set[uuid.UUID] = set()
        for event in by_journal.get(str(row.id), []):
            if event.id in seen:
                continue
            seen.add(event.id)
            matches.append(event)
        if len(matches) != 1:
            continue
        event = matches[0]
        found[row.id] = {
            "actor_id": None if event.actor_id is None else str(event.actor_id),
            "actor_label": labels.get(event.actor_id),
        }
    return found


def _stored_directory_observation(member: OrgMember) -> list[str] | None:
    raw = member.observed_directory_department_ids
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    return None


def _observation_key(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    return tuple(str(item) for item in value)


def _directory_observation_facts(members: list[OrgMember]) -> list[dict[str, Any]]:
    return [
        {
            "org_member_id": str(member.id),
            "observed_directory_department_ids": _stored_directory_observation(member),
        }
        for member in sorted(members, key=lambda item: str(item.id))
    ]


def _user_department_fact(user: User, *, observations: list[dict[str, Any]]) -> dict[str, Any]:
    fact = _user_snapshot(user, link_id=None, member_id=None)
    fact["directory_observations"] = observations
    return fact


async def _linked_directory_members(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[OrgMember]:
    member_ids = list(
        (
            await session.execute(
                select(OrgMemberUserLink.org_member_id).where(
                    OrgMemberUserLink.tenant_id == tenant_id,
                    OrgMemberUserLink.user_id == user_id,
                    OrgMemberUserLink.status == "active",
                )
            )
        ).scalars().all()
    )
    if not member_ids:
        return []
    members = list(
        (
            await session.execute(
                select(OrgMember).where(OrgMember.tenant_id == tenant_id, OrgMember.id.in_(member_ids))
            )
        ).scalars().all()
    )
    return sorted(members, key=lambda item: str(item.id))


def _observation_rows(record: dict | None) -> dict[str, object]:
    rows: dict[str, object] = {}
    if not isinstance(record, dict):
        return rows
    for item in record.get("directory_observations") or []:
        if isinstance(item, dict) and item.get("org_member_id"):
            rows[str(item["org_member_id"])] = item.get("observed_directory_department_ids")
    return rows


def _observation_writes(
    before: dict | None,
    after: dict | None,
    current: dict[str, list[str] | None],
) -> dict[str, list[str] | None] | None:
    """Restore observations that still equal this journal's after state.

    None means the journal did not record observations. A current value that
    differs from after is a newer provider observation and is left unchanged.
    """

    if not isinstance(after, dict) or "directory_observations" not in after:
        return None
    before_rows = _observation_rows(before)
    writes: dict[str, list[str] | None] = {}
    for member_id, expected in _observation_rows(after).items():
        if member_id not in before_rows or member_id not in current:
            continue
        if _observation_key(current[member_id]) != _observation_key(expected):
            continue
        target = before_rows[member_id]
        restored = [str(value) for value in target] if isinstance(target, list) and target else None
        if _observation_key(current[member_id]) == _observation_key(restored):
            continue
        writes[member_id] = restored
    return writes


async def rollback_cutover(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    journal_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """Restore selected journal rows only when current fields still match after."""

    await lock_directory_sync(session, tenant_id)
    counts = _blank_counts()
    rows: list[dict] = []
    async with _AuditScope(session) as audit:
        selected = list(
            (
                await session.execute(
                    select(IdentityCutoverJournal)
                    .where(
                        IdentityCutoverJournal.tenant_id == tenant_id,
                        IdentityCutoverJournal.id.in_(set(journal_ids)),
                    )
                    .with_for_update()
                )
            ).scalars().all()
        )
        found = {row.id for row in selected}
        for journal_id in journal_ids:
            if journal_id not in found:
                counts["link_backfill"]["unavailable"] += 1
                rows.append(
                    {
                        "journal_id": str(journal_id),
                        "outcome": "unavailable",
                        "reason": "journal_not_in_tenant",
                    }
                )
        protected: set[str] = set()
        people = [
            row
            for row in selected
            if row.object_kind in {"user_department", "org_member_user_link"}
        ]
        departments = [row for row in selected if row.object_kind == "department_directory_link"]
        unknown = [
            row
            for row in selected
            if row.object_kind not in {"user_department", "org_member_user_link", "department_directory_link"}
        ]
        for row in sorted(people, key=lambda item: (item.applied_at, item.id), reverse=True):
            result = await _restore_journal(session, tenant_id=tenant_id, actor_id=actor_id, row=row, counts=counts)
            if row.object_kind == "user_department" and result["outcome"] == "conflict":
                link_id = str((row.after_json or {}).get("department_directory_link_id") or "")
                if link_id:
                    protected.add(link_id)
            rows.append(result)
        for row in sorted(departments, key=lambda item: (item.applied_at, item.id), reverse=True):
            if str(row.object_id) in protected:
                retained = (
                    await session.execute(
                        select(DepartmentDirectoryLink).where(
                            DepartmentDirectoryLink.id == row.object_id,
                            DepartmentDirectoryLink.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                rows.append(
                    {
                        "journal_id": str(row.id),
                        "object_kind": row.object_kind,
                        "object_id": str(row.object_id),
                        "outcome": "conflict",
                        "reason": "dependent_link_retained",
                        "expected_after": row.after_json,
                        "observed": _department_snapshot(retained) if retained is not None else None,
                    }
                )
                counts["department_mappings"]["conflict"] += 1
                continue
            rows.append(await _restore_journal(session, tenant_id=tenant_id, actor_id=actor_id, row=row, counts=counts))
        for row in unknown:
            counts["link_backfill"]["skipped"] += 1
            rows.append(
                {
                    "journal_id": str(row.id),
                    "object_kind": row.object_kind,
                    "object_id": str(row.object_id),
                    "outcome": "skipped",
                    "reason": "unsupported_object",
                }
            )
        await audit.commit()
    return _result("hive.identity_cutover_rollback.v1", tenant_id, counts, rows)


def _same_mapping(observed: dict | None, expected: dict | None, keys: tuple[str, ...]) -> bool:
    if not isinstance(expected, dict):
        return False
    observed = observed or {}
    return all(observed.get(key) == expected.get(key) for key in keys)


async def _restore_journal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    row: IdentityCutoverJournal,
    counts: dict,
) -> dict[str, Any]:
    if row.object_kind == "user_department":
        return await _restore_user(session, tenant_id=tenant_id, actor_id=actor_id, row=row, counts=counts)
    if row.object_kind == "org_member_user_link":
        return await _restore_member_link(session, tenant_id=tenant_id, actor_id=actor_id, row=row, counts=counts)
    if row.object_kind == "department_directory_link":
        return await _restore_department_link(session, tenant_id=tenant_id, actor_id=actor_id, row=row, counts=counts)
    counts["link_backfill"]["skipped"] += 1
    return {"journal_id": str(row.id), "outcome": "skipped", "reason": "unsupported_object"}


async def _restore_user(session, *, tenant_id, actor_id, row, counts) -> dict[str, Any]:
    user = await _fresh_locked(
        session,
        User,
        row.object_id,
        select(User).where(User.id == row.object_id, User.tenant_id == tenant_id),
    )
    expected = (row.after_json or {}).get("department_id")
    observed = str(user.department_id) if user is not None and user.department_id else None
    if user is None or observed != expected:
        counts["user_assignments"]["conflict"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(row.object_id),
            "outcome": "conflict",
            "reason": "rollback_conflict",
            "expected_after": {"department_id": expected},
            "observed": {"department_id": observed},
        }
    before_body = row.before_json or {}
    before_id = before_body.get("department_id")
    recorded = isinstance(row.after_json, dict) and "directory_observations" in row.after_json
    members = await _linked_directory_members(session, tenant_id, user.id) if recorded else []
    current_observations = {str(member.id): _stored_directory_observation(member) for member in members}
    writes = _observation_writes(before_body, row.after_json, current_observations) or {}
    if observed == before_id and not writes:
        counts["user_assignments"]["noop"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(user.id),
            "outcome": "noop",
            "reason": "already_restored",
        }
    previous = (
        _user_department_fact(user, observations=_directory_observation_facts(members))
        if recorded
        else _user_snapshot(user, link_id=None, member_id=None)
    )
    user.department_id = uuid.UUID(before_id) if before_id else None
    by_member = {str(member.id): member for member in members}
    for member_id, target in writes.items():
        by_member[member_id].observed_directory_department_ids = list(target) if target else None
    await session.flush()
    after = (
        _user_department_fact(user, observations=_directory_observation_facts(members))
        if recorded
        else _user_snapshot(user, link_id=None, member_id=None)
    )
    await _journal(
        session,
        tenant_id=tenant_id,
        object_kind="user_department",
        object_id=user.id,
        before=previous,
        after=after,
        action="user_department.rollback",
        actor_id=actor_id,
        details={"user_id": str(user.id), "journal_id": str(row.id), "department_id": after["department_id"]},
    )
    counts["user_assignments"]["changed"] += 1
    return {
        "journal_id": str(row.id),
        "object_kind": row.object_kind,
        "object_id": str(user.id),
        "outcome": "restored",
        "reason": "restored",
        "department_id": after["department_id"],
    }


def _rollback_conflict(row: IdentityCutoverJournal, *, reason: str, observed) -> dict[str, Any]:
    return {
        "journal_id": str(row.id),
        "object_kind": row.object_kind,
        "object_id": str(row.object_id),
        "outcome": "conflict",
        "reason": reason,
        "expected_after": row.after_json,
        "observed": observed,
    }


async def _member_active_taken(session: AsyncSession, tenant_id: uuid.UUID, link: OrgMemberUserLink, status: str) -> bool:
    if status != "active":
        return False
    shared = (
        OrgMemberUserLink.tenant_id == tenant_id,
        OrgMemberUserLink.status == "active",
        OrgMemberUserLink.id != link.id,
    )
    subject = (
        await session.execute(
            select(OrgMemberUserLink.id)
            .where(
                *shared,
                OrgMemberUserLink.provider_id == link.provider_id,
                OrgMemberUserLink.subject_kind == link.subject_kind,
                OrgMemberUserLink.match_subject == link.match_subject,
            )
            .limit(1)
        )
    ).first()
    if subject is not None:
        return True
    member = (
        await session.execute(
            select(OrgMemberUserLink.id)
            .where(
                *shared,
                OrgMemberUserLink.org_member_id == link.org_member_id,
                OrgMemberUserLink.provider_id == link.provider_id,
                OrgMemberUserLink.subject_kind == link.subject_kind,
            )
            .limit(1)
        )
    ).first()
    return member is not None


async def _department_active_taken(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    link: DepartmentDirectoryLink,
    status: str,
    department_id: uuid.UUID | None,
) -> bool:
    if status != "active":
        return False
    shared = (
        DepartmentDirectoryLink.tenant_id == tenant_id,
        DepartmentDirectoryLink.status == "active",
        DepartmentDirectoryLink.id != link.id,
    )
    org = (
        await session.execute(
            select(DepartmentDirectoryLink.id)
            .where(*shared, DepartmentDirectoryLink.org_department_id == link.org_department_id)
            .limit(1)
        )
    ).first()
    if org is not None:
        return True
    provider = (
        await session.execute(
            select(DepartmentDirectoryLink.id)
            .where(
                *shared,
                DepartmentDirectoryLink.provider == link.provider,
                DepartmentDirectoryLink.provider_department_id == link.provider_department_id,
            )
            .limit(1)
        )
    ).first()
    if provider is not None or department_id is None:
        return provider is not None
    taken = (
        await session.execute(
            select(DepartmentDirectoryLink.id)
            .where(*shared, DepartmentDirectoryLink.provider == link.provider,
                DepartmentDirectoryLink.department_id == department_id)
            .limit(1)
        )
    ).first()
    return taken is not None


async def _restore_member_link(session, *, tenant_id, actor_id, row, counts) -> dict[str, Any]:
    link = await _fresh_locked(
        session,
        OrgMemberUserLink,
        row.object_id,
        select(OrgMemberUserLink).where(
            OrgMemberUserLink.id == row.object_id,
            OrgMemberUserLink.tenant_id == tenant_id,
        ),
    )
    observed = _snapshot(link) if link is not None else None
    if link is None or not _same_mapping(
        observed,
        row.after_json,
        ("org_member_id", "user_id", "provider_id", "subject_kind", "match_subject", "status", "external_identity_id", "source"),
    ):
        counts["link_backfill"]["conflict"] += 1
        return _rollback_conflict(row, reason="rollback_conflict", observed=observed)
    target = "revoked" if row.before_json is None else (row.before_json.get("status") or link.status)
    if target == link.status:
        counts["link_backfill"]["noop"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(link.id),
            "outcome": "noop",
            "reason": "already_restored",
        }
    if await _member_active_taken(session, tenant_id, link, target):
        counts["link_backfill"]["conflict"] += 1
        return _rollback_conflict(row, reason="active_link_conflict", observed=observed)
    audit_mark = identity_audit_mark(session)
    wrote = None
    try:
        async with session.begin_nested():
            before = _snapshot(link)
            link.status = target
            await session.flush()
            after = _snapshot(link)
            if before != after:
                await _journal(
                    session,
                    tenant_id=tenant_id,
                    object_kind="org_member_user_link",
                    object_id=link.id,
                    before=before,
                    after=after,
                    action="identity_link.rollback",
                    actor_id=actor_id,
                    details={"journal_id": str(row.id), "status": link.status, "org_member_id": str(link.org_member_id)},
                )
                wrote = after
    except IntegrityError:
        rewind_deferred_identity_audit(session, audit_mark)
        session.expire(link)
        counts["link_backfill"]["conflict"] += 1
        return _rollback_conflict(row, reason="active_link_conflict", observed=observed)
    if wrote is None:
        counts["link_backfill"]["noop"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(link.id),
            "outcome": "noop",
            "reason": "already_restored",
        }
    counts["link_backfill"]["applied"] += 1
    return {
        "journal_id": str(row.id),
        "object_kind": row.object_kind,
        "object_id": str(link.id),
        "outcome": "restored",
        "reason": wrote["status"],
    }


async def _restore_department_link(session, *, tenant_id, actor_id, row, counts) -> dict[str, Any]:
    link = await _department_link(session, tenant_id, row.object_id)
    observed = _department_snapshot(link) if link is not None else None
    if link is None or not _same_mapping(
        observed,
        row.after_json,
        ("org_department_id", "department_id", "provider", "provider_id", "provider_department_id", "status"),
    ):
        counts["department_mappings"]["conflict"] += 1
        return _rollback_conflict(row, reason="rollback_conflict", observed=observed)
    if row.before_json is None:
        target_status, target_department = "revoked", link.department_id
    else:
        raw_department = row.before_json.get("department_id")
        target_status = row.before_json.get("status") or link.status
        target_department = uuid.UUID(str(raw_department)) if raw_department else None
    if target_status == link.status and target_department == link.department_id:
        counts["department_mappings"]["noop"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(link.id),
            "outcome": "noop",
            "reason": "already_restored",
        }
    if target_status == "active" and link.provider_id is not None:
        reason = await _department_link_reason(session, tenant_id, link)
        if reason:
            counts["department_mappings"]["conflict"] += 1
            return _rollback_conflict(row, reason=reason, observed=observed)
    if await _department_active_taken(session, tenant_id, link, target_status, target_department):
        counts["department_mappings"]["conflict"] += 1
        return _rollback_conflict(row, reason="already_mapped", observed=observed)
    audit_mark = identity_audit_mark(session)
    wrote = None
    try:
        async with session.begin_nested():
            before = _department_snapshot(link)
            link.status = target_status
            link.department_id = target_department
            await session.flush()
            after = _department_snapshot(link)
            if before != after:
                await _journal(
                    session,
                    tenant_id=tenant_id,
                    object_kind="department_directory_link",
                    object_id=link.id,
                    before=before,
                    after=after,
                    action="department_directory_link.rollback",
                    actor_id=actor_id,
                    details={
                        "journal_id": str(row.id),
                        "status": link.status,
                        "org_department_id": str(link.org_department_id),
                    },
                )
                wrote = after
    except IntegrityError:
        rewind_deferred_identity_audit(session, audit_mark)
        session.expire(link)
        counts["department_mappings"]["conflict"] += 1
        return _rollback_conflict(row, reason="already_mapped", observed=observed)
    if wrote is None:
        counts["department_mappings"]["noop"] += 1
        return {
            "journal_id": str(row.id),
            "object_kind": row.object_kind,
            "object_id": str(link.id),
            "outcome": "noop",
            "reason": "already_restored",
        }
    counts["department_mappings"]["revoked" if wrote["status"] == "revoked" else "mapped"] += 1
    return {
        "journal_id": str(row.id),
        "object_kind": row.object_kind,
        "object_id": str(link.id),
        "outcome": "restored",
        "reason": wrote["status"],
    }
