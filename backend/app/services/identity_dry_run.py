"""Read-only organization identity assessment.

The report describes stored rows and the provider snapshot that a sync would
submit. It is not authority to create links, users, or departments.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY, TEXT
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.runtime_task import RuntimeTask
from app.models.user import Department, User
from app.services.company_knowledge_identity import explicit_subject_matches
from app.services.identity_links import _Binding, recheck_stored_link
from app.services.org_sync_service import DirectoryFetchError, INCOMPLETE_MESSAGE

SCHEMA = "hive.identity_dry_run.v1"
COUNT_KEYS = (
    "trusted",
    "conflict",
    "unregistered",
    "inactive_user",
    "cross_tenant",
    "name_collision",
    "department_conflict",
    "email_only_candidate",
)
_KIND_ORDER = ("provider_open_id", "provider_user_id", "provider_union_id")
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def identity_source_revision() -> str:
    """Return the git revision, or unknown when it cannot be proven."""

    override = os.environ.get("HIVE_SOURCE_REVISION", "").strip()
    if override:
        return override
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = completed.stdout.strip().lower()
    if len(revision) < 7 or any(char not in "0123456789abcdef" for char in revision):
        return "unknown"
    return revision


def _text(value: object) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _real_email(value: object) -> str | None:
    email = _text(value)
    if email is None or "@" not in email or email.endswith("@feishu.local"):
        return None
    return email


def _namespace(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def _preferred_kind(kinds: list[str]) -> str | None:
    for kind in _KIND_ORDER:
        if kind in kinds:
            return kind
    return kinds[0] if kinds else None


def _row(
    *,
    object_kind: str,
    object_id: str,
    disposition: str,
    reason: str,
    user_id: uuid.UUID | None,
    blocked: bool,
    provenance: dict,
) -> dict:
    return {
        "object_kind": object_kind,
        "object_id": object_id,
        "disposition": disposition,
        "reason": reason,
        "user_id": str(user_id) if user_id else None,
        "global_unique_blocked": blocked,
        "provenance": provenance,
    }


async def _foreign_blocks(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    probes: dict[str, list[str]],
) -> set[tuple[str, str]]:
    """Return caller subjects that already exist outside this tenant.

    The query selects only those subject values. It does not return the other
    tenant, user, or row.
    """

    clauses: list[str] = []
    params: dict[str, object] = {"tenant": str(tenant_id)}
    if probes["open_ids"]:
        clauses.append(
            """
            SELECT 'open' AS kind, feishu_open_id AS value FROM org_members
            WHERE feishu_open_id = ANY(:open_ids) AND tenant_id::text <> :tenant
            UNION ALL
            SELECT 'open' AS kind, feishu_open_id AS value FROM users
            WHERE feishu_open_id = ANY(:open_ids) AND tenant_id::text IS DISTINCT FROM :tenant
            """
        )
        params["open_ids"] = probes["open_ids"]
    if probes["dept_ids"]:
        clauses.append(
            """
            SELECT 'dept' AS kind, feishu_id AS value FROM org_departments
            WHERE feishu_id = ANY(:dept_ids) AND tenant_id::text <> :tenant
            """
        )
        params["dept_ids"] = probes["dept_ids"]
    if probes["emails"]:
        clauses.append(
            """
            SELECT 'email' AS kind, email AS value FROM users
            WHERE email = ANY(:emails) AND tenant_id::text IS DISTINCT FROM :tenant
            """
        )
        params["emails"] = probes["emails"]
    provider_id = probes.get("provider_id")
    for column, name, kind in (
        ("provider_open_id", "ext_open", "ext_open"),
        ("provider_user_id", "ext_user", "ext_user"),
        ("provider_union_id", "ext_union", "ext_union"),
    ):
        if provider_id and probes[name]:
            clauses.append(
                f"""
                SELECT '{kind}' AS kind, {column} AS value FROM external_identities
                WHERE provider_id::text = :provider_id AND {column} = ANY(:{name})
                  AND NOT EXISTS (
                    SELECT 1 FROM users AS same_tenant
                    WHERE same_tenant.id = external_identities.user_id
                      AND same_tenant.tenant_id::text = :tenant
                  )
                """
            )
            params[name] = probes[name]
            params["provider_id"] = str(provider_id)
    if not clauses:
        return set()
    statement = text(" UNION ALL ".join(clauses))
    for name, value in params.items():
        if isinstance(value, list):
            statement = statement.bindparams(bindparam(name, type_=ARRAY(TEXT)))
    found: set[tuple[str, str]] = set()
    async with enter_rls_bypass(
        db,
        reason="identity_dry_run_global_unique_exists",
        actor_id=str(actor_id) if actor_id else None,
    ):
        result = await db.execute(statement, params)
        for kind, value in result.all():
            if kind and value:
                found.add((str(kind), str(value)))
    return found


def _match_union(users: list[User], namespaces: list[SimpleNamespace]) -> dict:
    matched: dict[str, User] = {}
    kinds: dict[str, list[str]] = {}
    subjects: dict[str, dict[str, str]] = {}
    for namespace in namespaces:
        found = explicit_subject_matches(users, namespace)
        for user in found["users"]:
            matched[str(user.id)] = user
        for user_id, user_kinds in found["kinds_by_user"].items():
            bucket = kinds.setdefault(user_id, [])
            for kind in user_kinds:
                if kind not in bucket:
                    bucket.append(kind)
        for user_id, values in found["subjects_by_user"].items():
            subjects.setdefault(user_id, {}).update(values)
    return {"users": list(matched.values()), "kinds_by_user": kinds, "subjects_by_user": subjects}


def _member_namespace(open_id, user_id, union_id, external_id) -> SimpleNamespace | None:
    if not any((open_id, user_id, union_id, external_id)):
        return None
    return _namespace(
        feishu_open_id=open_id,
        open_id=open_id,
        feishu_user_id=user_id,
        external_id=external_id,
        unionid=union_id,
    )


async def build_identity_dry_run_report(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    snapshot: dict,
    actor_id: uuid.UUID | None,
    source_revision: str,
) -> dict:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("complete") is not True
        or not isinstance(snapshot.get("departments"), list)
        or not isinstance(snapshot.get("members"), list)
    ):
        raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
    observed = datetime.now(timezone.utc).isoformat()
    users = list((await db.execute(select(User).where(User.tenant_id == tenant_id))).scalars().all())
    departments = list(
        (await db.execute(select(Department).where(Department.tenant_id == tenant_id))).scalars().all()
    )
    org_departments = list(
        (await db.execute(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id))).scalars().all()
    )
    org_members = list((await db.execute(select(OrgMember).where(OrgMember.tenant_id == tenant_id))).scalars().all())
    providers = list(
        (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.provider_type == "feishu",
                    IdentityProvider.tenant_id == tenant_id,
                    IdentityProvider.is_active.is_(True),
                )
            )
        ).scalars().all()
    )
    proven = providers[0] if len(providers) == 1 else None
    identities = []
    if proven is not None:
        identities = list(
            (
                await db.execute(select(ExternalIdentity).where(ExternalIdentity.provider_id == proven.id))
            ).scalars().all()
        )
    tasks = (
        await db.execute(
            select(RuntimeTask.id, RuntimeTask.root_user_id).where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.root_user_id.is_not(None),
            )
        )
    ).all()
    users_by_id = {user.id: user for user in users}
    users_by_email = {_real_email(user.email): user for user in users if _real_email(user.email)}
    department_names = {department.name for department in departments}
    departments_by_id = {department.id: department for department in departments}
    org_departments_by_id = {department.id: department for department in org_departments}
    identity_by_subject: dict[tuple[str, str], list[ExternalIdentity]] = {}
    bindings = [
        _Binding(
            id=identity.id,
            provider_id=identity.provider_id,
            user_id=identity.user_id,
            provider_user_id=identity.provider_user_id,
            provider_open_id=identity.provider_open_id,
            provider_union_id=identity.provider_union_id,
        )
        for identity in identities
    ]
    active_links: list[OrgMemberUserLink] = []
    if proven is not None:
        active_links = list(
            (
                await db.execute(
                    select(OrgMemberUserLink).where(
                        OrgMemberUserLink.tenant_id == tenant_id,
                        OrgMemberUserLink.provider_id == proven.id,
                        OrgMemberUserLink.status == "active",
                    )
                )
            ).scalars().all()
        )
    members_by_id = {member.id: member for member in org_members}
    for identity in identities:
        for kind, value in (
            ("provider_open_id", _text(identity.provider_open_id)),
            ("provider_user_id", _text(identity.provider_user_id)),
            ("provider_union_id", _text(identity.provider_union_id)),
        ):
            if value:
                identity_by_subject.setdefault((kind, value), []).append(identity)

    incoming_departments = [_incoming_department(item) for item in snapshot["departments"]]
    incoming_members = [_incoming_member(item) for item in snapshot["members"]]
    if any(item is None for item in incoming_departments + incoming_members):
        raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
    department_records = _merge_departments(org_departments, incoming_departments)
    member_records = _merge_members(org_members, incoming_members, org_departments_by_id, incoming_departments)

    probes = _probe_values(department_records, member_records, str(proven.id) if proven else None)
    blocked = await _foreign_blocks(db, tenant_id, actor_id, probes)
    rows: list[dict] = []
    proposals: list[dict] = []
    for record in department_records:
        rows.extend(
            _department_rows(
                record,
                names=department_names,
                blocked=blocked,
                observed=observed,
                proposals=proposals,
            )
        )
    for record in member_records:
        rows.extend(
            _member_rows(
                record,
                users=users,
                users_by_email=users_by_email,
                departments_by_id=departments_by_id,
                proven=proven,
                identity_by_subject=identity_by_subject,
                blocked=blocked,
                observed=observed,
                active_links=active_links,
                members_by_id=members_by_id,
                users_by_id=users_by_id,
                bindings=bindings,
            )
        )
    active_user_ids = {user.id for user in users if user.is_active}
    for task_id, root_user_id in tasks:
        if root_user_id in active_user_ids and users_by_id[root_user_id].tenant_id == tenant_id:
            continue
        rows.append(
            _row(
                object_kind="runtime_task",
                object_id=str(task_id),
                disposition="conflict",
                reason="dangling_root_user_id",
                user_id=None,
                blocked=False,
                provenance={
                    "source": "dry_run",
                    "observed_at": observed,
                    "origin": "stored",
                    "applied": False,
                    "claimed_root_user_id": str(root_user_id),
                },
            )
        )
    rows.sort(key=lambda item: (item["object_kind"], item["object_id"], item["disposition"], item["reason"]))
    return {
        "schema": SCHEMA,
        "tenant_id": str(tenant_id),
        "source_revision": source_revision,
        "applied": False,
        "inputs": {"stored_rows": True, "incoming_provider_subjects": True},
        "counts": {key: sum(1 for item in rows if item["disposition"] == key) for key in COUNT_KEYS},
        "rows": rows,
        "department_proposals": proposals,
    }


def _incoming_department(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    return {
        "open_department_id": _text(item.get("open_department_id")),
        "name": _text(item.get("name")) or "",
        "parent_provider_department_id": _text(item.get("parent_department_id")),
    }


def _incoming_member(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    return {
        "open_id": _text(item.get("open_id")),
        "user_id": _text(item.get("user_id")),
        "union_id": _text(item.get("union_id")),
        "email": _text(item.get("email")),
        "department_id": _text(item.get("department_id")),
    }


def _merge_departments(stored: list[OrgDepartment], incoming: list[dict]) -> list[dict]:
    records = []
    used: set[str] = set()
    incoming_by_id = {
        item["open_department_id"]: item for item in incoming if item.get("open_department_id")
    }
    for department in stored:
        overlay = incoming_by_id.get(department.feishu_id or "")
        feishu_id = _text(department.feishu_id)
        if overlay:
            used.add(overlay["open_department_id"])
            feishu_id = overlay["open_department_id"] or feishu_id
        records.append(
            {
                "object_id": str(department.id),
                "origin": "stored_and_incoming" if overlay else "stored",
                "feishu_id": feishu_id,
                "name": (overlay or {}).get("name") or department.name,
                "parent_provider_department_id": (overlay or {}).get("parent_provider_department_id"),
            }
        )
    for item in incoming:
        feishu_id = item.get("open_department_id")
        if feishu_id and feishu_id in used:
            continue
        records.append(
            {
                "object_id": feishu_id or f"incoming-department-{len(records)}",
                "origin": "incoming",
                "feishu_id": feishu_id,
                "name": item.get("name") or "",
                "parent_provider_department_id": item.get("parent_provider_department_id"),
            }
        )
    return records


def _merge_members(stored, incoming, org_departments_by_id, incoming_departments) -> list[dict]:
    incoming_department_names = {
        item["open_department_id"]: item["name"]
        for item in incoming_departments
        if item.get("open_department_id")
    }
    by_user = {}
    by_open = {}
    for member in stored:
        if member.feishu_user_id:
            by_user.setdefault(member.feishu_user_id, member)
        if member.feishu_open_id:
            by_open.setdefault(member.feishu_open_id, member)
    overlays: dict[uuid.UUID, list[dict]] = {member.id: [] for member in stored}
    incoming_only: list[dict] = []
    local_blocked: set[uuid.UUID] = set()
    for item in incoming:
        matches = []
        seen: set[uuid.UUID] = set()
        for member in (
            by_user.get(item.get("user_id") or ""),
            by_open.get(item.get("open_id") or ""),
        ):
            if member is not None and member.id not in seen:
                seen.add(member.id)
                matches.append(member)
        if not matches:
            incoming_only.append(item)
            continue
        primary = by_user.get(item.get("user_id") or "") or matches[0]
        overlays[primary.id].append(item)
        if len(matches) > 1:
            local_blocked.add(primary.id)
    records = []
    for member in stored:
        record = _member_record(
            member,
            overlays[member.id],
            org_departments_by_id,
            incoming_department_names,
            origin="stored_and_incoming" if overlays[member.id] else "stored",
            object_id=str(member.id),
        )
        record["local_unique_blocked"] = member.id in local_blocked
        records.append(record)
    for index, item in enumerate(incoming_only):
        object_id = item.get("open_id") or item.get("user_id") or item.get("union_id") or f"incoming-member-{index}"
        records.append(
            _member_record(
                None,
                [item],
                org_departments_by_id,
                incoming_department_names,
                origin="incoming",
                object_id=object_id,
            )
        )
    return records


def _member_record(member, overlays, org_departments_by_id, incoming_department_names, *, origin, object_id) -> dict:
    open_ids = {value for value in (_text(getattr(member, "feishu_open_id", None)), _text(getattr(member, "open_id", None))) if value}
    user_ids = {value for value in (_text(getattr(member, "feishu_user_id", None)), _text(getattr(member, "external_id", None))) if value}
    union_ids = {value for value in (_text(getattr(member, "unionid", None)),) if value}
    emails = {value for value in (_text(getattr(member, "email", None)),) if value}
    department_ids = []
    if member is not None and member.department_id and member.department_id in org_departments_by_id:
        department_ids.append(org_departments_by_id[member.department_id].feishu_id)
    for item in overlays:
        for field, bucket in (("open_id", open_ids), ("user_id", user_ids), ("union_id", union_ids)):
            value = item.get(field)
            if value:
                bucket.add(value)
        if item.get("email"):
            emails.add(item["email"])
        if item.get("department_id"):
            department_ids.append(item["department_id"])
    names = []
    for department_id in department_ids:
        if department_id and department_id in incoming_department_names:
            names.append(incoming_department_names[department_id])
    if member is not None and member.department_id in org_departments_by_id:
        names.append(org_departments_by_id[member.department_id].name)
    distinct_departments = [value for value in dict.fromkeys(department_ids) if value]
    distinct_names = [value for value in dict.fromkeys(names) if value]
    return {
        "object_id": object_id,
        "origin": origin,
        "open_ids": sorted(open_ids),
        "user_ids": sorted(user_ids),
        "union_ids": sorted(union_ids),
        "email": next(iter(emails), None),
        "provider_id": getattr(member, "provider_id", None),
        "status": getattr(member, "status", None) or "active",
        "department_ids": distinct_departments,
        "department_name": distinct_names[0] if len(distinct_names) == 1 else None,
        "department_names": distinct_names,
    }


def _probe_values(departments: list[dict], members: list[dict], provider_id: str | None) -> dict:
    open_ids = sorted({value for member in members for value in member["open_ids"]})
    return {
        "open_ids": open_ids,
        "dept_ids": sorted({record["feishu_id"] for record in departments if record.get("feishu_id")}),
        "emails": sorted({member["email"] for member in members if _real_email(member.get("email"))}),
        "ext_open": open_ids,
        "ext_user": sorted({value for member in members for value in member["user_ids"]}),
        "ext_union": sorted({value for member in members for value in member["union_ids"]}),
        "provider_id": provider_id,
    }


def _department_rows(record, *, names, blocked, observed, proposals) -> list[dict]:
    feishu_id = record.get("feishu_id")
    blocked_id = bool(feishu_id and ("dept", feishu_id) in blocked)
    name = _text(record.get("name"))
    provenance = {
        "source": "dry_run",
        "observed_at": observed,
        "origin": record["origin"],
        "applied": False,
        "provider_department_id": feishu_id,
        "parent_provider_department_id": record.get("parent_provider_department_id"),
    }
    if blocked_id:
        return [
            _row(
                object_kind="org_department",
                object_id=record["object_id"],
                disposition="cross_tenant",
                reason="cross_tenant_subject",
                user_id=None,
                blocked=True,
                provenance=provenance,
            )
        ]
    if not name:
        return [
            _row(
                object_kind="org_department",
                object_id=record["object_id"],
                disposition="conflict",
                reason="missing_department_name",
                user_id=None,
                blocked=False,
                provenance=provenance,
            )
        ]
    if name in names:
        return [
            _row(
                object_kind="org_department",
                object_id=record["object_id"],
                disposition="name_collision",
                reason="name_collision",
                user_id=None,
                blocked=False,
                provenance=provenance,
            )
        ]
    proposals.append(
        {
            "object_kind": "org_department",
            "object_id": record["object_id"],
            "provider_department_id": feishu_id,
            "name": name,
            "parent_provider_department_id": record.get("parent_provider_department_id"),
            "action": "create",
            "applied": False,
        }
    )
    return []


def _accounts_for_active_links(record, proven, active_links, members_by_id, users_by_id, bindings) -> tuple[list, str | None]:
    """Return accounts a current directory link proves. Email is not consulted."""

    if proven is None or not active_links:
        return [], None
    try:
        member_id = uuid.UUID(str(record.get("object_id")))
    except ValueError:
        return [], None
    member = members_by_id.get(member_id)
    if member is None:
        return [], None
    kind_values = {
        "provider_open_id": record["open_ids"],
        "provider_user_id": record["user_ids"],
        "provider_union_id": record["union_ids"],
    }
    accepted: dict[uuid.UUID, User] = {}
    inactive = False
    for link in active_links:
        if link.org_member_id != member_id or link.provider_id != proven.id or link.status != "active":
            continue
        if link.match_subject not in (kind_values.get(link.subject_kind) or []):
            continue
        account = users_by_id.get(link.user_id)
        valid, reason = recheck_stored_link(
            link, member=member, user=account, provider=proven, bindings=bindings
        )
        if valid and account is not None:
            accepted[account.id] = account
        elif reason == "inactive_user":
            inactive = True
    if accepted:
        return list(accepted.values()), None
    if inactive:
        return [], "inactive_user"
    return [], None


def _member_rows(
    record,
    *,
    users,
    users_by_email,
    departments_by_id,
    proven,
    identity_by_subject,
    blocked,
    observed,
    active_links=None,
    members_by_id=None,
    users_by_id=None,
    bindings=None,
) -> list[dict]:
    namespaces = []
    for open_id in record["open_ids"]:
        namespace = _member_namespace(open_id, None, None, None)
        if namespace:
            namespaces.append(namespace)
    for user_id in record["user_ids"]:
        namespace = _member_namespace(None, user_id, None, user_id)
        if namespace:
            namespaces.append(namespace)
    for union_id in record["union_ids"]:
        namespace = _member_namespace(None, None, union_id, None)
        if namespace:
            namespaces.append(namespace)
    matched = _match_union(users, namespaces)
    matched_users = matched["users"]
    kinds = sorted({kind for values in matched["kinds_by_user"].values() for kind in values})
    primary_kind = _preferred_kind(kinds)
    provider_ok = _provider_ok(record, proven)
    foreign_identity = _foreign_identity(record, blocked)
    open_blocked = any(("open", value) in blocked for value in record["open_ids"])
    email = _real_email(record.get("email"))
    email_blocked = bool(email and ("email", email) in blocked)
    linked_users, link_rejection = _accounts_for_active_links(
        record,
        proven,
        active_links or [],
        members_by_id or {},
        users_by_id or {},
        bindings or [],
    )
    if linked_users:
        known = {user.id: user for user in matched_users}
        for account in linked_users:
            known.setdefault(account.id, account)
        matched_users = list(known.values())
    local_identity_conflict, same_identity = _local_identity_conflict(
        matched_users, record, identity_by_subject
    )
    subject = None
    if len(matched_users) == 1 and primary_kind:
        subject = matched["subjects_by_user"].get(str(matched_users[0].id), {}).get(primary_kind)
    candidate_ids = sorted(str(user.id) for user in matched_users)
    email_user = users_by_email.get(email) if email and not matched_users else None
    provenance = {
        "source": "dry_run",
        "observed_at": observed,
        "origin": record["origin"],
        "applied": False,
        "subject_kind": primary_kind,
        "subject_kinds": kinds,
        "match_subject": subject,
        "provider_id": str(proven.id) if proven is not None and provider_ok else None,
        "external_identity_id": str(same_identity.id) if same_identity is not None else None,
        "candidate_user_ids": candidate_ids,
        "open_ids": list(record["open_ids"]),
        "user_ids": list(record["user_ids"]),
        "union_ids": list(record["union_ids"]),
        "department_ids": list(record["department_ids"]),
    }
    rows = []
    if open_blocked or (email_blocked and email_user is None and not matched_users):
        provenance["candidate_user_ids"] = []
        rows.append(
            _row(
                object_kind="org_member",
                object_id=record["object_id"],
                disposition="cross_tenant",
                reason="cross_tenant_subject",
                user_id=None,
                blocked=True,
                provenance=provenance,
            )
        )
        return rows
    if record.get("local_unique_blocked"):
        provenance["candidate_user_ids"] = []
        rows.append(
            _row(
                object_kind="org_member",
                object_id=record["object_id"],
                disposition="conflict",
                reason="local_subject_collision",
                user_id=None,
                blocked=True,
                provenance=provenance,
            )
        )
        return rows
    disposition = None
    reason = None
    user_id = None
    blocked_flag = False
    if len(matched_users) > 1:
        disposition, reason = "conflict", "ambiguous_identity"
    elif len(matched_users) == 1 and not provider_ok:
        disposition, reason = "conflict", "provider_unproven"
    elif len(matched_users) == 1 and (local_identity_conflict or foreign_identity):
        disposition, reason = "conflict", "external_identity_conflict"
        blocked_flag = foreign_identity
    elif len(matched_users) == 1 and matched_users[0].is_active is False:
        disposition, reason, user_id = "inactive_user", "authenticated_user_inactive", matched_users[0].id
    elif len(matched_users) == 1 and record["status"] not in (None, "", "active"):
        disposition, reason = "conflict", "directory_member_inactive"
    elif len(matched_users) == 1:
        disposition, reason, user_id = "trusted", primary_kind or "provider_open_id", matched_users[0].id
    elif link_rejection == "inactive_user":
        disposition, reason = "inactive_user", "authenticated_user_inactive"
    elif email_user is not None:
        disposition, reason, user_id = "email_only_candidate", "email_only_candidate", email_user.id
        provenance["candidate_user_ids"] = [str(email_user.id)]
    elif email_blocked:
        disposition, reason, blocked_flag = "cross_tenant", "cross_tenant_subject", True
        provenance["candidate_user_ids"] = []
    elif not record["open_ids"] and not record["user_ids"] and not record["union_ids"]:
        disposition, reason = "conflict", "missing_provider_subject"
    else:
        disposition, reason = "unregistered", "unregistered"
    rows.append(
        _row(
            object_kind="org_member",
            object_id=record["object_id"],
            disposition=disposition,
            reason=reason,
            user_id=user_id,
            blocked=blocked_flag,
            provenance=provenance,
        )
    )
    if len(matched_users) == 1 and disposition in {"trusted", "inactive_user"}:
        extra = _department_conflict_row(
            record,
            matched_users[0],
            departments_by_id,
            observed,
        )
        if extra:
            rows.append(extra)
    return rows


def _provider_ok(record, proven) -> bool:
    if proven is None or proven.tenant_id is None:
        return False
    provider_id = record.get("provider_id")
    return provider_id is None or provider_id == proven.id


def _local_identity_conflict(matched_users, record, identity_by_subject):
    """Every incoming subject is checked in its own ExternalIdentity namespace."""

    if len(matched_users) != 1:
        return False, None
    user = matched_users[0]
    same = None
    for kind, field in (
        ("provider_open_id", "open_ids"),
        ("provider_user_id", "user_ids"),
        ("provider_union_id", "union_ids"),
    ):
        for value in record[field]:
            for identity in identity_by_subject.get((kind, value), []):
                if identity.user_id != user.id:
                    return True, None
                same = identity
    return False, same


def _foreign_identity(record, blocked) -> bool:
    pairs = (
        ("ext_open", record["open_ids"]),
        ("ext_user", record["user_ids"]),
        ("ext_union", record["union_ids"]),
    )
    return any((kind, value) in blocked for kind, values in pairs for value in values)


def _department_conflict_row(record, user, departments_by_id, observed) -> dict | None:
    if len(record["department_ids"]) > 1 or len(record["department_names"]) > 1:
        reason = "multiple_directory_departments"
    elif user.department_id is None:
        return None
    elif user.department_id not in departments_by_id:
        reason = "department_id_missing"
    else:
        current_name = departments_by_id[user.department_id].name
        directory_name = record.get("department_name")
        if not directory_name or current_name == directory_name:
            return None
        reason = "department_id_points_elsewhere"
    return _row(
        object_kind="org_member",
        object_id=record["object_id"],
        disposition="department_conflict",
        reason=reason,
        user_id=user.id,
        blocked=False,
        provenance={
            "source": "dry_run",
            "observed_at": observed,
            "origin": record["origin"],
            "applied": False,
            "department_name": record.get("department_name"),
        },
    )
