from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushes = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


@pytest.mark.asyncio
async def test_transfer_loaded_agent_owner_changes_only_current_owner_and_audits(monkeypatch) -> None:
    from app.models.audit import AuditLog
    import app.services.agent_ownership_service as ownership_service
    from app.services.agent_ownership_service import transfer_loaded_agent_owner

    tenant_id = uuid4()
    creator_id = uuid4()
    sponsor_id = uuid4()
    old_owner_id = uuid4()
    new_owner_id = uuid4()
    actor_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        name="Finance Analyst",
        tenant_id=tenant_id,
        creator_id=creator_id,
        sponsor_user_id=sponsor_id,
        owner_user_id=old_owner_id,
    )
    new_owner = SimpleNamespace(
        id=new_owner_id,
        tenant_id=tenant_id,
        is_active=True,
        display_name="Admin Alice",
    )
    actor = SimpleNamespace(id=actor_id)
    db = _FakeDB()
    asset_syncs = []

    async def fake_register_agent_asset(_db, synced_agent, **kwargs):
        asset_syncs.append((synced_agent, kwargs))

    async def fake_rebind(*_args, **_kwargs):
        return [uuid4()]

    monkeypatch.setattr(ownership_service, "register_agent_asset", fake_register_agent_asset)
    monkeypatch.setattr(ownership_service, "_rebind_active_collaboration_memberships", fake_rebind)

    receipt = await transfer_loaded_agent_owner(
        db,
        agent=agent,
        new_owner=new_owner,
        actor=actor,
        reason="Member offboarding",
        expected_owner_id=old_owner_id,
        mode="user_offboarding",
        request_id="offboard-1",
    )

    assert receipt.status == "transferred"
    assert receipt.old_owner_id == old_owner_id
    assert receipt.new_owner_id == new_owner_id
    assert agent.owner_user_id == new_owner_id
    assert agent.creator_id == creator_id
    assert agent.sponsor_user_id == sponsor_id
    audits = [item for item in db.added if isinstance(item, AuditLog)]
    assert len(audits) == 1
    assert audits[0].action == "agent:handover"
    assert audits[0].details["mode"] == "user_offboarding"
    assert audits[0].details["reason"] == "Member offboarding"
    assert audits[0].details["request_id"] == "offboard-1"
    assert len(audits[0].details["a2a_memberships_pending_reconfirmation"]) == 1
    assert asset_syncs[0][0] is agent
    assert asset_syncs[0][1]["change_source"] == "owner_transfer"
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_transfer_loaded_agent_owner_rejects_stale_expected_owner() -> None:
    from app.services.agent_ownership_service import transfer_loaded_agent_owner

    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        name="Ops Analyst",
        tenant_id=tenant_id,
        creator_id=uuid4(),
        sponsor_user_id=uuid4(),
        owner_user_id=uuid4(),
    )
    new_owner = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, is_active=True, display_name="Admin")

    with pytest.raises(HTTPException) as exc:
        await transfer_loaded_agent_owner(
            _FakeDB(),
            agent=agent,
            new_owner=new_owner,
            actor=SimpleNamespace(id=uuid4()),
            reason="Manual transfer",
            expected_owner_id=uuid4(),
            mode="manual_admin",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "agent_owner_changed"


@pytest.mark.asyncio
async def test_transfer_loaded_agent_owner_rejects_inactive_or_cross_tenant_target() -> None:
    from app.services.agent_ownership_service import transfer_loaded_agent_owner

    tenant_id = uuid4()
    current_owner_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        name="Ops Analyst",
        tenant_id=tenant_id,
        creator_id=uuid4(),
        sponsor_user_id=uuid4(),
        owner_user_id=current_owner_id,
    )
    actor = SimpleNamespace(id=uuid4())

    with pytest.raises(HTTPException) as inactive_exc:
        await transfer_loaded_agent_owner(
            _FakeDB(),
            agent=agent,
            new_owner=SimpleNamespace(id=uuid4(), tenant_id=tenant_id, is_active=False, display_name="Inactive"),
            actor=actor,
            reason="Manual transfer",
            expected_owner_id=current_owner_id,
            mode="manual_admin",
        )
    assert inactive_exc.value.status_code == 400

    with pytest.raises(HTTPException) as tenant_exc:
        await transfer_loaded_agent_owner(
            _FakeDB(),
            agent=agent,
            new_owner=SimpleNamespace(id=uuid4(), tenant_id=uuid4(), is_active=True, display_name="Foreign"),
            actor=actor,
            reason="Manual transfer",
            expected_owner_id=current_owner_id,
            mode="manual_admin",
        )
    assert tenant_exc.value.status_code == 400
