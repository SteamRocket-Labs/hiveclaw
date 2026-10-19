"""Company directory HTTP entry, persisted provenance, and explicit account repair.

Provider HTTP is controlled; application routes, migrations, audit, and RLS use
native PostgreSQL. These tests do not establish real provider acceptance.
"""

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.api import tenant_channels
from app.database import pin_rls_tenant_context
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from tests.integration.test_m1d_department_cutover_postgres import _app


@pytest.fixture
async def company_directory(owner_sessionmaker, app_user_sessionmaker):
    tenant, foreign, admin, account = uuid4(), uuid4(), uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add_all([Tenant(id=tenant, name="Directory company", slug=tenant.hex),
            Tenant(id=foreign, name="Foreign company", slug=foreign.hex)])
        await db.flush()
        actor = User(id=admin, tenant_id=tenant, username=admin.hex, email=f"{admin.hex}@fixture.test",
            password_hash="synthetic", display_name="Directory admin", role="org_admin")
        db.add_all([actor, User(id=account, tenant_id=tenant, username=account.hex, email=f"same-{tenant.hex}@fixture.test",
            password_hash="preserve-password", display_name="Existing person", role="member"),
            TenantChannelConfig(tenant_id=tenant, channel_type="dingtalk", app_id="directory-app",
                app_secret="synthetic-directory-secret", extra_config={}),
            TenantChannelConfig(tenant_id=tenant, channel_type="wecom", app_id="directory-corp",
                app_secret="synthetic-corp-secret", extra_config={})])
        await db.commit()
    app = _app(app_user_sessionmaker, actor)
    app.include_router(tenant_channels.router, prefix="/api")
    return {"tenant": tenant, "foreign": foreign, "admin": admin, "account": account,
        "actor": actor, "app": app}


def _provider(request, state):
    path = request.url.path
    state["calls"].append(path)
    if path.endswith("gettoken"):
        return httpx.Response(200, json={"errcode": 0, "access_token": "synthetic-directory-token"})
    assert request.url.params["access_token"] == "synthetic-directory-token"
    if state.get("empty") and path.endswith("/user/list"):
        return httpx.Response(200, json={"errcode": 0, "userlist": []})
    if state.get("fail") and path.endswith(("/user/list", "/department/listsub")):
        return httpx.Response(200, json={"errcode": 60011, "errmsg": "synthetic-directory-secret synthetic-directory-token"})
    if request.url.host == "oapi.dingtalk.com":
        body = json.loads(request.content)
        if path.endswith("/department/get"):
            return httpx.Response(200, json={"errcode": 0, "result": {"dept_id": 1, "name": "Ding root", "parent_id": 0}})
        if path.endswith("/department/listsub"):
            children = [{"dept_id": "invalid" if state.get("invalid_department") else 2, "name": "Ding team", "parent_id": 1}] if body["dept_id"] == 1 else []
            return httpx.Response(200, json={"errcode": 0, "result": children})
        assert path.endswith("/user/list"), path
        more = body["dept_id"] == 1 and body["cursor"] == 0
        if state.get("loop"):
            return httpx.Response(200, json={"errcode": 0, "result": {"has_more": True, "next_cursor": 0, "list": []}})
        people = [{"userid": "same-id", "name": "Ding person", "email": state.get("email", "same@fixture.test"), "dept_id_list": [1, 2]}]
        return httpx.Response(200, json={"errcode": 0, "result": {"has_more": more, "next_cursor": 100,
            "list": people if body["cursor"] == 0 else [{"userid": "second-id", "name": "Second person", "dept_id_list": [1]}]}})
    assert request.url.host == "qyapi.weixin.qq.com"
    if path.endswith("/department/list"):
        return httpx.Response(200, json={"errcode": 0, "department": [
            {"id": 1, "name": "WeCom root", "parentid": 0}, {"id": 2, "name": "WeCom team", "parentid": 1}]})
    assert path.endswith("/user/list"), path
    return httpx.Response(200, json={"errcode": 0, "userlist": [
        {"userid": "same-id", "name": "WeCom person", "email": state.get("email", "same@fixture.test"), "department": [1, 2], "status": 1}]})


