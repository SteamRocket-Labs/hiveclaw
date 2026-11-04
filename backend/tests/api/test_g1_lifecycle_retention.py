"""G1 — digital employees are created, never deleted; conversations leave lists, not the database.

Owner decision (2026-11-04): 数字员工只建不删，管理员也不能删；会话只从自己的列表移除，
谁也不能物理删除。Deactivation ("停用") reuses ``agent_retirement.prepare`` and stops there;
restore ("恢复") clears the deactivation; rollback of an old AI-asset revision can no longer
revive or delete an employee; company identity retirement requires every bot deactivated, not
deleted. These tests fail on the pre-G1 code and pin the post-G1 contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _FakeDB:
    def __init__(self, results=()):
        self._results = list(results)
        self.statements: list[str] = []
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def _router_paths(method: str) -> set[str]:
    from app.api.agents import router as agents_router
    from app.api.desktop_agents import router as desktop_router

    paths = {
        f"{getattr(route, 'path', '')}"
        for route in agents_router.routes
        if getattr(route, "methods", None) and method in route.methods
    }
    paths |= {
        f"{getattr(route, 'path', '')}"
        for route in desktop_router.routes
        if getattr(route, "methods", None) and method in route.methods
    }
    return paths


# ─── no interface deletes a digital employee ────────────────────────────


def test_no_agent_delete_route_remains() -> None:
    import app.api.agents as agents_api

    delete_paths = _router_paths("DELETE")
    assert "/agents/{agent_id}" not in delete_paths, f"agent DELETE routes remain: {sorted(delete_paths)}"
    assert not hasattr(agents_api, "delete_agent")


def test_no_desktop_sub_agent_delete_route_remains() -> None:
    import app.api.desktop_agents as desktop_agents_api

    delete_paths = _router_paths("DELETE")
    assert "/desktop/agents/{agent_id}" not in delete_paths, f"desktop DELETE routes remain: {sorted(delete_paths)}"
    assert not hasattr(desktop_agents_api, "delete_sub_agent")


def test_no_cleanup_pending_route_remains() -> None:
    """File archival retry existed only to finish a bot deletion; with no deletion it is dead."""
    from app.api.agents import router as agents_router

    paths = {getattr(route, "path", "") for route in agents_router.routes}
    assert "/agents/cleanup-pending" not in paths


@pytest.mark.asyncio
async def test_deactivate_reuses_retirement_prepare_and_keeps_the_employee(monkeypatch) -> None:
    import app.api.agents as agents_api
    from app.services import agent_retirement

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        name="在被测的员工",
        status="idle",
        deleted_at=None,
        deactivated_at=None,
        deactivation_reason=None,
    )
    db = _FakeDB()
    prepare_calls: list[tuple[object, object]] = []

    async def fake_check_agent_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent.id
        return agent, "manage"

    async def record_prepare(_db, *, agent, actor):
        prepare_calls.append((agent, actor))
        # The post-G1 prepare commits the deactivation state itself.
        agent.deactivated_at = datetime.now(timezone.utc)
        agent.deactivation_reason = agent_retirement.PENDING_REASON
        await _db.commit()

    audits: list[dict] = []
    projections: list[tuple[object, str]] = []

    async def fake_audit(_db, **kwargs):
        audits.append(kwargs)

    async def fake_register(_db, projection, *, change_source, actor_user_id, change_message):
        projections.append((projection, change_source))

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_check_agent_history_access)
    monkeypatch.setattr(agent_retirement, "prepare", record_prepare)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fake_register)

    await agents_api.deactivate_agent(agent_id=agent.id, current_user=admin, db=db)

    assert prepare_calls == [(agent, admin)], "停用 must run agent_retirement.prepare exactly once"
    assert agent.deleted_at is None, "停用 must not delete the employee"
    assert agent.deactivated_at is not None
    assert agent.status == "stopped"
    assert any(a.get("event_type") == "agent.deactivated" for a in audits), f"audits={audits}"
    assert projections and projections[0][1] in {"revoke", "update"}
    assert db.deleted == []
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_deactivate_requires_administrator_role(monkeypatch) -> None:
    import app.api.agents as agents_api
    from app.services import agent_retirement

    member = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("prepare must not run for a non-admin")

    monkeypatch.setattr(agent_retirement, "prepare", fail_prepare)

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.deactivate_agent(agent_id=uuid4(), current_user=member, db=_FakeDB())

    assert exc_info.value.status_code == 403


# ─── 恢复 clears the deactivation; triggers stay off ────────────────────


@pytest.mark.asyncio
async def test_restore_clears_deactivation_and_leaves_triggers_off(monkeypatch) -> None:
    import app.api.agents as agents_api

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    deactivated_at = datetime.now(timezone.utc)
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        name="待恢复的员工",
        status="stopped",
        deleted_at=None,
        deactivated_at=deactivated_at,
        deactivation_reason="retirement_pending",
        primary_model_id=None,
        fallback_model_id=None,
    )
    db = _FakeDB()
    audits: list[dict] = []
    projections: list[object] = []

    async def fake_check_agent_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent.id
        return agent, "read"

    async def fake_audit(_db, **kwargs):
        audits.append(kwargs)

    async def fake_register(_db, projection, **_kwargs):
        projections.append(projection)

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_check_agent_history_access)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fake_register)

    await agents_api.restore_agent(agent_id=agent.id, current_user=admin, db=db)

    assert agent.deactivated_at is None
    assert agent.deactivation_reason is None
    assert agent.deleted_at is None
    assert any(a.get("event_type") == "agent.restored" for a in audits), f"audits={audits}"
    assert projections and projections[0].lifecycle_status == "active"
    # Triggers were turned off by 停用 and stay off: restore issues no enable update.
    assert all("agent_triggers" not in statement for statement in db.statements), db.statements
    assert all("agent_schedules" not in statement for statement in db.statements), db.statements
    assert db.commits == 1


@pytest.mark.asyncio
async def test_restore_requires_administrator_role(monkeypatch) -> None:
    import app.api.agents as agents_api

    member = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.restore_agent(agent_id=uuid4(), current_user=member, db=_FakeDB())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_restore_rejects_agent_that_is_not_deactivated(monkeypatch) -> None:
    import app.api.agents as agents_api

    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4())
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=admin.tenant_id,
        deleted_at=None,
        deactivated_at=None,
        deactivation_reason=None,
    )

    async def fake_check_agent_history_access(_db, _user, requested_agent_id):
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_check_agent_history_access)

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.restore_agent(agent_id=agent.id, current_user=admin, db=_FakeDB())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail.get("code") == "agent_not_deactivated"


# ─── AI-asset rollback cannot change lifecycle ──────────────────────────


def test_asset_rollback_neither_revives_nor_deletes_the_employee() -> None:
    from datetime import UTC

    from app.services.ai_asset_adapters import apply_agent_content

    deactivated_at = datetime(2026, 11, 1, tzinfo=UTC)
    agent = SimpleNamespace(
        name="回滚目标",
        deleted_at=None,
        deactivated_at=deactivated_at,
        deactivation_reason="retirement_pending",
        max_tool_rounds=10,
    )

    active_revision = {
        "schema": "hive.ai_asset.agent.v1",
        "asset_type": "agent",
        "config": {"max_tool_rounds": 50},
        "control": {"lifecycle_status": "active"},
    }
    apply_agent_content(agent, active_revision)

    assert agent.max_tool_rounds == 50, "config fields still roll back"
    assert agent.deactivated_at == deactivated_at, "rollback to an active revision must not revive"
    assert agent.deactivation_reason == "retirement_pending"
    assert agent.deleted_at is None

    deleted_revision = {
        "schema": "hive.ai_asset.agent.v1",
        "asset_type": "agent",
        "config": {"max_tool_rounds": 20},
        "control": {"lifecycle_status": "deleted"},
    }
    apply_agent_content(agent, deleted_revision)

    assert agent.max_tool_rounds == 20
    assert agent.deleted_at is None, "rollback to a deleted revision must not delete"
    assert agent.deactivated_at == deactivated_at, "rollback must not clear a deactivation either"


# ─── company identity retirement requires all bots deactivated ──────────


@pytest.mark.asyncio
async def test_tenant_retirement_accepts_deactivated_not_deleted_agents(monkeypatch) -> None:
    from tests.api.test_tenant_delete_api import (
        _FakeDB as TenantFakeDB,
        _ListResult,
        _ScalarResult,
        _patch_platform_bypass,
        _tenant,
    )
    import app.api.tenants as tenants_api
    from app.services import channel_secret_storage, tool_config_service
    from app.services.user_offboarding_service import AuthorityRevocationReceipt

    tenant_id = uuid4()
    fallback_tenant_id = uuid4()
    owner = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        is_active=True,
        quota_tokens_per_day=999,
        quota_tokens_per_month=9999,
        tokens_used_today=1,
        tokens_used_month=2,
        tokens_used_total=3,
        tokens_reset_at=object(),
    )
    last_admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id, department_id=uuid4(), is_active=True)
    # Deactivated but NOT deleted: under G1 this is the settled state retirement requires.
    deactivated_agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=last_admin.id,
        creator_id=last_admin.id,
        status="stopped",
        deleted_at=None,
        deactivated_at=datetime.now(timezone.utc),
    )
    db = TenantFakeDB(
        [
            _ListResult([]),
            _ListResult([_tenant(tenant_id), _tenant(fallback_tenant_id)]),
            _ScalarResult(None),
            _ListResult([owner, last_admin]),
            _ListResult([deactivated_agent]),
        ]
    )

    async def fake_revoke(_db, *, target_user, actor_user, now):
        return AuthorityRevocationReceipt(refresh_tokens=0, local_bridge_connections=0, runtime_tasks=0)

    async def no_scrub(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(tenants_api, "revoke_user_authority", fake_revoke, raising=False)
    monkeypatch.setattr(
        tenants_api, "publish_user_authority_runtime_cancellations", no_scrub, raising=False
    )
    monkeypatch.setattr(tool_config_service, "scrub_tenant_tool_secrets", no_scrub)
    monkeypatch.setattr(channel_secret_storage, "scrub_tenant_channel_secrets", no_scrub)
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    result = await tenants_api.delete_tenant(
        tenant_id=tenant_id,
        retirement=tenants_api.TenantRetirementRequest(
            expected_user_ids=[last_admin.id],
            reason="G1 fixture: deactivated agents must not block identity retirement",
            request_id="g1-retire-1",
        ),
        current_user=owner,
        db=db,
    )

    assert result.retirement_status == "retired"


@pytest.mark.asyncio
async def test_tenant_retirement_still_blocked_by_active_agent(monkeypatch) -> None:
    from tests.api.test_tenant_delete_api import (
        _FakeDB as TenantFakeDB,
        _ListResult,
        _ScalarResult,
        _patch_platform_bypass,
        _tenant,
    )
    import app.api.tenants as tenants_api

    tenant_id = uuid4()
    owner = SimpleNamespace(
        id=uuid4(),
        role="platform_admin",
        tenant_id=tenant_id,
        department_id=uuid4(),
        is_active=True,
        quota_tokens_per_day=999,
        quota_tokens_per_month=9999,
        tokens_used_today=1,
        tokens_used_month=2,
        tokens_used_total=3,
        tokens_reset_at=object(),
    )
    last_admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id, department_id=uuid4(), is_active=True)
    active_agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=last_admin.id,
        creator_id=last_admin.id,
        status="running",
        deleted_at=None,
        deactivated_at=None,
    )
    db = TenantFakeDB(
        [
            _ListResult([]),
            _ListResult([_tenant(tenant_id), _tenant(uuid4())]),
            _ScalarResult(None),
            _ListResult([owner, last_admin]),
            _ListResult([active_agent]),
        ]
    )
    _patch_platform_bypass(monkeypatch, tenants_api, db)

    with pytest.raises(HTTPException) as exc_info:
        await tenants_api.delete_tenant(
            tenant_id=tenant_id,
            retirement=tenants_api.TenantRetirementRequest(
                expected_user_ids=[last_admin.id],
                reason="G1 fixture: an active agent still blocks identity retirement",
                request_id="g1-retire-2",
            ),
            current_user=owner,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "tenant_retirement_owned_agents_active"
    assert exc_info.value.detail["agent_ids"] == [str(active_agent.id)]


# ─── conversations leave the caller's list, never the database ──────────


class _SessionResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _session_row(user_id, *, metadata=None):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=user_id,
        title="一次会话",
        parent_session_id=None,
        transcript_metadata_json=dict(metadata or {}),
    )


@pytest.mark.asyncio
async def test_remove_session_marks_own_list_without_deleting_rows(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api

    user_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member", tenant_id=uuid4())
    session = _session_row(user_id)
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id)
    db = _FakeDB([_SessionResult(session)])

    async def fake_check_agent_access(_db, _user, agent_id):
        assert agent_id == session.agent_id
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    await chat_sessions_api.delete_session(
        agent_id=session.agent_id, session_id=session.id, current_user=current_user, db=db
    )

    removed = session.transcript_metadata_json.get("removed_from_list_by")
    assert removed and str(user_id) in removed, session.transcript_metadata_json
    assert db.deleted == [], "the session row must stay in the database"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_remove_session_is_idempotent(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api

    user_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member", tenant_id=uuid4())
    earlier = "2026-11-04T00:00:00+00:00"
    session = _session_row(user_id, metadata={"removed_from_list_by": {str(user_id): earlier}})
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id)
    db = _FakeDB([_SessionResult(session)])

    async def fake_check_agent_access(_db, _user, agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    await chat_sessions_api.delete_session(
        agent_id=session.agent_id, session_id=session.id, current_user=current_user, db=db
    )

    assert session.transcript_metadata_json["removed_from_list_by"][str(user_id)] == earlier


@pytest.mark.asyncio
async def test_remove_session_refuses_anyone_but_the_owner(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api

    owner_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4())
    session = _session_row(owner_id)
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id)
    db = _FakeDB([_SessionResult(session)])

    async def fake_check_agent_access(_db, _user, agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.delete_session(
            agent_id=session.agent_id, session_id=session.id, current_user=admin, db=db
        )

    assert exc_info.value.status_code == 403
    assert "removed_from_list_by" not in (session.transcript_metadata_json or {})
    assert db.deleted == []
    assert db.commits == 0


def test_removed_from_own_list_marker_semantics() -> None:
    import app.api.chat_sessions as chat_sessions_api

    user_id = uuid4()
    other_id = uuid4()
    removed_session = _session_row(user_id, metadata={"removed_from_list_by": {str(user_id): "x"}})
    kept_session = _session_row(user_id, metadata={"removed_from_list_by": {str(other_id): "x"}})
    plain_session = _session_row(user_id)

    assert chat_sessions_api._removed_from_own_list(removed_session, user_id) is True
    assert chat_sessions_api._removed_from_own_list(kept_session, user_id) is False
    assert chat_sessions_api._removed_from_own_list(plain_session, user_id) is False
