from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


class _PermissionUpdateDB:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.added: list[object] = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


class _GrantResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(all=lambda: self.value)


class _GrantDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False

    async def execute(self, _statement, _params=None):
        self.statements.append((_statement, _params))
        return _GrantResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_org_admin_can_update_same_tenant_agent_permissions(monkeypatch):
    from app.api import agents as agents_api

    agent_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=tenant_id)
    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    db = _PermissionUpdateDB()

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)

    result = await agents_api.update_agent_permissions(
        agent_id,
        {"scope_type": "company", "scope_ids": [], "access_level": "use"},
        current_user=current_user,
        db=db,
    )

    assert result == {"status": "ok"}
    assert db.committed is True
    assert len(db.added) == 1
    assert db.added[0].scope_type == "company"
    assert db.added[0].access_level == "use"


@pytest.mark.asyncio
async def test_member_non_owner_cannot_update_agent_permissions(monkeypatch):
    from app.api import agents as agents_api

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=agent.tenant_id)
    db = _PermissionUpdateDB()

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        raise HTTPException(status_code=403, detail="Manage access required")

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    with pytest.raises(HTTPException) as exc:
        await agents_api.update_agent_permissions(
            agent_id,
            {"scope_type": "company", "scope_ids": [], "access_level": "use"},
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403
    assert db.committed is False


@pytest.mark.asyncio
async def test_delegated_manage_permission_does_not_disclose_permission_principals(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), creator_id=uuid4(), owner_user_id=uuid4(), tenant_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=agent.tenant_id)

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)

    result = await agents_api.get_agent_permissions(agent.id, current_user=current_user, db=object())

    assert result == {
        "scope_type": "effective",
        "scope_ids": [],
        "scope_names": [],
        "access_level": "manage",
        "is_owner": False,
        "can_manage_permissions": False,
    }


@pytest.mark.asyncio
async def test_legacy_company_manage_permission_is_reported_as_use(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), creator_id=uuid4(), owner_user_id=uuid4(), tenant_id=uuid4())
    current_user = SimpleNamespace(id=agent.owner_user_id, role="member", tenant_id=agent.tenant_id)
    permission = SimpleNamespace(scope_type="company", scope_id=None, access_level="manage")
    db = _GrantDB([[permission]])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)

    result = await agents_api.get_agent_permissions(agent.id, current_user=current_user, db=db)

    assert result["scope_type"] == "company"
    assert result["access_level"] == "use"


@pytest.mark.asyncio
async def test_company_permission_rejects_manage_without_silent_downgrade(monkeypatch):
    from app.api import agents as agents_api

    agent_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), owner_user_id=uuid4(), tenant_id=tenant_id)
    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    db = _PermissionUpdateDB()

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    with pytest.raises(HTTPException) as exc:
        await agents_api.update_agent_permissions(
            agent_id,
            {"scope_type": "company", "scope_ids": [], "access_level": "manage"},
            current_user=current_user,
            db=db,
        )
    assert exc.value.status_code == 422
    assert db.added == []

    with pytest.raises(ValidationError):
        await agents_api.update_agent_permissions(
            agent_id,
            {"scope_type": "company", "scope_ids": [uuid4()], "access_level": "use"},
            current_user=current_user,
            db=db,
        )
    assert db.added == []


@pytest.mark.asyncio
async def test_operator_inspection_grant_is_targeted_audited_and_revocable(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    operator = SimpleNamespace(
        id=uuid4(),
        display_name="Operator",
        username="operator",
        tenant_id=agent.tenant_id,
        is_active=True,
    )
    grant_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db = _GrantDB([None, None, operator])
    audit_calls = []

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    async def fake_validate_users(_db, *, tenant_id, user_ids):
        assert tenant_id == agent.tenant_id
        return user_ids

    async def fake_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)
    monkeypatch.setattr(agents_api, "_validate_active_tenant_users", fake_validate_users)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)

    created = await agents_api.create_agent_operator_grant(
        agent.id,
        agents_api.AgentOperatorGrantIn(
            request_id=grant_id,
            principal_id=operator.id,
            effect="allow",
            expires_at=expires_at,
            reason="Production incident review",
        ),
        current_user=admin,
        db=db,
    )

    permission = db.added[0]
    assert created["id"] == str(grant_id)
    assert permission.principal_id == operator.id
    assert permission.actions == ["operator.inspect"]
    assert permission.effect == "allow"
    assert permission.expires_at == expires_at
    assert audit_calls[0]["request_id"] == grant_id
    assert db.committed is True
    assert db.statements[0][1] == {"lock_key": f"agent-operator-grant-create:{grant_id}"}

    revoke_request_id = uuid4()
    revoke_db = _GrantDB([None, None, permission])
    audit_calls.clear()
    revoked = await agents_api.revoke_agent_operator_grant(
        agent.id,
        grant_id,
        agents_api.AgentOperatorGrantRevokeIn(
            request_id=revoke_request_id,
            reason="Incident review complete",
        ),
        current_user=admin,
        db=revoke_db,
    )

    assert revoked["revoked_at"] is not None
    assert permission.revoked_by_user_id == admin.id
    assert permission.conditions["operator_inspection"]["revocation_request_id"] == str(revoke_request_id)
    assert len(permission.conditions["operator_inspection"]["revocation_request_hash"]) == 64
    assert audit_calls[0]["event_type"] == "agent.operator_grant_revoked"
    assert revoke_db.committed is True


