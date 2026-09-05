"""PDEC-013 final backend root correction — red-first discriminating regressions.

One regression per merged CC re-review / Codex independent blocker, written
against the uncorrected candidate before the production edits:

* the selected-company boundary for every platform-admin Agent lookup (the
  two cross-tenant loaders, the Personal-KB scoped-admin predicate, and true
  HTTP round trips without/with ``X-Tenant-Id``);
* the duplicate tenant-selection policy in ``api/tools.py``;
* authenticated Bearer/query-JWT file downloads honoring the validated
  selection (raw workspace + artifact download, inactive company, stale role);
* scoped-admin resource attribution before ordinary grants (grant-plus-admin
  precedence and exactly one audit row);
* the Local Agent business-session audit naming the joined ``ChatSession``
  user — including the admin-owned-host/employee-mirrored-session shape —
  across list/resolve/ws-ticket/subscribe;
* cross-owner Agent Knowledge read audits at the consumption boundary;
* the CC final-review corrections: the Session collection audit schema
  (explicit ``outcome`` + the deduplicated target set), audit-write
  fail-closed at the real ``write_audit_event`` writer for every read lane,
  and the stale-platform-token/tenantless-live-user tenant-pin restore in
  canonical authentication.

Real PostgreSQL 16 (full alembic chain, NOBYPASSRLS ``rls_app_user``) plus
true ASGI round trips through TraceId → CORS → TenantMiddleware →
``get_current_user`` → route dependencies; only ``get_db`` is redirected to
the container engine, mirroring the production dependency exactly.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.permissions import require_agent_owner_or_admin
from app.core.security import create_access_token
from app.database import _current_tenant_id, enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.knowledge import KnowledgeDocument
from app.models.local_agent_channel import LocalAgentChannelSession
from app.models.security_audit import ResourcePermission, SecurityAuditEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel_service
from app.services.personal_knowledge_access import (
    HumanBrowserPrincipal,
    resolve_personal_knowledge_permission,
)


class _World:
    def __init__(self) -> None:
        self.tenant_a: uuid.UUID
        self.tenant_b: uuid.UUID
        self.employee: uuid.UUID
        self.employee_host: uuid.UUID
        self.org_admin: uuid.UUID
        self.platform_admin: uuid.UUID
        self.member_b: uuid.UUID
        self.agent: uuid.UUID
        self.admin_hosted_agent: uuid.UUID
        self.channel_session_id: uuid.UUID
        self.employee_chat_session_id: uuid.UUID
        self.artifact_id: uuid.UUID


async def _seed_world(owner_sessionmaker) -> _World:
    world = _World()
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenant_a = Tenant(name=f"FR A {token}", slug=f"fr-a-{token}")
        tenant_b = Tenant(name=f"FR B {token}", slug=f"fr-b-{token}")
        session.add_all([tenant_a, tenant_b])
        await session.flush()

        def _user(name: str, role: str, tenant_id: uuid.UUID) -> User:
            return User(
                username=f"{name}-{token}",
                email=f"{name}-{token}@test.invalid",
                password_hash="x",
                display_name=name,
                tenant_id=tenant_id,
                role=role,
                is_active=True,
            )

        employee = _user("fr-employee", "member", tenant_a.id)
        org_admin = _user("fr-orgadmin", "org_admin", tenant_a.id)
        platform_admin = _user("fr-platform", "platform_admin", tenant_b.id)
        member_b = _user("fr-member-b", "member", tenant_b.id)
        session.add_all([employee, org_admin, platform_admin, member_b])
        await session.flush()

        agent = Agent(
            name=f"FR employee agent {token}",
            tenant_id=tenant_a.id,
            creator_id=employee.id,
            owner_user_id=employee.id,
            sponsor_user_id=employee.id,
            status="running",
        )
        # The discriminating Local-Agent shape: the host/Agent owner is the
        # administrator, while the mirrored private ChatSession belongs to an
        # employee.
        admin_hosted_agent = Agent(
            name=f"FR admin-hosted local agent {token}",
            tenant_id=tenant_a.id,
            creator_id=org_admin.id,
            owner_user_id=org_admin.id,
            sponsor_user_id=org_admin.id,
            status="running",
            agent_type="local_agent",
        )
        session.add_all([agent, admin_hosted_agent])
        await session.flush()

        employee_chat = ChatSession(
            agent_id=admin_hosted_agent.id,
            tenant_id=tenant_a.id,
            user_id=employee.id,
            title=f"employee private session {token}",
            source_channel="web",
        )
        artifact_chat = ChatSession(
            agent_id=agent.id,
            tenant_id=tenant_a.id,
            user_id=employee.id,
            title=f"artifact session {token}",
            source_channel="web",
        )
        session.add_all([employee_chat, artifact_chat])
        await session.flush()

        channel_session = LocalAgentChannelSession(
            tenant_id=tenant_a.id,
            owner_user_id=org_admin.id,
            source_agent_id=admin_hosted_agent.id,
            chat_session_id=employee_chat.id,
            source="web",
            status="active",
        )
        session.add(channel_session)

        artifact_message = ChatMessage(
            agent_id=agent.id,
            tenant_id=tenant_a.id,
            user_id=employee.id,
            role="assistant",
            content=f"artifact message {token}",
        )
        session.add(artifact_message)
        await session.flush()

        artifact = ChatArtifact(
            agent_id=agent.id,
            tenant_id=tenant_a.id,
            session_id=artifact_chat.id,
            message_id=artifact_message.id,
            owner_user_id=employee.id,
            root_session_id=artifact_chat.id,
            authority_state="owned",
            path="workspace/report.md",
            name="report.md",
            mime_type="text/markdown",
            size=24,
            preview_kind="download",
            source="workspace_write",
            snapshot_hash=f"hash-{token}",
        )
        session.add(artifact)

        document = KnowledgeDocument(
            tenant_id=tenant_a.id,
            scope_type="person",
            scope_id=employee.id,
            title=f"employee private doc {token}",
            source_kind="markdown",
            source_uri=f"markdown://fr-{token}",
            source_sha256=f"sha256-{token}",
            sensitivity="PL3_sensitive",
            status="ready",
            agent_searchable=True,
            canonical_md_path=f"personal/{token}/doc.md",
            created_by_user_id=employee.id,
        )
        session.add(document)
        await session.commit()

        world.tenant_a = tenant_a.id
        world.tenant_b = tenant_b.id
        world.employee = employee.id
        world.employee_host = org_admin.id  # host of admin_hosted_agent is the org admin
        world.org_admin = org_admin.id
        world.platform_admin = platform_admin.id
        world.member_b = member_b.id
        world.agent = agent.id
        world.admin_hosted_agent = admin_hosted_agent.id
        world.channel_session_id = channel_session.id
        world.employee_chat_session_id = employee_chat.id
        world.artifact_id = artifact.id
    return world


async def _write_workspace_file(owner_sessionmaker, world: _World, rel_path: str, content: bytes) -> None:
    """Write a workspace file plus its ownership manifest.

    The manifest is what makes the file provably the employee's for ordinary
    (non-administrator) readers; without it the file is a quarantined legacy
    row that only Operator View may inspect — a pre-existing boundary this
    suite must not weaken.
    """

    from app.api.files import _agent_base_dir
    from app.models.workspace_resource import WorkspaceResourceManifest

    base = _agent_base_dir(world.agent)
    target = base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    async with owner_sessionmaker() as session:
        session.add(
            WorkspaceResourceManifest(
                tenant_id=world.tenant_a,
                agent_id=world.agent,
                path=rel_path,
                owner_user_id=world.employee,
                authority_state="owned",
            )
        )
        await session.commit()


async def _load_actor(db, user_id: uuid.UUID) -> User:
    actor = await db.get(User, user_id)
    if actor is None:
        async with enter_rls_bypass(db, reason="test canonical actor lookup", actor_id=str(user_id)) as bypass_db:
            actor = await bypass_db.get(User, user_id)
    assert actor is not None
    return actor


def _selected(db, actor: User, tenant_id: uuid.UUID) -> User:
    """Mirror ``get_current_user``: detach and override the selected tenant."""
    db.expunge(actor)
    actor.tenant_id = tenant_id
    return actor


async def _audit_rows(db, event_type: str, *, actor_id: uuid.UUID | None = None) -> list[SecurityAuditEvent]:
    statement = select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == event_type)
    if actor_id is not None:
        statement = statement.where(SecurityAuditEvent.actor_id == actor_id)
    return list((await db.execute(statement)).scalars().all())


# ─── Fix 1: selected-company boundary for platform-admin Agent lookups ──


async def test_unselected_platform_admin_agent_lookup_is_not_found(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.core.permissions import check_agent_access

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        # The middleware-equivalent request scope: the platform administrator's
        # own home company, with no X-Tenant-Id selection.
        await pin_rls_tenant_context(db, world.tenant_b)
        actor = await _load_actor(db, world.platform_admin)
        assert str(actor.tenant_id) == str(world.tenant_b)

        with pytest.raises(HTTPException) as exc:
            await check_agent_access(db, actor, world.agent)
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            await require_agent_owner_or_admin(db, actor, world.agent)
        assert exc.value.status_code == 404


async def test_tenantless_platform_admin_must_select_a_company_first(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.core.permissions import check_agent_access

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        # Tenantless platform identity: no token tenant and no selection, the
        # same fail-closed pin the middleware/get_db pair would produce.
        await pin_rls_tenant_context(db, None)
        actor = await _load_actor(db, world.platform_admin)
        db.expunge(actor)
        actor.tenant_id = None

        with pytest.raises(HTTPException) as exc:
            await check_agent_access(db, actor, world.agent)
        assert exc.value.status_code == 404


async def test_selected_platform_admin_keeps_selected_company_authority(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.core.permissions import check_agent_access

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        actor = await _load_actor(db, world.platform_admin)
        selected = _selected(db, actor, world.tenant_a)
        await pin_rls_tenant_context(db, world.tenant_a)

        loaded, access_level = await check_agent_access(db, selected, world.agent)
        assert str(loaded.id) == str(world.agent)
        assert access_level == "manage"
        lifecycle_agent = await require_agent_owner_or_admin(db, selected, world.agent)
        assert str(lifecycle_agent.id) == str(world.agent)


async def test_org_admin_stays_bounded_to_own_company(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.core.permissions import check_agent_access

    world = await _seed_world(owner_sessionmaker)
    async with owner_sessionmaker() as owner_db:
        foreign_agent = Agent(
            name="FR foreign agent",
            tenant_id=world.tenant_b,
            creator_id=world.member_b,
            owner_user_id=world.member_b,
            sponsor_user_id=world.member_b,
            status="running",
        )
        owner_db.add(foreign_agent)
        await owner_db.commit()
        foreign_agent_id = foreign_agent.id

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        actor = await _load_actor(db, world.org_admin)
        with pytest.raises(HTTPException) as exc:
            await check_agent_access(db, actor, foreign_agent_id)
        assert exc.value.status_code == 404


async def test_personal_kb_platform_admin_scope_requires_company_equality(
    owner_sessionmaker,
) -> None:
    world = await _seed_world(owner_sessionmaker)

    class _NoGrantSession:
        async def execute(self, _stmt):
            class _Rows:
                def all(self):
                    return []

            return _Rows()

    # A platform administrator whose authenticated company (home tenant B)
    # is NOT the employee's company must not read the employee's PL3 personal
    # knowledge by role alone.
    foreign_decision = await resolve_personal_knowledge_permission(
        _NoGrantSession(),  # type: ignore[arg-type]
        tenant_id=world.tenant_a,
        owner_user_id=world.employee,
        principal=HumanBrowserPrincipal(
            user_id=world.platform_admin,
            role="platform_admin",
            home_tenant_id=world.tenant_b,
        ),
        action="read",
        document_sensitivity="PL3_sensitive",
    )
    assert foreign_decision.allowed is False
    assert foreign_decision.authority_source == "none"

    selected_decision = await resolve_personal_knowledge_permission(
        _NoGrantSession(),  # type: ignore[arg-type]
        tenant_id=world.tenant_a,
        owner_user_id=world.employee,
        principal=HumanBrowserPrincipal(
            user_id=world.platform_admin,
            role="platform_admin",
            home_tenant_id=world.tenant_a,
        ),
        action="read",
        document_sensitivity="PL3_sensitive",
    )
    assert selected_decision.allowed is True
    assert selected_decision.authority_source == "scoped_business_admin"


# ─── True HTTP/ASGI round trips ─────────────────────────────────────────


@pytest.fixture()
async def asgi(app_user_sessionmaker, owner_sessionmaker, monkeypatch):
    from app.services import audit_logger

    from app.database import get_db
    from app.main import app

    async def _override_get_db():
        tenant_id = _current_tenant_id.get()
        async with app_user_sessionmaker() as session:
            try:
                await pin_rls_tenant_context(session, tenant_id)
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    # The fail-closed tenant-impersonation audit deliberately commits through
    # an independent session (``audit_logger.async_session``) so the operator
    # receipt never depends on the uncommitted request transaction. Under the
    # CI hermetic DATABASE_URL that module-global engine is unroutable, so the
    # real production writer would fail-closed 503 before the route runs. Point
    # the factory at the same Testcontainers PostgreSQL (owner role, matching
    # the operator-plane BYPASS insert) so the full chain — advisory lock →
    # cutover/head load → envelope seal → insert → independent commit — stays
    # the real code path under test. The 503-on-audit-failure contract itself
    # is covered by tests/core/test_security.py against the real writer.
    monkeypatch.setattr(audit_logger, "async_session", owner_sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fr.test") as client:
        yield client
    app.dependency_overrides.pop(get_db, None)


def _bearer(user_id, role, tid) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id), role, str(tid) if tid else None)}"}


async def test_http_agent_detail_requires_selection_for_foreign_company(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    resp = await asgi.get(f"/api/agents/{world.agent}", headers=headers)
    assert resp.status_code == 404, f"expected no-leak 404, got {resp.status_code}: {resp.text[:300]}"


async def test_http_personal_kb_requires_selection_for_foreign_company(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    resp = await asgi.get(f"/api/agents/{world.agent}/knowledge/personal/documents", headers=headers)
    assert resp.status_code == 404, f"expected no-leak 404, got {resp.status_code}: {resp.text[:300]}"


async def test_http_selected_company_agent_detail_and_personal_kb_still_work(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    detail = await asgi.get(f"/api/agents/{world.agent}", headers=headers)
    assert detail.status_code == 200, detail.text[:300]
    assert detail.json()["action_capabilities"]["can_manage_permissions"] is True

    documents = await asgi.get(f"/api/agents/{world.agent}/knowledge/personal/documents", headers=headers)
    assert documents.status_code == 200, documents.text[:300]
    titles = [item["title"] for item in documents.json()["documents"]]
    assert any("employee private doc" in title for title in titles)


# ─── Fix 2: tools.py duplicate tenant-selection policy ──────────────────


async def test_tools_foreign_query_tenant_requires_selection(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.tools import list_tools

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        actor = await _load_actor(db, world.platform_admin)

        with pytest.raises(HTTPException) as exc:
            await list_tools(tenant_id=str(world.tenant_a), current_user=actor, db=db)
        assert exc.value.status_code == 400
        assert "select the company first" in str(exc.value.detail).lower()


async def test_tools_retired_foreign_query_tenant_rejected(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.tools import list_tools

    world = await _seed_world(owner_sessionmaker)
    async with owner_sessionmaker() as owner_db:
        retired = await owner_db.get(Tenant, world.tenant_a)
        assert retired is not None
        retired.is_active = False
        await owner_db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        actor = await _load_actor(db, world.platform_admin)
        with pytest.raises(HTTPException) as exc:
            await list_tools(tenant_id=str(world.tenant_a), current_user=actor, db=db)
        assert exc.value.status_code == 400


async def test_tools_matching_selected_company_query_tenant_works(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.tools import list_tools

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        actor = await _load_actor(db, world.platform_admin)
        selected = _selected(db, actor, world.tenant_a)
        await pin_rls_tenant_context(db, world.tenant_a)

        rows = await list_tools(tenant_id=str(world.tenant_a), current_user=selected, db=db)
        assert isinstance(rows, list)


async def test_http_tools_foreign_query_tenant_rejected(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    resp = await asgi.get(f"/api/tools?tenant_id={world.tenant_a}", headers=headers)
    assert resp.status_code == 400, f"expected selection recovery error, got {resp.status_code}: {resp.text[:300]}"


# ─── Fix 3: authenticated downloads honor the validated selection ───────


async def test_http_platform_admin_workspace_download_uses_selection(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    await _write_workspace_file(owner_sessionmaker, world, "workspace/report.md", b"# selected company report\n")

    # The real frontend getBlob shape: Bearer + validated X-Tenant-Id.
    selected_headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    ok = await asgi.get(
        f"/api/agents/{world.agent}/files/download", params={"path": "workspace/report.md"}, headers=selected_headers
    )
    assert ok.status_code == 200, ok.text[:300]
    assert b"selected company report" in ok.content

    # Without any selection the foreign Agent UUID is not a second selector.
    unselected_headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    denied = await asgi.get(
        f"/api/agents/{world.agent}/files/download", params={"path": "workspace/report.md"}, headers=unselected_headers
    )
    assert denied.status_code == 404, f"expected no-leak 404, got {denied.status_code}: {denied.text[:300]}"


async def test_http_platform_admin_artifact_download_uses_selection(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    await _write_workspace_file(owner_sessionmaker, world, "workspace/report.md", b"# artifact bytes\n")

    selected_headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    ok = await asgi.get(
        f"/api/agents/{world.agent}/files/artifacts/{world.artifact_id}/download", headers=selected_headers
    )
    assert ok.status_code == 200, ok.text[:300]

    unselected_headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    denied = await asgi.get(
        f"/api/agents/{world.agent}/files/artifacts/{world.artifact_id}/download", headers=unselected_headers
    )
    assert denied.status_code == 404, f"expected no-leak 404, got {denied.status_code}: {denied.text[:300]}"


async def test_http_download_rejects_inactive_selected_company(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    await _write_workspace_file(owner_sessionmaker, world, "workspace/report.md", b"# retired company report\n")
    async with owner_sessionmaker() as owner_db:
        retired = await owner_db.get(Tenant, world.tenant_a)
        assert retired is not None
        retired.is_active = False
        await owner_db.commit()

    headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    resp = await asgi.get(
        f"/api/agents/{world.agent}/files/download", params={"path": "workspace/report.md"}, headers=headers
    )
    assert resp.status_code == 403, f"expected disabled-company 403, got {resp.status_code}: {resp.text[:300]}"


async def test_http_download_stale_platform_token_role_is_not_a_selector(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    await _write_workspace_file(owner_sessionmaker, world, "workspace/report.md", b"# stale role report\n")
    async with owner_sessionmaker() as owner_db:
        demoted = await owner_db.get(User, world.platform_admin)
        assert demoted is not None
        demoted.role = "member"
        await owner_db.commit()

    # A stale token still claims platform_admin and carries a foreign
    # X-Tenant-Id; the canonical DB role wins and no second selector appears.
    headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    resp = await asgi.get(
        f"/api/agents/{world.agent}/files/download", params={"path": "workspace/report.md"}, headers=headers
    )
    assert resp.status_code == 404, f"expected no-leak 404, got {resp.status_code}: {resp.text[:300]}"


async def test_stale_platform_token_tenantless_live_user_pin_is_restored(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    """A stale platform token cannot leave a foreign tenant pin active.

    The CC final-review residual: when the token still claims
    ``platform_admin`` (so TenantMiddleware optimistically pins the
    ``X-Tenant-Id`` company for the whole request) but the live canonical user
    is no longer a platform administrator and is tenantless, the old
    ``authenticate_request_user`` branches never fired — the request kept the
    foreign tenant pin, and every RLS tenant policy would treat it as the
    selected company. The canonical authentication must restore the live
    user's actual tenant scope, including ``None``.
    """

    from starlette.requests import Request

    from app.core.permissions import is_scoped_business_admin
    from app.core.security import authenticate_request_user
    from app.database import reset_current_tenant, set_current_tenant

    world = await _seed_world(owner_sessionmaker)
    async with owner_sessionmaker() as owner_db:
        demoted = await owner_db.get(User, world.platform_admin)
        assert demoted is not None
        demoted.role = "member"
        demoted.tenant_id = None  # offboarded from every company
        await owner_db.commit()
        # Sanity: company A still owns business rows, so an empty result below
        # proves the pin was restored rather than an empty world.
        assert len(list((await owner_db.execute(select(Agent.id))).scalars().all())) >= 2

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/agents",
            "raw_path": b"/api/agents",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("fr.test", 80),
        }
    )
    stale_token = create_access_token(str(world.platform_admin), "platform_admin", str(world.tenant_b))
    # The exact TenantMiddleware + get_db shape for a platform_admin token
    # with an X-Tenant-Id header: the optimistic pin lands before the live
    # user is verified.
    context_token = set_current_tenant(str(world.tenant_a))
    try:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, world.tenant_a)
            user = await authenticate_request_user(
                db,
                jwt_token=stale_token,
                requested_tenant=str(world.tenant_a),
                request=request,
            )
            assert user.role == "member"
            assert user.tenant_id is None

            # No scoped-business-admin authority in any company: the live
            # role/scope wins, never the stale token claim.
            assert is_scoped_business_admin(user, resource_tenant_id=world.tenant_a) is False
            assert is_scoped_business_admin(user, resource_tenant_id=world.tenant_b) is False

            # No selected-tenant RLS/business data visibility: the stale pin
            # must be restored to the tenantless scope, so company A's rows
            # are invisible through this request session.
            visible_agents = list((await db.execute(select(Agent.id))).scalars().all())
            assert visible_agents == [], "stale X-Tenant-Id pin must not survive canonical authentication"
    finally:
        reset_current_tenant(context_token)


async def test_http_employee_download_lanes(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    await _write_workspace_file(owner_sessionmaker, world, "workspace/report.md", b"# employee report\n")

    # Own company query-JWT download (browser-friendly lane) keeps working.
    resp = await asgi.get(
        f"/api/agents/{world.agent}/files/download",
        params={
            "path": "workspace/report.md",
            "token": create_access_token(str(world.employee), "member", str(world.tenant_a)),
        },
    )
    assert resp.status_code == 200, resp.text[:300]

    # A foreign-company member cannot download by Agent UUID.
    foreign = await asgi.get(
        f"/api/agents/{world.agent}/files/download",
        params={
            "path": "workspace/report.md",
            "token": create_access_token(str(world.member_b), "member", str(world.tenant_b)),
        },
    )
    assert foreign.status_code == 404, foreign.text[:300]


# ─── Fix 4: scoped-admin attribution before ordinary grants ─────────────


async def test_grant_does_not_suppress_scoped_admin_audit(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.core.resource_authority import authorize_resource_action

    world = await _seed_world(owner_sessionmaker)
    resource_id = uuid.uuid4()
    async with owner_sessionmaker() as owner_db:
        owner_db.add(
            ResourcePermission(
                tenant_id=world.tenant_a,
                principal_type="user",
                principal_id=world.org_admin,
                resource_type="task",
                resource_id=resource_id,
                actions=["read"],
                effect="allow",
                conditions={},
            )
        )
        await owner_db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        actor = await _load_actor(db, world.org_admin)

        decision = await authorize_resource_action(
            db,
            actor,
            agent_id=world.agent,
            resource_kind="task",
            resource_id=resource_id,
            action="read",
            owner_user_id=world.employee,
        )
        assert decision.authority_source == "scoped_business_admin"

        audit_rows = await _audit_rows(db, "resource.scoped_business_admin_access", actor_id=world.org_admin)
        assert len(audit_rows) == 1, "grant-plus-admin must audit exactly once as scoped admin"
        row = audit_rows[0]
        assert str(row.tenant_id) == str(world.tenant_a)
        assert row.details["outcome"] == "allowed"
        assert row.details["authority_source"] == "scoped_business_admin"
        assert row.details["owner_user_id"] == str(world.employee)

        # The true owner stays on the quiet path: no admin audit noise.
        owner = await _load_actor(db, world.employee)
        owner_decision = await authorize_resource_action(
            db,
            owner,
            agent_id=world.agent,
            resource_kind="task",
            resource_id=resource_id,
            action="read",
            owner_user_id=world.employee,
        )
        assert owner_decision.authority_source == "resource_owner"
        assert len(await _audit_rows(db, "resource.scoped_business_admin_access", actor_id=world.employee)) == 0


async def test_resource_audit_failure_denies_before_content(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch
) -> None:
    from app.core import policy as policy_module
    from app.core.resource_authority import authorize_resource_action

    world = await _seed_world(owner_sessionmaker)

    async def failing_audit(*_args, **_kwargs):
        raise RuntimeError("audit plane unavailable")

    # The real audit writer, not an intermediate wrapper: the resource lane
    # imports ``write_audit_event`` lazily, so patching the policy module
    # attribute proves the actual write failure propagates.
    monkeypatch.setattr(policy_module, "write_audit_event", failing_audit)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        actor = await _load_actor(db, world.org_admin)
        with pytest.raises(RuntimeError):
            await authorize_resource_action(
                db,
                actor,
                agent_id=world.agent,
                resource_kind="task",
                resource_id=uuid.uuid4(),
                action="read",
                owner_user_id=world.employee,
            )


async def test_audit_write_failure_denies_protected_content_at_the_real_writer(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch
) -> None:
    """A failure of the real audit writer denies before protected content.

    CC final-review F-3 regression: the fail-closed contract must hold at
    ``app.core.policy.write_audit_event`` itself — not only at an intermediate
    wrapper — for every scoped-admin read lane: Session collection, Agent
    Knowledge, Local Agent, and Personal Knowledge. Every lane imports the
    writer lazily, so the module-attribute patch is the exact production call.
    """

    import app.api.agent_knowledge as agent_knowledge_api
    import app.api.chat_sessions as chat_sessions_api
    from app.core import policy as policy_module

    async def failing_write_audit(*_args, **_kwargs):
        raise RuntimeError("audit plane unavailable")

    monkeypatch.setattr(policy_module, "write_audit_event", failing_write_audit)

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin = await _load_actor(db, world.org_admin)

        # Session collection: the scope=all listing must not return while the
        # collection audit cannot be written.
        with pytest.raises(RuntimeError):
            await chat_sessions_api.list_sessions(agent_id=world.agent, scope="all", current_user=admin, db=db)

        # Agent Knowledge consumption surface.
        with pytest.raises(RuntimeError):
            await agent_knowledge_api.get_overview(agent_id=world.agent, db=db, current_user=admin)

        # Local Agent channel collection naming the mirrored business user.
        with pytest.raises(RuntimeError):
            await channel_service.list_agent_channel_sessions(
                db,
                tenant_id=world.tenant_a,
                owner_user_id=world.org_admin,
                actor_user_id=None,
                source_agent_id=world.admin_hosted_agent,
                access_user=admin,
            )

        # Personal Knowledge cross-owner read.
        with pytest.raises(RuntimeError):
            await agent_knowledge_api.list_personal_documents(agent_id=world.agent, limit=50, db=db, current_user=admin)


# ─── Fix 5: Local Agent business-session audit ──────────────────────────


async def test_admin_owned_host_employee_session_audited_with_business_user(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin = await _load_actor(db, world.org_admin)

        # The host owner is the administrator, so the old host-owner
        # comparison produced no audit at all; the mirrored ChatSession
        # belongs to the employee and must drive the cross-owner decision.
        rows = await channel_service.list_agent_channel_sessions(
            db,
            tenant_id=world.tenant_a,
            owner_user_id=world.org_admin,
            actor_user_id=None,
            source_agent_id=world.admin_hosted_agent,
            access_user=admin,
        )
        assert [str(row["id"]) for row in rows] == [str(world.channel_session_id)]

        audit_rows = await _audit_rows(db, "local_agent_channel.scoped_business_admin_access", actor_id=world.org_admin)
        assert len(audit_rows) == 1, "one collection audit per request naming the business target set"
        row = audit_rows[0]
        assert str(row.tenant_id) == str(world.tenant_a)
        assert row.action == "list"
        assert row.details["session_user_id"] == str(world.employee)
        assert row.details["session_owner_user_id"] == str(world.org_admin)
        assert row.details["outcome"] == "allowed"

        # Detail resolution of the same employee session names the employee.
        await channel_service.get_channel_session_for_actor(
            db,
            session_id=world.channel_session_id,
            actor_user_id=world.org_admin,
            access_user=admin,
            action="read",
        )
        detail_rows = await _audit_rows(
            db, "local_agent_channel.scoped_business_admin_access", actor_id=world.org_admin
        )
        assert len(detail_rows) == 2
        assert detail_rows[-1].action == "read"
        assert detail_rows[-1].details["session_user_id"] == str(world.employee)


async def test_ws_ticket_issuance_and_subscription_name_business_user(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin = await _load_actor(db, world.org_admin)

        ticket = await channel_service.create_browser_session_ws_ticket(
            db,
            tenant_id=world.tenant_a,
            actor_user_id=world.org_admin,
            session_id=world.channel_session_id,
            access_user=admin,
        )
        issued = await _audit_rows(db, "local_agent_channel.scoped_business_admin_access", actor_id=world.org_admin)
        assert [row.action for row in issued] == ["ws_ticket"]
        assert issued[0].details["session_user_id"] == str(world.employee)

        resolved = await channel_service.resolve_browser_session_ws_ticket(
            db, ticket=ticket["ticket"], session_id=world.channel_session_id
        )
        assert str(resolved["owner_user_id"]) == str(world.org_admin)
        subscribed = await _audit_rows(db, "local_agent_channel.scoped_business_admin_access", actor_id=world.org_admin)
        assert [row.action for row in subscribed] == ["ws_ticket", "ws_subscribe"]
        assert subscribed[-1].details["session_user_id"] == str(world.employee)


async def test_local_agent_self_and_owner_reads_stay_quiet(owner_sessionmaker, app_user_sessionmaker) -> None:
    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        employee = await _load_actor(db, world.employee)

        # The employee (mirrored business user) reads their own private
        # session through the session-id-only lane: quiet.
        payload, host_owner = await channel_service.get_channel_session_for_actor(
            db,
            session_id=world.channel_session_id,
            actor_user_id=world.employee,
            access_user=employee,
        )
        assert str(host_owner) == str(world.org_admin)
        assert str(payload["id"]) == str(world.channel_session_id)
        assert await _audit_rows(db, "local_agent_channel.scoped_business_admin_access") == []

        # A same-tenant ordinary member without any scope gets nothing.
        with pytest.raises(HTTPException) as exc:
            await channel_service.get_channel_session_for_actor(
                db,
                session_id=world.channel_session_id,
                actor_user_id=world.member_b,
                access_user=employee,
            )
        assert exc.value.status_code == 404


async def test_http_local_agent_list_audits_business_user(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    resp = await asgi.get(f"/api/agents/{world.admin_hosted_agent}/local-agent/sessions", headers=headers)
    assert resp.status_code == 200, resp.text[:300]
    assert [str(item["id"]) for item in resp.json()] == [str(world.channel_session_id)]

    async with owner_sessionmaker() as owner_db:
        rows = list(
            (
                await owner_db.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.event_type == "local_agent_channel.scoped_business_admin_access",
                        SecurityAuditEvent.actor_id == world.platform_admin,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].action == "list"
    assert rows[0].details["session_user_id"] == str(world.employee)
    assert str(rows[0].tenant_id) == str(world.tenant_a)


# ─── Fix 6: Agent Knowledge consumption audit ───────────────────────────


async def test_agent_knowledge_reads_audit_cross_owner_admin(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.agent_knowledge import get_candidates, get_entries, get_events, get_overview, get_pages

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin = await _load_actor(db, world.org_admin)

        await get_overview(agent_id=world.agent, db=db, current_user=admin)
        await get_pages(agent_id=world.agent, db=db, current_user=admin)
        await get_entries(agent_id=world.agent, db=db, current_user=admin)
        await get_events(agent_id=world.agent, db=db, current_user=admin)
        await get_candidates(agent_id=world.agent, db=db, current_user=admin)

        audit_rows = await _audit_rows(db, "agent_knowledge.scoped_business_admin_access", actor_id=world.org_admin)
        assert [row.action for row in audit_rows] == [
            "agent_knowledge_overview",
            "agent_knowledge_pages",
            "agent_knowledge_entries",
            "agent_knowledge_events",
            "agent_knowledge_candidates",
        ]
        for row in audit_rows:
            assert str(row.tenant_id) == str(world.tenant_a)
            assert row.details["owner_user_id"] == str(world.employee)
            assert row.details["outcome"] == "allowed"
            assert row.details["agent_id"] == str(world.agent)


async def test_agent_knowledge_owner_reads_stay_quiet(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.agent_knowledge import get_overview

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        employee = await _load_actor(db, world.employee)

        await get_overview(agent_id=world.agent, db=db, current_user=employee)
        assert await _audit_rows(db, "agent_knowledge.scoped_business_admin_access") == []


async def test_http_agent_knowledge_audit_and_cross_company_negative(owner_sessionmaker, asgi) -> None:
    world = await _seed_world(owner_sessionmaker)
    selected_headers = {
        **_bearer(world.platform_admin, "platform_admin", world.tenant_b),
        "X-Tenant-Id": str(world.tenant_a),
    }
    ok = await asgi.get(f"/api/agents/{world.agent}/knowledge/entries", headers=selected_headers)
    assert ok.status_code == 200, ok.text[:300]

    unselected_headers = _bearer(world.platform_admin, "platform_admin", world.tenant_b)
    denied = await asgi.get(f"/api/agents/{world.agent}/knowledge/entries", headers=unselected_headers)
    assert denied.status_code == 404, denied.text[:300]

    async with owner_sessionmaker() as owner_db:
        rows = list(
            (
                await owner_db.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.event_type == "agent_knowledge.scoped_business_admin_access",
                        SecurityAuditEvent.actor_id == world.platform_admin,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].action == "agent_knowledge_entries"
    assert rows[0].details["owner_user_id"] == str(world.employee)


# ─── Fix 8: outcome field + fail-closed writes for the session lane ─────


async def test_scoped_admin_session_audit_records_allowed_outcome(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.core.permissions import authorize_session_action

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin = await _load_actor(db, world.org_admin)
        await db.flush()

        decision = await authorize_session_action(
            db,
            admin,
            agent_id=world.agent,
            session_id=(await db.execute(select(ChatSession).where(ChatSession.agent_id == world.agent)))
            .scalar_one()
            .id,
            action="read",
        )
        assert decision.authority_source == "scoped_business_admin"
        rows = await _audit_rows(db, "session.scoped_business_admin_access", actor_id=world.org_admin)
        assert rows and rows[-1].details["outcome"] == "allowed"
