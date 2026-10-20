"""Shared memory uses the workspace's real audience inside the tenant."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.org import OrgMember  # noqa: F401
from tests.integration.test_m1g21_team_memory_receipts_postgres import body, memory_clients  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def team_memory(memory_clients, owner_sessionmaker):  # noqa: F811
    tenant, users, clients, root = memory_clients
    team_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(BusinessTeam(id=team_id, tenant_id=tenant, name="Release team", status="active",
            created_by_user_id=users[0].id))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=tenant, business_team_id=team_id,
            user_id=users[0].id, role="lead", status="active"))
        await db.commit()
    return tenant, users, clients, root, f"business_team:{team_id}"


@pytest.mark.parametrize("surface", ["list", "search", "detail", "write"])
async def test_same_company_nonmember_cannot_read_or_replace_team_memory(team_memory, surface):
    _tenant, _users, (writer, outsider), _root, workspace = team_memory
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    if surface == "write":
        response = await outsider.put("/enterprise/memory/shared", json=body("UNAUTHORIZED_REPLACEMENT", base_revision=1,
            workspace_key=workspace))
    else:
        suffix = {"list": "", "search": "/search", "detail": "/report"}[surface]
        response = await outsider.get("/enterprise/memory/shared" + suffix,
            params={"workspace_key": workspace, "query": "release"})
    assert response.status_code in {403, 404}, response.text
    assert "TEAM_ONLY_RELEASE_MARKER" not in response.text
    current = await writer.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert current.status_code == 200 and current.json()["content"] == "TEAM_ONLY_RELEASE_MARKER", current.text


async def change_membership(owner_sessionmaker, workspace, user_id, status):
    from uuid import UUID

    async with owner_sessionmaker() as db:
        membership = await db.scalar(select(BusinessTeamMembership).where(
            BusinessTeamMembership.business_team_id == UUID(workspace.split(":", 1)[1]),
            BusinessTeamMembership.user_id == user_id))
        membership.status = status
        await db.commit()


async def test_current_member_can_read_until_membership_is_withdrawn(team_memory, owner_sessionmaker):
    from uuid import UUID

    tenant, users, (writer, reader), _root, workspace = team_memory
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    async with owner_sessionmaker() as db:
        db.add(BusinessTeamMembership(tenant_id=tenant, business_team_id=UUID(workspace.split(":", 1)[1]),
            user_id=users[1].id, role="member", status="active"))
        await db.commit()
    available = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert available.status_code == 200 and "TEAM_ONLY_RELEASE_MARKER" in available.text
    await change_membership(owner_sessionmaker, workspace, users[1].id, "left")
    denied = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert denied.status_code == 404 and "TEAM_ONLY_RELEASE_MARKER" not in denied.text


@pytest.mark.parametrize("withdrawal,expected_status", [("membership", 404), ("company", 404)])
async def test_authority_withdrawn_during_semantic_review_cannot_commit_a_file(
    team_memory, owner_sessionmaker, monkeypatch, withdrawal, expected_status,
):
    import app.services.team_memory as storage

    tenant, users, (writer, _reader), root, workspace = team_memory
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    review = storage.prepare_memory_write_with_llm

    async def withdraw(*args, **kwargs):
        decision = await review(*args, **kwargs)
        if withdrawal == "membership":
            await change_membership(owner_sessionmaker, workspace, users[0].id, "left")
        else:
            from app.models.tenant import Tenant

            async with owner_sessionmaker() as db:
                (await db.get(Tenant, tenant)).is_active = False
                await db.commit()
        return decision

    monkeypatch.setattr(storage, "prepare_memory_write_with_llm", withdraw)
    changed = await writer.put("/enterprise/memory/shared", json=body("WITHDRAWN_WRITE_MARKER",
        workspace_key=workspace, base_revision=1))
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert changed.status_code == expected_status and before == after, changed.text


async def test_late_membership_withdrawal_hides_all_read_response_fields(team_memory, owner_sessionmaker, monkeypatch):
    import app.services.content_input_receipts as sources

    _tenant, users, (writer, _reader), _root, workspace = team_memory
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    validate = sources.validate_team_memory_receipt

    async def withdraw(*args, **kwargs):
        receipt = await validate(*args, **kwargs)
        await change_membership(owner_sessionmaker, workspace, users[0].id, "left")
        return receipt

    monkeypatch.setattr(sources, "validate_team_memory_receipt", withdraw)
    result = await writer.get("/enterprise/memory/shared", params={"workspace_key": workspace})
    assert result.status_code == 404 and "TEAM_ONLY_RELEASE_MARKER" not in result.text and "Weekly result" not in result.text


async def test_workspace_choices_include_only_current_memberships(team_memory):
    _tenant, _users, (writer, outsider), _root, workspace = team_memory
    visible = await writer.get("/enterprise/memory/shared-workspaces")
    hidden = await outsider.get("/enterprise/memory/shared-workspaces")
    assert visible.status_code == hidden.status_code == 200
    assert workspace in {row["workspace_key"] for row in visible.json()["workspaces"]}
    assert workspace not in {row["workspace_key"] for row in hidden.json()["workspaces"]}


async def test_unbound_legacy_memory_stays_preserved_and_is_not_adopted(team_memory):
    from app.services.team_memory import TeamMemoryStore

    tenant, users, (writer, _reader), root, _workspace = team_memory
    store = TeamMemoryStore(data_root=root)
    entry = await store.upsert_entry_async(tenant_id=str(tenant), workspace_key="workspace", key="legacy",
        title="Legacy source", content="LEGACY_SCOPE_MARKER", updated_by=str(users[0].id))
    from pathlib import Path

    path = Path(entry.absolute_path)
    original = path.read_bytes()
    read = await writer.get("/enterprise/memory/shared/legacy", params={"workspace_key": "workspace"})
    assert read.status_code == 409 and "team_memory_workspace_unbound" in read.text
    assert "LEGACY_SCOPE_MARKER" not in read.text and path.read_bytes() == original


@pytest.fixture
async def runtime_team(shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import memory
    from app.core.security import get_current_user
    from app.database import get_db, tenant_scoped_session
    from app.models.user import User
    from tests.api.test_memory_api import _review_team_memory_semantics

    _review_team_memory_semantics.__wrapped__(monkeypatch)
    s = shared_memory
    n = s.n
    team_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(BusinessTeam(id=team_id, tenant_id=n.tenant, name="Runtime team", created_by_user_id=n.user))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=n.tenant, business_team_id=team_id,
            user_id=n.user, role="lead", status="active"))
        await db.commit()

    async def database():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            yield db
            await db.commit()

    clients = []
    for user_id in (n.user, n.foreign):
        async def current(user_id=user_id):
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, user_id)

        app = FastAPI()
        app.include_router(memory.router)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = current
        clients.append(AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local"))
    yield s, f"business_team:{team_id}", clients
    for client in clients:
        await client.aclose()


@pytest.mark.parametrize("kind", ["business_team", "agent_team"])
async def test_admin_browser_inspection_does_not_become_runtime_memory_authority(
    runtime_team, owner_sessionmaker, app_user_sessionmaker, kind,
):
    from app.database import tenant_scoped_session
    from app.models.agent_team import AgentTeam
    from app.models.security_audit import SecurityAuditEvent
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.team_memory_authority import authorize_workspace

    s, workspace, (writer, inspector) = runtime_team
    n = s.n
    if kind == "agent_team":
        team_id = uuid4()
        async with owner_sessionmaker() as db:
            db.add(AgentTeam(id=team_id, tenant_id=n.tenant, lead_agent_id=n.agent,
                parent_session_id=n.session, name="Owner work team", created_by_user_id=n.user))
            await db.commit()
        workspace = f"agent_team:{team_id}"
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    ordinary = await inspector.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert ordinary.status_code in {403, 404} and "TEAM_ONLY_RELEASE_MARKER" not in ordinary.text
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    inspected = await inspector.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert inspected.status_code == 200 and "TEAM_ONLY_RELEASE_MARKER" in inspected.text, inspected.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        audits = (await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.actor_id == n.foreign))).all()
        assert audits and all("TEAM_ONLY_RELEASE_MARKER" not in str(row.details) for row in audits)
        principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.foreign,
            session_id=str(s.second_session), runtime_task_id=str(s.second_run), purpose="interactive_session")
        with pytest.raises(ContentInputReceiptError):
            await authorize_workspace(db, n.tenant, workspace, runtime_request=principal)


@pytest.mark.parametrize("kind", ["business_team", "agent_team"])
async def test_platform_admin_uses_the_selected_company_without_inheriting_its_home_company(
    runtime_team, owner_sessionmaker, app_user_sessionmaker, kind,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import memory
    from app.core.security import get_current_user
    from app.database import get_db, tenant_scoped_session
    from app.models.tenant import Tenant
    from app.models.user import User

    s, workspace, (writer, _inspector) = runtime_team
    n = s.n
    if kind == "agent_team":
        from app.models.agent_team import AgentTeam

        team_id = uuid4()
        async with owner_sessionmaker() as db:
            db.add(AgentTeam(id=team_id, tenant_id=n.tenant, lead_agent_id=n.agent, parent_session_id=n.session,
                name="Selected-company task team", created_by_user_id=n.user))
            await db.commit()
        workspace = f"agent_team:{team_id}"
    saved = await writer.put("/enterprise/memory/shared", json=body("TEAM_ONLY_RELEASE_MARKER", workspace_key=workspace))
    assert saved.status_code == 200, saved.text
    home_id, actor_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home_id, name="Operator home", slug=f"operator-{home_id.hex}"))
        await db.flush()
        actor = User(id=actor_id, tenant_id=home_id, role="platform_admin", is_active=True,
            username=f"operator-{actor_id.hex}", email=f"{actor_id.hex}@fixture.test", password_hash="fixture",
            display_name="Selected-company operator")
        db.add(actor)
        await db.commit()

    async def current():
        async with tenant_scoped_session(home_id, session_factory=app_user_sessionmaker) as db:
            authenticated = await db.get(User, actor_id)
            db.expunge(authenticated)
            authenticated.tenant_id = n.tenant
            return authenticated

    async def selected_database():
        # Controlled authenticated selection; business reads still use the real NOBYPASSRLS role.
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(memory.router)
    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = selected_database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        read = await client.get("/enterprise/memory/shared/report", params={"workspace_key": workspace, "tenant_id": str(n.tenant)})
        assert read.status_code == 200 and "TEAM_ONLY_RELEASE_MARKER" in read.text, read.text
        wrong = await client.get("/enterprise/memory/shared/report", params={"workspace_key": workspace, "tenant_id": str(home_id)})
        assert wrong.status_code == 400 and "TEAM_ONLY_RELEASE_MARKER" not in wrong.text, wrong.text


@pytest.mark.skipif(os.environ.get("HIVE_M4E_DOM_BRIDGE") != "1", reason="explicit mounted DOM/native API run")
async def test_mounted_team_memory_selection_save_reopen_and_membership_withdrawal(
    runtime_team, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    import hashlib
    from fastapi import FastAPI
    from app.api import memory
    from app.core.security import get_current_user
    from app.database import get_db, tenant_scoped_session
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services.team_memory import TeamMemoryStore
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

    s, workspace, _clients = runtime_team
    n = s.n
    second_team = uuid4()
    async with owner_sessionmaker() as db:
        db.add(BusinessTeam(id=second_team, tenant_id=n.tenant, name="Other team", created_by_user_id=n.user))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=n.tenant, business_team_id=second_team,
            user_id=n.user, role="member", status="active"))
        await db.commit()

    async def current():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            return await db.get(User, n.user)

    async def database():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            yield db
            await db.commit()

    web = FastAPI()
    web.include_router(memory.router, prefix="/api")
    web.dependency_overrides[get_current_user] = current
    web.dependency_overrides[get_db] = database

    @web.post("/api/__reference/revoke-team-memory")
    async def revoke():
        await change_membership(owner_sessionmaker, workspace, n.user, "left")
        return {"revoked": True}

    await _exercise_dom_bridge(web, {"user_id": n.user, "tenant_id": n.tenant, "agent_id": n.agent}, tmp_path,
        entry_file="e2e/team-memory-native.tsx", extra_env={"HIVE_M4E_WORKSPACE": workspace})
    content = "NATIVE_TEAM_MEMORY_REVISED"
    entry = TeamMemoryStore(data_root=n.root.parent).get_entry(str(n.tenant), workspace, "release")
    assert entry.content == content and entry.revision == 2
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "team_memory", ContentInputReceipt.carrier_ref == f"{workspace}\nrelease",
            ContentInputReceipt.revision == 2))
        assert receipt is not None and receipt.content_sha256 == hashlib.sha256(content.encode()).hexdigest()