@pytest.mark.asyncio
async def test_operator_grant_exact_replay_ignores_later_expiry_and_principal_state(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    principal_id = uuid4()
    request_id = uuid4()
    expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    reason = "Completed incident inspection"
    request_hash = agents_api.hashlib.sha256(
        agents_api.json.dumps(
            {
                "agent_id": str(agent.id),
                "principal_id": str(principal_id),
                "effect": "allow",
                "expires_at": expires_at.isoformat(),
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    permission = SimpleNamespace(
        id=request_id,
        principal_id=principal_id,
        effect="allow",
        actions=[agents_api.AGENT_OPERATOR_INSPECT_ACTION],
        expires_at=expires_at,
        revoked_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        created_by_user_id=admin.id,
        revoked_by_user_id=None,
        conditions={"operator_inspection": {"request_hash": request_hash}},
    )
    inactive_user = SimpleNamespace(display_name="Former operator", username="former-operator")
    db = _GrantDB([None, permission, inactive_user])

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    async def unexpected_validate(*_args, **_kwargs):
        raise AssertionError("exact replay must not depend on current principal state")

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)
    monkeypatch.setattr(agents_api, "_validate_active_tenant_users", unexpected_validate)

    replay = await agents_api.create_agent_operator_grant(
        agent.id,
        agents_api.AgentOperatorGrantIn(
            request_id=request_id,
            principal_id=principal_id,
            effect="allow",
            expires_at=expires_at,
            reason=reason,
        ),
        current_user=admin,
        db=db,
    )

    assert replay["id"] == str(request_id)
    assert replay["principal_name"] == "Former operator"
    assert db.committed is True
    assert db.added == []


@pytest.mark.asyncio
async def test_operator_grant_unique_race_rolls_back_as_conflict(monkeypatch):
    from app.api import agents as agents_api

    class _ConflictingGrantDB(_GrantDB):
        async def flush(self):
            self.flushed = True
            raise IntegrityError("INSERT resource_permissions", {}, RuntimeError("duplicate request id"))

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    principal_id = uuid4()
    db = _ConflictingGrantDB([None, None])

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    async def fake_validate_users(_db, *, tenant_id, user_ids):
        assert tenant_id == agent.tenant_id
        return user_ids

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)
    monkeypatch.setattr(agents_api, "_validate_active_tenant_users", fake_validate_users)

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.create_agent_operator_grant(
            agent.id,
            agents_api.AgentOperatorGrantIn(
                request_id=uuid4(),
                principal_id=principal_id,
                effect="allow",
                reason="Concurrent inspection request",
            ),
            current_user=admin,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert db.flushed is True
    assert db.rolled_back is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_operator_candidates_include_the_current_owner(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    owner = SimpleNamespace(
        id=agent.owner_user_id,
        display_name="Owner",
        username="owner",
        email="owner@example.test",
        role="member",
    )
    db = _GrantDB([[owner]])

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    result = await agents_api.list_agent_operator_candidates(agent.id, current_user=admin, db=db)

    assert result == [
        {
            "id": str(owner.id),
            "display_name": "Owner",
            "email": "owner@example.test",
            "role": "member",
        }
    ]


@pytest.mark.asyncio
async def test_operator_grant_creation_fails_closed_when_audit_write_fails(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    operator = SimpleNamespace(
        id=uuid4(),
        display_name="Operator",
        username="operator",
        tenant_id=agent.tenant_id,
        is_active=True,
    )
    db = _GrantDB([None, None])

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    async def fake_validate_users(_db, *, tenant_id, user_ids):
        return user_ids

    async def failing_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)
    monkeypatch.setattr(agents_api, "_validate_active_tenant_users", fake_validate_users)
    monkeypatch.setattr("app.core.policy.write_audit_event", failing_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await agents_api.create_agent_operator_grant(
            agent.id,
            agents_api.AgentOperatorGrantIn(
                request_id=uuid4(),
                principal_id=operator.id,
                effect="deny",
                reason="Temporary deny during review",
            ),
            current_user=admin,
            db=db,
        )

    assert db.flushed is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_operator_grant_revocation_request_is_exactly_idempotent(monkeypatch):
    from app.api import agents as agents_api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), owner_user_id=uuid4())
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id)
    grant_id = uuid4()
    request_id = uuid4()
    reason = "Incident review complete"
    request_hash = agents_api.hashlib.sha256(
        agents_api.json.dumps(
            {"agent_id": str(agent.id), "grant_id": str(grant_id), "reason": reason},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    permission = SimpleNamespace(
        id=grant_id,
        tenant_id=agent.tenant_id,
        principal_id=uuid4(),
        principal_type="user",
        resource_type="agent",
        resource_id=agent.id,
        actions=["operator.inspect"],
        conditions={
            "operator_inspection": {
                "schema": agents_api.AGENT_OPERATOR_INSPECTION_SCHEMA,
                "revocation_request_id": str(request_id),
                "revocation_request_hash": request_hash,
            }
        },
        effect="allow",
        expires_at=None,
        revoked_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        created_by_user_id=admin.id,
        revoked_by_user_id=admin.id,
    )

    async def fake_require_owner_or_admin(_db, _user, _agent_id, *, lock=False):
        return agent

    monkeypatch.setattr(agents_api, "require_agent_owner_or_admin", fake_require_owner_or_admin)

    replay_db = _GrantDB([None, permission])
    replayed = await agents_api.revoke_agent_operator_grant(
        agent.id,
        grant_id,
        agents_api.AgentOperatorGrantRevokeIn(request_id=request_id, reason=reason),
        current_user=admin,
        db=replay_db,
    )
    assert replayed["id"] == str(grant_id)
    assert replay_db.committed is True

    conflict_db = _GrantDB([None, permission])
    with pytest.raises(HTTPException) as exc:
        await agents_api.revoke_agent_operator_grant(
            agent.id,
            uuid4(),
            agents_api.AgentOperatorGrantRevokeIn(request_id=request_id, reason=reason),
            current_user=admin,
            db=conflict_db,
        )
    assert exc.value.status_code == 409