async def test_sync_visible_directories_and_repair_without_account_rewrite(
    company_directory, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    seed = company_directory
    from app.services import workspace_sync
    original_projection = workspace_sync.sync_org_structure

    async def publish_committed_directory(db, tenant_id):
        async with owner_sessionmaker() as reader:
            visible = list(await reader.scalars(select(OrgMember).where(OrgMember.tenant_id == tenant_id)))
            assert visible, "Workspace publication must read durable directory truth"
        await original_projection(db, tenant_id)

    monkeypatch.setattr(workspace_sync, "sync_org_structure", publish_committed_directory)
    original = httpx.AsyncClient
    state = {"calls": [], "email": f"same-{seed['tenant'].hex}@fixture.test"}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**{**kw, "transport": httpx.MockTransport(lambda r: _provider(r, state))}))
    async with original(transport=httpx.ASGITransport(app=seed["app"]), base_url="http://directory") as api:
        for provider, count in (("dingtalk", 2), ("wecom", 1)):
            response = await api.post(f"/api/enterprise/org/sync?provider={provider}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "complete", body
            assert (body["provider"], body["coverage"], body["members"]) == (provider, "application_visible", count)
            again = await api.post(f"/api/enterprise/org/sync?provider={provider}")
            assert again.json()["status"] == "complete", again.text
        calls = len(state["calls"])
        wrong_requester = await api.post(f"/api/enterprise/org/sync?provider=dingtalk&expected_user_id={uuid4()}")
        assert wrong_requester.status_code == 409 and len(state["calls"]) == calls
        departments = (await api.get("/api/enterprise/org/departments")).json()
        assert len(departments) == 4
        assert all(row["feishu_id"] is None for row in departments)
        assert len({row["provider_id"] for row in departments}) == 2
        members = (await api.get("/api/enterprise/org/members")).json()
        assert len(members) == 3
        assert all(row["department_path"] == "" for row in members if row["name"] != "Second person")
        identities = (await api.get("/api/enterprise/org/identity-links")).json()
        assert len(identities["members"]) == 3
        assert all(not row["valid"] and not row["links"] for row in identities["members"])
        async with owner_sessionmaker() as db:
            member = await db.scalar(select(OrgMember).where(OrgMember.tenant_id == seed["tenant"], OrgMember.name == "Ding person"))
            member_id, provider_id = member.id, member.provider_id
        linked = await api.post("/api/enterprise/org/identity-links", json={"org_member_id": str(member_id),
            "provider_id": str(provider_id), "user_id": str(seed["account"]), "subject_kind": "provider_user_id"})
        assert linked.status_code == 200 and linked.json()["applied"] is True, linked.text
        identities = (await api.get("/api/enterprise/org/identity-links")).json()
        assert next(row for row in identities["members"] if row["org_member_id"] == str(member_id))["valid"] is True
        state["empty"] = True
        retained = await api.post("/api/enterprise/org/sync?provider=wecom")
        assert retained.json()["members"] == 0, retained.text
        assert len((await api.get("/api/enterprise/org/members")).json()) == 3
        state.pop("empty")
        assert (await api.post("/api/enterprise/org/sync?provider=dingtalk")).json()["status"] == "complete"
        identities = (await api.get("/api/enterprise/org/identity-links")).json()
        assert next(row for row in identities["members"] if row["org_member_id"] == str(member_id))["valid"] is True
        seed["actor"].role = "member"
        before = len(state["calls"])
        assert (await api.post("/api/enterprise/org/sync?provider=dingtalk")).status_code == 403
        assert len(state["calls"]) == before
        denied = await api.get(f"/api/enterprise/org/members?tenant_id={seed['foreign']}")
        assert denied.status_code == 403
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed["account"])
        assert (user.role, user.password_hash, user.department_id, user.feishu_user_id) == ("member", "preserve-password", None, None)
        assert len(list((await db.scalars(select(User).where(User.tenant_id == seed["tenant"]))))) == 2
        people = list(await db.scalars(select(OrgMember).where(OrgMember.tenant_id == seed["tenant"])))
        assert all(p.feishu_user_id is None and p.feishu_open_id is None for p in people)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["foreign"])
        assert not list(await db.scalars(select(OrgMember).where(OrgMember.tenant_id == seed["tenant"])))
        assert not list(await db.scalars(select(OrgDepartment).where(OrgDepartment.tenant_id == seed["tenant"])))
        assert await db.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")) is False


