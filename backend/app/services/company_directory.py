"""Observe application-visible company directories without creating account authority.

Credentials stay in TenantChannelConfig. IdentityProvider records the immutable
connection namespace only; neither directory visibility nor email proves a login.
"""

from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy import write_audit_event
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant_channel_config import TenantChannelConfig
from app.services.identity_links import lock_directory_sync
from app.services.org_sync_service import DirectoryFetchError


def _invalid():
    raise DirectoryFetchError("directory_response_invalid", "The directory response is incomplete or inconsistent. Retry after checking the connection.")


def _subject(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip() or len(str(value)) > 100:
        _invalid()
    return str(value).strip()


def _field(value, limit):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        _invalid()
    return value


def _department_id(value):
    key = _subject(value)
    if not key.isascii() or not key.isdecimal():
        _invalid()
    return str(int(key))


def _objects(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _invalid()
    return value


async def _request(client, method, url, **kwargs):
    try:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        # Request URLs contain bearer tokens and must not enter diagnostics/logs.
        raise DirectoryFetchError("directory_transport_failed", "Directory transport failed. Check the connection and retry.") from None
    if not isinstance(data, dict) or type(data.get("errcode")) is not int:
        _invalid()
    if data["errcode"] != 0:
        raise DirectoryFetchError("directory_provider_rejected", f"The directory provider rejected the request (code {data['errcode']}). Check application permissions and retry.")
    return data


async def _fetch(config):
    provider = config.channel_type
    departments, members = {}, {}

    def department(item, id_key, parent_key):
        key = _department_id(item.get(id_key))
        parent = _department_id(item.get(parent_key))
        row = {"id": key, "parent": parent, "name": _field(item.get("name"), 200)}
        if not row["name"] or key == parent or key in departments and departments[key] != row:
            _invalid()
        departments[key] = row
        return key

    def person(item, department_key, current_department):
        key = _subject(item.get("userid"))
        ids = item.get(department_key)
        if not isinstance(ids, list) or not ids:
            _invalid()
        ids = set(map(_department_id, ids))
        if current_department not in ids:
            _invalid()
        row = {"name": _field(item.get("name") or key, 100), "email": _field(item.get("email"), 200),
            "title": _field(item.get("title") if provider == "dingtalk" else item.get("position"), 200),
            "avatar_url": _field(item.get("avatar"), 500), "phone": _field(item.get("mobile"), 50),
            "status": "inactive" if (provider == "wecom" and item.get("status") != 1)
                or (provider == "dingtalk" and item.get("active") is False) else "active"}
        previous = members.get(key)
        if previous is not None:
            if previous["profile"] != row:
                _invalid()
            ids |= previous["departments"]
        members[key] = {"profile": row, "departments": ids}

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        if provider == "dingtalk":
            base = "https://oapi.dingtalk.com"
            token = (await _request(client, "GET", f"{base}/gettoken",
                params={"appkey": config.app_id, "appsecret": config.app_secret})).get("access_token")
        else:
            base = "https://qyapi.weixin.qq.com/cgi-bin"
            token = (await _request(client, "GET", f"{base}/gettoken",
                params={"corpid": config.app_id, "corpsecret": config.app_secret})).get("access_token")
        if not isinstance(token, str) or not token:
            _invalid()
        params = {"access_token": token}
        if provider == "dingtalk":
            async def ding(path, body):
                return (await _request(client, "POST", f"{base}/topapi/v2/{path}", params=params, json=body)).get("result")

            root = await ding("department/get", {"dept_id": 1})
            if not isinstance(root, dict) or department(root, "dept_id", "parent_id") != "1":
                _invalid()
            queue, visited = ["1"], set()
            while queue:
                current = queue.pop()
                if current in visited:
                    _invalid()
                visited.add(current)
                children = _objects(await ding("department/listsub", {"dept_id": int(current)}))
                for item in children:
                    key = department(item, "dept_id", "parent_id")
                    if departments[key]["parent"] != current or key in visited or key in queue:
                        _invalid()
                    queue.append(key)
                cursor, cursors = 0, set()
                while True:
                    if cursor in cursors:
                        _invalid()
                    cursors.add(cursor)
                    page = await ding("user/list", {"dept_id": int(current), "cursor": cursor, "size": 100})
                    if not isinstance(page, dict) or type(page.get("has_more")) is not bool:
                        _invalid()
                    for item in _objects(page.get("list")):
                        person(item, "dept_id_list", current)
                    if not page["has_more"]:
                        break
                    cursor = page.get("next_cursor")
                    if type(cursor) is not int or cursor < 0:
                        _invalid()
        else:
            data = await _request(client, "GET", f"{base}/department/list", params=params)
            for item in _objects(data.get("department")):
                department(item, "id", "parentid")
            for key in departments:
                data = await _request(client, "GET", f"{base}/user/list", params={**params, "department_id": key})
                for item in _objects(data.get("userlist")):
                    person(item, "department", key)
    # Missing parents can be outside the application's visible directory scope.
    for key, row in departments.items():
        seen, names, current = set(), [], key
        while current in departments:
            if current in seen:
                _invalid()
            seen.add(current)
            names.append(departments[current]["name"])
            current = departments[current]["parent"]
        row["path"] = "/".join(reversed(names))
        if len(row["path"]) > 500:
            _invalid()
    return departments, members


async def sync_company_directory(db: AsyncSession, *, tenant_id: UUID, actor_id: UUID, provider_type: str):
    """Fetch first, apply one atomic observed snapshot, and retain unseen rows."""
    from app.api.tenant_channels import _lock_config

    if provider_type not in {"dingtalk", "wecom"}:
        raise ValueError("Unsupported company directory provider")
    await lock_directory_sync(db, tenant_id)
    await _lock_config(db, tenant_id, provider_type)
    try:
        config = await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == tenant_id,
            TenantChannelConfig.channel_type == provider_type).with_for_update())
        if config is None or not config.app_id or not config.app_secret:
            raise DirectoryFetchError("missing_config", "Save a company application connection before syncing its directory.")
        if not config.is_active:
            raise DirectoryFetchError("directory_connection_inactive", "The company connection is paused. Resume it before syncing.")
        namespace = {"connection_id": str(config.id), "app_id": config.app_id}
        provider = await db.scalar(select(IdentityProvider).where(IdentityProvider.tenant_id == tenant_id,
            IdentityProvider.provider_type == provider_type).with_for_update())
        if provider is not None:
            if not provider.is_active:
                raise DirectoryFetchError("directory_provider_inactive", "The identity provider is paused. Review it before syncing.")
            if provider.config != namespace:
                raise DirectoryFetchError("directory_namespace_changed", "The saved application differs from this directory's identity namespace. Restore the original connection or reconcile its identities before syncing.")
        departments, members = await _fetch(config)
        now = datetime.now(timezone.utc)
        async with db.begin_nested():
            if provider is None:
                provider = IdentityProvider(tenant_id=tenant_id, provider_type=provider_type,
                    name="DingTalk" if provider_type == "dingtalk" else "WeCom", is_active=True,
                    sso_login_enabled=False, config=namespace)
                db.add(provider)
                await db.flush()
            stored = list(await db.scalars(select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id,
                OrgDepartment.provider_id == provider.id)))
            mirrors = {d.provider_department_id: d for d in stored}
            for key, row in departments.items():
                mirror = mirrors.get(key)
                if mirror is None:
                    mirror = OrgDepartment(tenant_id=tenant_id, provider_id=provider.id, provider_department_id=key)
                    db.add(mirror)
                    mirrors[key] = mirror
                mirror.name, mirror.path, mirror.synced_at = row["name"], row["path"], now
                mirror.member_count = sum(key in p["departments"] for p in members.values())
            await db.flush()
            for key, row in departments.items():
                parent = mirrors.get(row["parent"]) if row["parent"] in departments else None
                mirrors[key].parent_id = parent.id if parent else None
            existing = list(await db.scalars(select(OrgMember).where(OrgMember.tenant_id == tenant_id,
                OrgMember.provider_id == provider.id)))
            people = {}
            for member in existing:
                if member.external_id in people:
                    raise DirectoryFetchError("directory_subject_conflict", "Stored people share a provider identity. Repair the conflicting records before syncing.")
                people[member.external_id] = member
            observed_members = []
            for key, row in members.items():
                member = people.get(key)
                if member is None:
                    member = OrgMember(tenant_id=tenant_id, provider_id=provider.id, external_id=key)
                    db.add(member)
                for field, value in row["profile"].items():
                    setattr(member, field, value)
                observed = sorted(row["departments"])
                member.observed_directory_department_ids = observed
                primary = mirrors.get(observed[0]) if len(observed) == 1 and observed[0] in departments else None
                member.department_id = primary.id if primary else None
                member.department_path = primary.path if primary else ""
                member.synced_at = now
                observed_members.append(member)
            await db.flush()
            await write_audit_event(db, tenant_id=tenant_id, actor_type="user", actor_id=actor_id,
                event_type="organization.directory_synced", action="organization.directory.sync",
                resource_type="identity_provider", resource_id=provider.id,
                details={"provider": provider_type, "coverage": "application_visible",
                    "departments": len(departments), "members": len(members), "unseen_rows": "retained"})
            from app.services.identity_cutover import apply_user_department_cutover
            assignments = await apply_user_department_cutover(db, tenant_id=tenant_id, actor_id=actor_id,
                org_member_ids=[member.id for member in observed_members])
        return {"status": "complete", "provider": provider_type, "coverage": "application_visible",
            "tenant_id": str(tenant_id),
            "assignment_counts": assignments["counts"]["user_assignments"],
            "assignment_rows": assignments["rows"],
            "departments": len(departments), "members": len(members), "applied": len(members),
            "skipped": 0, "conflicts": 0, "rows": []}
    except DirectoryFetchError as exc:
        return {"status": "unavailable", "provider": provider_type, "code": exc.code,
            "tenant_id": str(tenant_id),
            "error": exc.message, "applied": 0, "retryable": True, "rows": []}
