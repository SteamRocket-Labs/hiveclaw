from __future__ import annotations

import uuid
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(self, values=()):
        self.values = list(values)
        self.commits = 0

    async def execute(self, _statement):
        return _ScalarResult(self.values.pop(0))

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_offboarding_preview_exposes_agents_successors_and_revocation_impact(monkeypatch) -> None:
    import app.api.users as users_api

    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member")
    actor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    successor_id = uuid4()
    agent_id = uuid4()

    async def fake_load(*_args, **_kwargs):
        return target

    async def fake_preview(*_args, **_kwargs):
        return SimpleNamespace(
            user_id=target.id,
            display_name="Bob",
            is_active=True,
            owned_agents=[{"id": str(agent_id), "name": "Agent A", "status": "idle", "agent_class": "internal_tenant"}],
            eligible_successors=[
                {"id": str(successor_id), "display_name": "Admin", "email": "admin@example.com", "role": "org_admin"}
            ],
            default_successor_id=successor_id,
            agent_permissions=2,
            resource_permissions=1,
            knowledge_grants=3,
            refresh_tokens=4,
            external_principals=1,
            local_bridge_connections=1,
            runtime_tasks=2,
            pending_approvals=1,
        )

    monkeypatch.setattr(users_api, "_load_target_user", fake_load)
    monkeypatch.setattr(users_api, "build_user_offboarding_preview", fake_preview)

    result = await users_api.preview_user_offboarding(
        user_id=target.id,
        tenant_id=str(tenant_id),
        current_user=actor,
        db=_FakeDB(),
    )

    assert result["owned_agents"][0]["id"] == str(agent_id)
    assert result["default_successor_id"] == str(successor_id)
    assert result["revocations"]["refresh_tokens"] == 4
    assert result["revocations"]["runtime_tasks"] == 2
    assert result["revocations"]["pending_approvals"] == 1
    assert result["blockers"] == []


@pytest.mark.asyncio
async def test_offboarding_route_passes_preview_snapshot_to_atomic_service(monkeypatch) -> None:
    import app.api.users as users_api
    from app.services.user_offboarding_service import AuthorityRevocationReceipt, UserOffboardingReceipt

    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member")
    successor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin", is_active=True)
    actor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    agent_ids = [uuid4(), uuid4()]
    calls = []
    order = []

    async def fake_pin(_db, _current_user, requested_tenant_id=None):
        return uuid.UUID(str(requested_tenant_id)) if requested_tenant_id else tenant_id

    async def fake_load(*_args, **_kwargs):
        order.append("load_target")
        return target

    async def fake_prelock(_db, **kwargs):
        order.append("prelock_pairings")
        calls.append(kwargs)

    async def fake_offboard(db, **kwargs):
        calls.append((db, kwargs))
        return UserOffboardingReceipt(
            status="deactivated",
            user_id=target.id,
            successor_user_id=successor.id,
            transferred_agent_ids=agent_ids,
            revocations=AuthorityRevocationReceipt(agent_permissions=1, refresh_tokens=2),
            request_id="offboard-1",
        )

    async def fake_replay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "resolve_and_pin_tenant_scope", fake_pin)
    monkeypatch.setattr(users_api, "_load_target_user", fake_load)
    monkeypatch.setattr(users_api, "_lock_target_user_claimable_pairings", fake_prelock)
    monkeypatch.setattr(users_api, "find_user_offboarding_replay", fake_replay)
    monkeypatch.setattr(users_api, "offboard_loaded_user", fake_offboard)
    db = _FakeDB([successor])

    result = await users_api.offboard_user(
        user_id=target.id,
        data=users_api.UserOffboardingRequest(
            successor_user_id=successor.id,
            expected_agent_ids=agent_ids,
            reason="Employment ended",
            request_id="offboard-1",
        ),
        tenant_id=str(tenant_id),
        current_user=actor,
        db=db,
    )

    # The claimable-pairing prelock must precede the target identity lock:
    # pairing→identity is the global lock order shared with device-code
    # exchange (see the real-PostgreSQL proof in
    # tests/integration/test_user_offboarding.py).
    assert order[:2] == ["prelock_pairings", "load_target"]
    assert calls[0] == {"tenant_id": tenant_id, "user_id": target.id}
    assert calls[1][1]["expected_agent_ids"] == agent_ids
    assert calls[1][1]["successor"] is successor
    assert result["status"] == "deactivated"
    assert result["transferred_agent_count"] == 2
    assert result["revocations"]["refresh_tokens"] == 2
    assert db.commits == 1