@pytest.mark.parametrize("provider,mode", [("dingtalk", "fail"), ("dingtalk", "loop"), ("wecom", "fail"), ("dingtalk", "invalid_department")])
async def test_failed_snapshot_writes_nothing_and_can_retry(company_directory, owner_sessionmaker, monkeypatch, provider, mode):
    seed, original = company_directory, httpx.AsyncClient
    state = {"calls": [], mode: True}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**{**kw, "transport": httpx.MockTransport(lambda r: _provider(r, state))}))
    async with original(transport=httpx.ASGITransport(app=seed["app"]), base_url="http://directory") as api:
        failed = await api.post(f"/api/enterprise/org/sync?provider={provider}")
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "unavailable" and failed.json()["applied"] == 0, failed.text
        assert "synthetic-directory-secret" not in failed.text and "synthetic-directory-token" not in failed.text
        async with owner_sessionmaker() as db:
            assert not list(await db.scalars(select(OrgDepartment).where(OrgDepartment.tenant_id == seed["tenant"])))
            assert not list(await db.scalars(select(OrgMember).where(OrgMember.tenant_id == seed["tenant"])))
            assert not list(await db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == seed["tenant"])))
        state.pop(mode)
        retried = await api.post(f"/api/enterprise/org/sync?provider={provider}")
        assert retried.json()["status"] == "complete", retried.text
        configs = (await api.get("/api/tenant-channels", params={"expected_tenant_id": str(seed["tenant"]), "expected_user_id": str(seed["admin"])})).json()
        config = next(c for c in configs if c["channel_type"] == provider)
        update = await api.put(f"/api/tenant-channels/{provider}", json={
            "app_id": config["app_id"], "app_secret": "synthetic-rotated-secret", "expected_revision": config["revision"],
            "expected_tenant_id": str(seed["tenant"]), "expected_user_id": str(seed["admin"])})
        assert update.status_code == 200, update.text
        assert (await api.post(f"/api/enterprise/org/sync?provider={provider}")).json()["status"] == "complete"
        changed = await api.put(f"/api/tenant-channels/{provider}", json={
            "app_id": "different-namespace", "app_secret": "synthetic-new-app-secret", "expected_revision": update.json()["revision"],
            "expected_tenant_id": str(seed["tenant"]), "expected_user_id": str(seed["admin"])})
        assert changed.status_code == 200, changed.text
        calls = len(state["calls"])
        blocked = await api.post(f"/api/enterprise/org/sync?provider={provider}")
        assert blocked.json()["code"] == "directory_namespace_changed", blocked.text
        assert len(state["calls"]) == calls
        paused = await api.put(f"/api/tenant-channels/{provider}", json={
            "app_id": "different-namespace", "is_active": False, "expected_revision": changed.json()["revision"],
            "expected_tenant_id": str(seed["tenant"]), "expected_user_id": str(seed["admin"])})
        assert paused.status_code == 200
        assert (await api.post(f"/api/enterprise/org/sync?provider={provider}")).json()["code"] == "directory_connection_inactive"
        assert len(state["calls"]) == calls


async def test_equal_subjects_in_two_companies_keep_separate_provenance(company_directory, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    seed, original = company_directory, httpx.AsyncClient
    state = {"calls": []}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**{**kw, "transport": httpx.MockTransport(lambda r: _provider(r, state))}))
    async with owner_sessionmaker() as db:
        other = User(tenant_id=seed["foreign"], username=uuid4().hex, email=f"{uuid4().hex}@fixture.test",
            password_hash="synthetic", display_name="Foreign directory admin", role="org_admin")
        db.add_all([other, TenantChannelConfig(tenant_id=seed["foreign"], channel_type="wecom",
            app_id="another-company", app_secret="synthetic-other-company-secret", extra_config={})])
        await db.commit()
    foreign_app = _app(app_user_sessionmaker, other)
    ids = []
    for app in (seed["app"], foreign_app):
        async with original(transport=httpx.ASGITransport(app=app), base_url="http://directory") as api:
            response = await api.post("/api/enterprise/org/sync?provider=wecom")
            assert response.json()["status"] == "complete", response.text
            people = (await api.get("/api/enterprise/org/members")).json()
            assert len(people) == 1
            ids.append(people[0]["id"])
    assert ids[0] != ids[1]
    async with owner_sessionmaker() as db:
        providers = list(await db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id.in_([seed["tenant"], seed["foreign"]]))))
        assert len(providers) == 2 and providers[0].id != providers[1].id
        assert all(p.sso_login_enabled is False and set(p.config) == {"connection_id", "app_id"} for p in providers)
    from app.services.workspace_sync import _enterprise_dir_async
    projection = (await _enterprise_dir_async(seed["tenant"])) / "org_structure.md"
    assert "WeCom person" in projection.read_text() and "WeCom team" in projection.read_text()
