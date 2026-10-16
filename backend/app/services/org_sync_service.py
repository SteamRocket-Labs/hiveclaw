"""Feishu organization structure sync service.

Pulls departments and members from Feishu Contact API and upserts into local DB.
"""

import uuid
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_scoped_session
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.auth_provider import FeishuIdentityConflict, feishu_auth_provider
from app.services.identity_links import (
    IdentityLinkRejected,
    apply_observed_subject_link,
    begin_deferred_identity_audit,
    commit_deferred_identity_audit,
    discard_deferred_identity_audit,
    identity_audit_mark,
    lock_directory_sync,
    rewind_deferred_identity_audit,
)

FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_DEPT_CHILDREN_URL = "https://open.feishu.cn/open-apis/contact/v3/departments"
FEISHU_USERS_URL = "https://open.feishu.cn/open-apis/contact/v3/users/find_by_department"


class _SyncUnavailable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _SkipRow(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_CONFLICT_DISPOSITIONS = {"conflict", "email_only_candidate", "cross_tenant"}
_SKIPPED_MEMBER_DISPOSITIONS = _CONFLICT_DISPOSITIONS | {"inactive_user"}


def _sync_unavailable(code: str, message: str) -> dict:
    return {
        "status": "unavailable",
        "code": code,
        "error": message,
        "applied": 0,
        "skipped": 0,
        "conflicts": 0,
        "departments": 0,
        "members": 0,
        "users_created": 0,
        "rows": [],
    }


def observed_directory_department_ids(item: dict) -> list[str]:
    """Every department id on one provider person, including a single-object list."""

    found: list[str] = []
    for field in ("department_id", "open_department_ids", "department_ids"):
        raw = item.get(field)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            text = str(value or "").strip()
            if text and text not in found:
                found.append(text)
    return found


def _index_snapshot_people(members: list[dict]) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for item in members:
        keys = []
        for kind, field in (("open", "open_id"), ("user", "user_id"), ("union", "union_id")):
            value = str(item.get(field) or "").strip()
            if value:
                keys.append((kind, value))
        person = next((index[key] for key in keys if key in index), None)
        if person is None:
            person = {
                "open_ids": [],
                "user_ids": [],
                "union_ids": [],
                "department_ids": [],
                "name": "",
                "email": "",
                "avatar_url": "",
                "title": "",
                "phone": "",
            }
        for key in keys:
            index[key] = person
        for field, bucket in (("open_id", "open_ids"), ("user_id", "user_ids"), ("union_id", "union_ids")):
            value = str(item.get(field) or "").strip()
            if value and value not in person[bucket]:
                person[bucket].append(value)
        for department_id in observed_directory_department_ids(item):
            if department_id not in person["department_ids"]:
                person["department_ids"].append(department_id)
        for field in ("name", "email", "avatar_url", "title", "phone"):
            value = str(item.get(field) or "").strip()
            if value and not person[field]:
                person[field] = value
    return index


def _person_for(index: dict[tuple[str, str], dict], open_id: str, user_id: str, union_id: str) -> dict:
    for key in (("user", user_id), ("open", open_id), ("union", union_id)):
        if key[1] and key in index:
            return index[key]
    return {"name": "", "email": "", "avatar_url": "", "title": "", "phone": ""}


def _observed_department_ids(people: dict[tuple[str, str], dict], provenance: dict) -> list[str]:
    """Keep every department observation for one person, including the root id."""

    for kind, field in (("user", "user_ids"), ("open", "open_ids"), ("union", "union_ids")):
        for value in provenance.get(field) or []:
            person = people.get((kind, str(value).strip()))
            if person and person.get("department_ids"):
                return [str(item) for item in person["department_ids"] if item]
    return [str(value) for value in provenance.get("department_ids") or [] if value]


def _one_subject(values) -> list[str]:
    found = [str(value).strip() for value in values or [] if str(value).strip()]
    if len(found) > 1:
        raise _SkipRow("ambiguous_subject")
    return found


def _keep_subject(current: str | None, incoming: str) -> str | None:
    value = incoming.strip()
    if not value or current in (None, "", value):
        return value or current
    return current


def _sync_profile(person: dict, open_id: str, user_id: str, union_id: str) -> dict:
    return {
        "user_id": user_id or None,
        "open_id": open_id or None,
        "union_id": union_id or None,
        "name": person.get("name") or "",
        "email": person.get("email") or "",
        "avatar_url": person.get("avatar_url") or "",
        "mobile": person.get("phone") or "",
        "raw_profile": {"source": "org_sync"},
    }


def _member_name(person: dict, user_id: str, open_id: str) -> str:
    return (person.get("name") or user_id or open_id or "Directory member")[:100]


def _fill_existing_user(user: User, person: dict, open_id: str, user_id: str, union_id: str) -> None:
    if person.get("name") and not (user.display_name or "").strip():
        user.display_name = person["name"][:100]
    if person.get("avatar_url") and not user.avatar_url:
        user.avatar_url = person["avatar_url"][:500]
    user.feishu_open_id = _keep_subject(user.feishu_open_id, open_id)
    user.feishu_user_id = _keep_subject(user.feishu_user_id, user_id)
    user.feishu_union_id = _keep_subject(user.feishu_union_id, union_id)


def _fill_member(
    member: OrgMember,
    provider: IdentityProvider,
    person: dict,
    open_id: str,
    user_id: str,
    union_id: str,
    department_ids: list[str],
    departments: dict[str, OrgDepartment],
    now: datetime,
) -> None:
    if member.provider_id not in (None, provider.id):
        raise _SkipRow("provider_namespace")
    member.provider_id = provider.id
    if person.get("name"):
        member.name = person["name"][:100]
    if person.get("email"):
        member.email = person["email"][:200]
    if person.get("avatar_url"):
        member.avatar_url = person["avatar_url"][:500]
    if person.get("title"):
        member.title = person["title"][:200]
    if person.get("phone"):
        member.phone = person["phone"][:50]
    member.feishu_open_id = _keep_subject(member.feishu_open_id, open_id)
    member.open_id = _keep_subject(member.open_id, open_id)
    member.feishu_user_id = _keep_subject(member.feishu_user_id, user_id)
    member.external_id = _keep_subject(member.external_id, user_id)
    member.unionid = _keep_subject(member.unionid, union_id)
    real_departments: list[str] = []
    for value in department_ids:
        text = str(value or "").strip()
        if text and text != "0" and text not in real_departments:
            real_departments.append(text)
    if len(real_departments) == 1 and real_departments[0] in departments:
        department = departments[real_departments[0]]
        member.department_id = department.id
        member.department_path = (department.path or department.name or "")[:500]
        member.observed_directory_department_ids = list(real_departments)
    elif len(real_departments) > 1:
        member.observed_directory_department_ids = list(real_departments)
    member.synced_at = now


class OrgSyncService:
    """Sync org structure from Feishu into local database."""

    async def _get_feishu_config(self, db: AsyncSession, tenant_id: uuid.UUID) -> dict | None:
        """Load Feishu org sync config from tenant settings."""
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == "feishu_org_sync",
            )
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def _get_app_token(self, app_id: str, app_secret: str) -> tuple[str, dict]:
        """Get Feishu tenant_access_token.
        Returns (token_string, raw_response_dict).
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            data = resp.json()
            code = data.get("code") if isinstance(data, dict) else None
            logger.info("[OrgSync] Token response: code={}", code if isinstance(code, int) else "invalid")
            token = data.get("tenant_access_token") or data.get("app_access_token") or ""
            return token, data

    async def full_sync(
        self,
        tenant_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        fetcher=None,
    ) -> dict:
        """Apply one complete directory snapshot, then commit it as a single transaction."""

        try:
            async with tenant_scoped_session(tenant_id) as db:
                await lock_directory_sync(db, tenant_id)
                snapshot = await self.fetch_pending_directory(db, tenant_id, fetcher=fetcher)
                provider = await self._sync_provider(db, tenant_id)
                from app.services.identity_dry_run import build_identity_dry_run_report

                report = await build_identity_dry_run_report(
                    db,
                    tenant_id=tenant_id,
                    snapshot=snapshot,
                    actor_id=actor_id,
                    source_revision="org_sync",
                )
                return await self._apply_directory(db, tenant_id, snapshot, report, provider, actor_id)
        except DirectoryFetchError as exc:
            return _sync_unavailable(exc.code, exc.message)
        except _SyncUnavailable as exc:
            return _sync_unavailable(exc.code, exc.message)

    async def _sync_provider(self, db: AsyncSession, tenant_id: uuid.UUID) -> IdentityProvider:
        providers = (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.provider_type == "feishu",
                    IdentityProvider.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        if len(providers) > 1:
            raise _SyncUnavailable(
                "provider_unproven",
                "Feishu directory sync does not have one company provider. Nothing was saved.",
            )
        if len(providers) == 1:
            provider = providers[0]
            if provider.is_active is not True:
                raise _SyncUnavailable(
                    "provider_disabled",
                    "Feishu directory sync is disabled for this company. Nothing was saved.",
                )
            return provider
        config = await self._get_feishu_config(db, tenant_id) or {}
        provider = IdentityProvider(
            provider_type="feishu",
            name="Feishu",
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant_id,
            config={
                "app_id": config.get("app_id"),
                "app_secret": config.get("app_secret"),
                "platform_region": config.get("platform_region") or "feishu_cn",
                "open_api_domain": config.get("open_api_domain") or "",
                "oauth_authorize_url": config.get("oauth_authorize_url") or "",
                "source": "tenant_setting",
            },
        )
        db.add(provider)
        await db.flush()
        return provider

    async def _apply_directory(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        snapshot: dict,
        report: dict,
        provider: IdentityProvider,
        actor_id: uuid.UUID | None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        departments_written = await self._apply_departments(db, tenant_id, snapshot, report, now)
        stored_departments = {
            department.feishu_id: department
            for department in (
                await db.execute(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id))
            ).scalars().all()
            if department.feishu_id
        }
        people = _index_snapshot_people(snapshot["members"])
        applied = skipped = conflicts = created = 0
        outcomes: list[dict] = []
        seen: set[str] = set()
        seen_subjects: set[tuple[str, str]] = set()
        begin_deferred_identity_audit(db)
        try:
            for row in report["rows"]:
                if row.get("object_kind") != "org_member" or row.get("disposition") == "department_conflict":
                    continue
                provenance = row.get("provenance") or {}
                if provenance.get("origin") == "stored":
                    continue
                object_id = str(row["object_id"])
                if object_id in seen:
                    continue
                disposition = str(row.get("disposition") or "")
                subject_keys = [
                    (kind, str(value).strip())
                    for kind, field in (("user", "user_ids"), ("open", "open_ids"), ("union", "union_ids"))
                    for value in provenance.get(field) or []
                    if str(value).strip()
                ]
                if disposition in {"trusted", "unregistered"} and any(key in seen_subjects for key in subject_keys):
                    continue
                seen.add(object_id)
                seen_subjects.update(subject_keys)
                reason = str(row.get("reason") or "")
                department_ids = _observed_department_ids(people, provenance)
                outcome = {
                    "object_id": object_id,
                    "disposition": disposition,
                    "reason": reason,
                    "applied": False,
                    "department_ids": department_ids,
                }
                if disposition in _SKIPPED_MEMBER_DISPOSITIONS:
                    skipped += 1
                    if disposition in _CONFLICT_DISPOSITIONS:
                        conflicts += 1
                    outcomes.append(outcome)
                    continue
                if disposition not in {"trusted", "unregistered"}:
                    skipped += 1
                    outcomes.append(outcome)
                    continue
                mark = identity_audit_mark(db)
                try:
                    async with db.begin_nested():
                        created_user = await self._apply_member_row(
                            db,
                            tenant_id=tenant_id,
                            row=row,
                            people=people,
                            provider=provider,
                            departments=stored_departments,
                            department_ids=department_ids,
                            actor_id=actor_id,
                            now=now,
                        )
                except (_SkipRow, IntegrityError, IdentityLinkRejected, FeishuIdentityConflict) as exc:
                    rewind_deferred_identity_audit(db, mark)
                    skipped += 1
                    conflicts += 1
                    outcome["disposition"] = "conflict"
                    outcome["reason"] = getattr(exc, "code", None) or "global_unique_collision"
                    outcomes.append(outcome)
                    continue
                applied += 1
                created += 1 if created_user else 0
                outcome["applied"] = True
                outcomes.append(outcome)
            from app.services.identity_cutover import apply_user_department_cutover

            withheld: dict[uuid.UUID, str | None] = {}
            for row in report["rows"]:
                if row.get("object_kind") != "org_member" or row.get("disposition") != "department_conflict":
                    continue
                if row.get("reason") != "multiple_directory_departments":
                    continue
                try:
                    member_id = uuid.UUID(str(row.get("object_id")))
                except (TypeError, ValueError):
                    continue
                user_ref = row.get("user_id")
                withheld[member_id] = str(user_ref) if user_ref else None
            assignment = await apply_user_department_cutover(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                org_member_ids=None,
                withheld_members=withheld,
            )
            await self._mark_synced(db, tenant_id, now)
            await commit_deferred_identity_audit(db)
            return {
                "status": "complete",
                "departments": departments_written,
                "members": applied + skipped,
                "applied": applied,
                "skipped": skipped,
                "conflicts": conflicts,
                "users_created": created,
                "user_assignments": assignment["counts"]["user_assignments"]["changed"],
                "assignment_counts": assignment["counts"]["user_assignments"],
                "assignment_rows": assignment["rows"],
                "synced_at": now.isoformat(),
                "rows": outcomes,
            }
        finally:
            discard_deferred_identity_audit(db)

    async def _mark_synced(self, db: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> None:
        setting = (
            await db.execute(
                select(TenantSetting).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "feishu_org_sync",
                )
            )
        ).scalar_one_or_none()
        if setting is not None:
            setting.value = {**dict(setting.value or {}), "last_synced_at": now.isoformat()}

    async def _apply_departments(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        snapshot: dict,
        report: dict,
        now: datetime,
    ) -> int:
        blocked: set[str] = set()
        for row in report["rows"]:
            if row.get("object_kind") != "org_department":
                continue
            if row.get("disposition") not in {"cross_tenant", "conflict"}:
                continue
            feishu_id = str((row.get("provenance") or {}).get("provider_department_id") or "").strip()
            if feishu_id:
                blocked.add(feishu_id)
        written = 0
        for item in snapshot["departments"]:
            feishu_id = str(item.get("open_department_id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not feishu_id or not name or feishu_id in blocked:
                continue
            try:
                async with db.begin_nested():
                    department = (
                        await db.execute(
                            select(OrgDepartment).where(
                                OrgDepartment.tenant_id == tenant_id,
                                OrgDepartment.feishu_id == feishu_id,
                            )
                        )
                    ).scalar_one_or_none()
                    count = _directory_member_count(item)
                    if department is None:
                        db.add(
                            OrgDepartment(
                                feishu_id=feishu_id,
                                name=name[:200],
                                path=name[:500],
                                member_count=count,
                                tenant_id=tenant_id,
                                synced_at=now,
                            )
                        )
                    else:
                        department.name = name[:200]
                        department.member_count = count
                        department.synced_at = now
            except IntegrityError:
                continue
            written += 1
        await db.flush()
        stored = {
            department.feishu_id: department
            for department in (
                await db.execute(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id))
            ).scalars().all()
            if department.feishu_id
        }
        for item in snapshot["departments"]:
            feishu_id = str(item.get("open_department_id") or "").strip()
            parent_id = str(item.get("parent_department_id") or "").strip()
            department = stored.get(feishu_id)
            parent = stored.get(parent_id)
            if department is not None and parent is not None and parent.id != department.id:
                department.parent_id = parent.id
        await db.flush()
        return written

    async def _apply_member_row(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        row: dict,
        people: dict[tuple[str, str], dict],
        provider: IdentityProvider,
        departments: dict[str, OrgDepartment],
        department_ids: list[str],
        actor_id: uuid.UUID | None,
        now: datetime,
    ) -> bool:
        provenance = row.get("provenance") or {}
        open_ids = _one_subject(provenance.get("open_ids"))
        user_ids = _one_subject(provenance.get("user_ids"))
        union_ids = _one_subject(provenance.get("union_ids"))
        open_id = open_ids[0] if open_ids else ""
        user_id = user_ids[0] if user_ids else ""
        union_id = union_ids[0] if union_ids else ""
        if not open_id and not user_id and not union_id:
            raise _SkipRow("missing_provider_subject")
        person = _person_for(people, open_id, user_id, union_id)
        member = await self._load_member(db, tenant_id, str(row["object_id"]), open_id, user_id)
        created_user = False
        if row["disposition"] == "trusted":
            account_id = row.get("user_id")
            user = await db.get(User, uuid.UUID(str(account_id))) if account_id else None
            if user is None or user.tenant_id != tenant_id:
                raise _SkipRow("cross_tenant")
            if user.is_active is not True:
                raise _SkipRow("inactive_user")
            _fill_existing_user(user, person, open_id, user_id, union_id)
        else:
            user = await feishu_auth_provider._create_user(
                db,
                tenant_id,
                _sync_profile(person, open_id, user_id, union_id),
            )
            created_user = True
        if member is None:
            member = OrgMember(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                provider_id=provider.id,
                name=_member_name(person, user_id, open_id),
                status="active",
                synced_at=now,
            )
            db.add(member)
        _fill_member(
            member,
            provider,
            person,
            open_id,
            user_id,
            union_id,
            department_ids,
            departments,
            now,
        )
        prepared = False

        async def persist_identity() -> None:
            nonlocal prepared
            if prepared:
                return
            prepared = True
            await feishu_auth_provider._upsert_external_identity(
                db,
                provider,
                user,
                _sync_profile(person, open_id, user_id, union_id),
            )

        for kind, value in (
            ("provider_open_id", open_id),
            ("provider_user_id", user_id),
            ("provider_union_id", union_id),
        ):
            if not value:
                continue
            outcome = await apply_observed_subject_link(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                member=member,
                user=user,
                provider=provider,
                subject_kind=kind,
                source="org_sync",
                after_lock=persist_identity,
            )
            if not outcome.applied:
                raise _SkipRow(outcome.code or "conflict")
        return created_user

    async def _load_member(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        object_id: str,
        open_id: str,
        user_id: str,
    ) -> OrgMember | None:
        try:
            member_id = uuid.UUID(object_id)
        except ValueError:
            member_id = None
        if member_id is not None:
            member = await db.get(OrgMember, member_id)
            if member is not None and member.tenant_id == tenant_id:
                return member
        if user_id:
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.feishu_user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if member is not None:
                return member
        if open_id:
            return (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.feishu_open_id == open_id,
                    )
                )
            ).scalar_one_or_none()
        return None


    async def fetch_pending_directory(self, db: AsyncSession, tenant_id: uuid.UUID, *, fetcher=None) -> dict:
        """Read the configured directory without writing rows or the secret.

        ``fetcher`` is the synthetic adapter seam. Production leaves it empty
        and uses the read-only Feishu calls below. A missing setting or a
        partial response raises ``DirectoryFetchError`` instead of returning
        an empty success.
        """

        config = await self._get_feishu_config(db, tenant_id)
        app_id = str((config or {}).get("app_id") or "").strip()
        app_secret = str((config or {}).get("app_secret") or "").strip()
        if not app_id or not app_secret:
            raise DirectoryFetchError("missing_config", MISSING_CONFIG_MESSAGE)
        if fetcher is not None:
            snapshot = await fetcher(app_id, app_secret)
        else:
            snapshot = await self._fetch_directory_readonly(app_id, app_secret)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("complete") is not True
            or not isinstance(snapshot.get("departments"), list)
            or not isinstance(snapshot.get("members"), list)
        ):
            raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
        return snapshot

    async def _fetch_directory_readonly(self, app_id: str, app_secret: str) -> dict:
        try:
            token, token_resp = await self._get_app_token(app_id, app_secret)
        except Exception:
            logger.warning("[IdentityDryRun] token request failed")
            raise DirectoryFetchError("provider_unavailable", UNAVAILABLE_MESSAGE) from None
        code = token_resp.get("code") if isinstance(token_resp, dict) else "invalid"
        if not token or code not in (0, None):
            logger.warning(
                "[IdentityDryRun] token unavailable code={}",
                code if isinstance(code, int) and not isinstance(code, bool) else "invalid",
            )
            raise DirectoryFetchError("provider_unavailable", UNAVAILABLE_MESSAGE)
        departments = []
        for item in await self._collect_directory_pages(
            token,
            f"{FEISHU_DEPT_CHILDREN_URL}/0/children",
            {
                "department_id_type": "open_department_id",
                "page_size": "50",
                "fetch_child": "true",
            },
        ):
            projected = _project_directory_department(item)
            if projected is None:
                raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
            departments.append(projected)
        # The requested root is a user scope even when it has children.
        # https://github.com/larksuite/oapi-sdk-python-demo/blob/main/composite_api/contact/list_user_by_department.py
        department_ids = ["0"]
        for item in departments:
            if item["open_department_id"] not in department_ids:
                department_ids.append(item["open_department_id"])
        members: list[dict] = []
        for department_id in department_ids:
            for item in await self._collect_directory_pages(
                token,
                FEISHU_USERS_URL,
                {
                    "department_id_type": "open_department_id",
                    "department_id": department_id,
                    "page_size": "50",
                },
            ):
                members.append(_project_directory_member(item, department_id))
        return {"complete": True, "departments": departments, "members": members}

    async def _collect_directory_pages(self, token: str, url: str, params: dict) -> list[dict]:
        items: list[dict] = []
        page_token = ""
        seen: set[str] = set()
        while True:
            query = dict(params)
            if page_token:
                if page_token in seen:
                    raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
                seen.add(page_token)
                query["page_token"] = page_token
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(
                        url,
                        params=query,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    data = response.json()
            except Exception:
                logger.warning("[IdentityDryRun] directory page failed")
                raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE) from None
            if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
                observed = data.get("code") if isinstance(data, dict) else None
                logger.warning(
                    "[IdentityDryRun] directory page rejected code={}",
                    observed if isinstance(observed, int) and not isinstance(observed, bool) else "invalid",
                )
                raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
            payload = data["data"]
            items.extend(_assessed_page_items(payload))
            if not payload.get("has_more"):
                return items
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)


class DirectoryFetchError(Exception):
    """The dry-run could not obtain a complete directory snapshot."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