@pytest.mark.asyncio
async def test_offboarding_route_returns_committed_idempotent_replay(monkeypatch) -> None:
    import app.api.users as users_api
    from app.services.user_offboarding_service import AuthorityRevocationReceipt, UserOffboardingReceipt

    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member")
    successor_id = uuid4()
    actor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    agent_id = uuid4()

    async def fake_pin(_db, _current_user, requested_tenant_id=None):
        return uuid.UUID(str(requested_tenant_id)) if requested_tenant_id else tenant_id

    async def fake_load(*_args, **_kwargs):
        return target

    async def fake_prelock(*_args, **_kwargs):
        return None

    async def fake_replay(*_args, **_kwargs):
        return UserOffboardingReceipt(
            status="deactivated",
            user_id=target.id,
            successor_user_id=successor_id,
            transferred_agent_ids=[agent_id],
            revocations=AuthorityRevocationReceipt(refresh_tokens=2),
            request_id="offboard-retry",
        )

    async def unexpected_offboard(*_args, **_kwargs):
        raise AssertionError("idempotent replay must not execute effects again")

    monkeypatch.setattr(users_api, "resolve_and_pin_tenant_scope", fake_pin)
    monkeypatch.setattr(users_api, "_load_target_user", fake_load)
    monkeypatch.setattr(users_api, "_lock_target_user_claimable_pairings", fake_prelock)
    monkeypatch.setattr(users_api, "find_user_offboarding_replay", fake_replay)
    monkeypatch.setattr(users_api, "offboard_loaded_user", unexpected_offboard)

    db = _FakeDB()
    result = await users_api.offboard_user(
        user_id=target.id,
        data=users_api.UserOffboardingRequest(
            successor_user_id=successor_id,
            expected_agent_ids=[agent_id],
            reason="Employment ended",
            request_id="offboard-retry",
        ),
        tenant_id=str(tenant_id),
        current_user=actor,
        db=db,
    )

    assert result["status"] == "deactivated"
    assert result["transferred_agent_ids"] == [str(agent_id)]
    assert result["revocations"]["refresh_tokens"] == 2
    assert db.commits == 1


@pytest.mark.asyncio
async def test_offboarding_route_rejects_self_offboarding_before_any_effect(monkeypatch) -> None:
    import app.api.users as users_api

    tenant_id = uuid4()
    actor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")

    async def fake_pin(_db, _current_user, requested_tenant_id=None):
        return uuid.UUID(str(requested_tenant_id)) if requested_tenant_id else tenant_id

    async def fake_load(*_args, **_kwargs):
        return actor

    async def fake_prelock(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "resolve_and_pin_tenant_scope", fake_pin)
    monkeypatch.setattr(users_api, "_load_target_user", fake_load)
    monkeypatch.setattr(users_api, "_lock_target_user_claimable_pairings", fake_prelock)
    db = _FakeDB()
    with pytest.raises(HTTPException) as exc:
        await users_api.offboard_user(
            user_id=actor.id,
            data=users_api.UserOffboardingRequest(
                successor_user_id=uuid4(),
                expected_agent_ids=[],
                reason="Employment ended",
                request_id="self-offboard",
            ),
            tenant_id=str(tenant_id),
            current_user=actor,
            db=db,
        )

    assert getattr(exc.value, "status_code", None) == 400
    assert db.commits == 0