MISSING_CONFIG_MESSAGE = (
    "Feishu directory sync is not configured for this company. Save the connection, then check again."
)
INCOMPLETE_MESSAGE = (
    "The directory snapshot did not finish. Nothing was checked or applied. Try again when the provider responds."
)
UNAVAILABLE_MESSAGE = (
    "The directory provider could not be reached. Nothing was checked or applied. Try again."
)


def _assessed_page_items(payload: dict) -> list[dict]:
    """Return every object on the page. Null, missing, and non-object items are incomplete."""

    if "items" not in payload:
        raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
    page_items = payload["items"]
    if page_items is None or not isinstance(page_items, list):
        raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
    if any(not isinstance(item, dict) for item in page_items):
        raise DirectoryFetchError("incomplete", INCOMPLETE_MESSAGE)
    return page_items


def _directory_member_count(item: dict) -> int:
    value = item.get("member_count", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def _project_directory_department(item: dict) -> dict | None:
    feishu_id = str(item.get("open_department_id") or "").strip()
    if not feishu_id:
        return None
    return {
        "open_department_id": feishu_id,
        "name": str(item.get("name") or "").strip(),
        "parent_department_id": str(item.get("parent_department_id") or "").strip(),
        "member_count": _directory_member_count(item),
    }


def _project_directory_member(item: dict, department_id: str) -> dict:
    avatar = item.get("avatar") if isinstance(item.get("avatar"), dict) else {}
    return {
        "open_id": str(item.get("open_id") or "").strip(),
        "user_id": str(item.get("user_id") or "").strip(),
        "union_id": str(item.get("union_id") or "").strip(),
        "email": str(item.get("email") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "department_id": department_id,
        "avatar_url": str(item.get("avatar_url") or avatar.get("avatar_origin") or "").strip(),
        "title": str(item.get("job_title") or item.get("title") or item.get("description") or "").strip()[:200],
        "phone": str(item.get("mobile") or item.get("phone") or "").strip(),
    }


def get_identity_directory_fetcher():
    """Production dependency. Tests override it with a synthetic adapter."""

    return None


org_sync_service = OrgSyncService()
