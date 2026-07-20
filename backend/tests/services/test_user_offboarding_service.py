from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_offboard_user_atomically_transfers_agents_then_deactivates(monkeypatch) -> None:
    import app.services.user_offboarding_service as service
    import app.services.agent_ownership_service as ownership_service

    tenant_id = uuid4()
    target_id = uuid4()
    successor_id = uuid4()
    actor_id = uuid4()
    target = SimpleNamespace(id=target_id, tenant_id=tenant_id, role="member", is_active=True, display_name="Bob")
    successor = SimpleNamespace(
        id=successor_id,
        tenant_id=tenant_id,
        role="org_admin",
        is_active=True,
        display_name="Admin Alice",
    )
    actor = SimpleNamespace(id=actor_id, tenant_id=tenant_id, role="org_admin")
    agents = [
        SimpleNamespace(
            id=uuid4(),
            name="Agent A",
            tenant_id=tenant_id,
            creator_id=target_id,
            sponsor_user_id=target_id,
            owner_user_id=target_id,
        ),
        SimpleNamespace(
            id=uuid4(),
            name="Agent B",
            tenant_id=tenant_id,
            creator_id=target_id,
            sponsor_user_id=target_id,
            owner_user_id=target_id,
        ),
    ]

    async def fake_lock_owned_agents(_db, *, target_user_id, tenant_id):
        assert target_user_id == target_id
        assert tenant_id == target.tenant_id
        return agents

    async def fake_revoke(_db, *, target_user, actor_user, now):
        assert target_user is target
        assert actor_user is actor
        return service.AuthorityRevocationReceipt(
            agent_permissions=3,
            resource_permissions=2,
            knowledge_grants=4,
            refresh_tokens=2,
            external_principals=1,
            local_bridge_connections=1,
        )

    monkeypatch.setattr(service, "_lock_owned_agents", fake_lock_owned_agents)
    monkeypatch.setattr(service, "_revoke_user_authority", fake_revoke)

    async def fake_register_agent_asset(*_args, **_kwargs):
        return None

    async def fake_rebind(*_args, **_kwargs):
        return []

    monkeypatch.setattr(ownership_service, "register_agent_asset", fake_register_agent_asset)
    monkeypatch.setattr(ownership_service, "_rebind_active_collaboration_memberships", fake_rebind)

    db = SimpleNamespace(added=[], flushes=0)
    db.add = db.added.append

    async def flush():
        db.flushes += 1

    db.flush = flush

    receipt = await service.offboard_loaded_user(
        db,
        target_user=target,
        successor=successor,
        actor=actor,
        expected_agent_ids=[agent.id for agent in agents],
        reason="Employment ended",
        request_id="offboard-1",
    )

    assert target.is_active is False
    assert [agent.owner_user_id for agent in agents] == [successor_id, successor_id]
    assert [agent.creator_id for agent in agents] == [target_id, target_id]
    assert [agent.sponsor_user_id for agent in agents] == [target_id, target_id]
    assert receipt.status == "deactivated"
    assert receipt.transferred_agent_ids == [agent.id for agent in agents]
    assert receipt.revocations.refresh_tokens == 2


@pytest.mark.asyncio
async def test_offboard_user_rejects_stale_preview_without_partial_change(monkeypatch) -> None:
    import app.services.user_offboarding_service as service

    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member", is_active=True, display_name="Bob")
    successor = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, role="org_admin", is_active=True, display_name="Admin Alice"
    )
    actor = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    agent = SimpleNamespace(
        id=uuid4(),
        name="Agent A",
        tenant_id=tenant_id,
        creator_id=target.id,
        sponsor_user_id=target.id,
        owner_user_id=target.id,
    )

    async def fake_lock_owned_agents(*_args, **_kwargs):
        return [agent]

    monkeypatch.setattr(service, "_lock_owned_agents", fake_lock_owned_agents)

    with pytest.raises(HTTPException) as exc:
        await service.offboard_loaded_user(
            SimpleNamespace(),
            target_user=target,
            successor=successor,
            actor=actor,
            expected_agent_ids=[],
            reason="Employment ended",
            request_id="offboard-2",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "offboarding_preview_stale"
    assert target.is_active is True
    assert agent.owner_user_id == target.id


@pytest.mark.asyncio
async def test_offboarding_replay_recovers_receipt_and_rejects_key_reuse() -> None:
    import app.services.user_offboarding_service as service

    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    successor_id = uuid4()
    agent_id = uuid4()
    audit = SimpleNamespace(
        details={
            "target_user_id": str(target.id),
            "successor_user_id": str(successor_id),
            "expected_agent_ids": [str(agent_id)],
            "transferred_agent_ids": [str(agent_id)],
            "reason": "Employment ended",
            "request_id": "offboard-1",
            "already_inactive": False,
            "revocations": {"refresh_tokens": 2, "external_principals": 1},
        }
    )
    db = SimpleNamespace()

    async def execute(_statement):
        return _ScalarResult(audit)

    db.execute = execute
    receipt = await service.find_user_offboarding_replay(
        db,
        target_user=target,
        successor_user_id=successor_id,
        expected_agent_ids=[agent_id],
        reason="Employment ended",
        request_id="offboard-1",
    )
    assert receipt is not None
    assert receipt.transferred_agent_ids == [agent_id]
    assert receipt.revocations.refresh_tokens == 2

    normalized_retry = await service.find_user_offboarding_replay(
        db,
        target_user=target,
        successor_user_id=successor_id,
        expected_agent_ids=[agent_id],
        reason="  Employment ended  ",
        request_id="  offboard-1  ",
    )
    assert normalized_retry is not None
    assert normalized_retry.request_id == "offboard-1"

    with pytest.raises(HTTPException) as exc:
        await service.find_user_offboarding_replay(
            db,
            target_user=target,
            successor_user_id=successor_id,
            expected_agent_ids=[agent_id],
            reason="Different request",
            request_id="offboard-1",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "offboarding_idempotency_conflict"


@pytest.mark.asyncio
async def test_offboarding_runtime_signals_cover_supported_inflight_task_types(monkeypatch) -> None:
    import app.services.runtime_control_bus as control_bus
    import app.services.user_offboarding_service as service

    user_id = uuid4()
    agent_id = uuid4()
    calls: list[tuple[str, dict]] = []

    async def record(kind, **kwargs):
        calls.append((kind, kwargs))

    monkeypatch.setattr(control_bus, "publish_web_chat_cancel", lambda **kwargs: record("chat", **kwargs))
    monkeypatch.setattr(control_bus, "publish_business_task_cancel", lambda **kwargs: record("business", **kwargs))
    monkeypatch.setattr(control_bus, "publish_delegation_cancel", lambda **kwargs: record("delegation", **kwargs))
    monkeypatch.setattr(control_bus, "publish_subagent_cancel", lambda **kwargs: record("subagent", **kwargs))

    receipt = service.UserOffboardingReceipt(
        status="deactivated",
        user_id=user_id,
        successor_user_id=uuid4(),
        transferred_agent_ids=[],
        revocations=service.AuthorityRevocationReceipt(
            runtime_tasks=4,
            runtime_task_signals=(
                service.RuntimeTaskRevocationSignal(
                    task_id=uuid4(),
                    task_type="web_chat_turn",
                    parent_agent_id=agent_id,
                    parent_session_id=str(uuid4()),
                ),
                service.RuntimeTaskRevocationSignal(
                    task_id=uuid4(),
                    task_type="business_task",
                    business_task_id=uuid4(),
                ),
                service.RuntimeTaskRevocationSignal(
                    task_id=uuid4(),
                    task_type="delegation",
                    parent_agent_id=agent_id,
                ),
                service.RuntimeTaskRevocationSignal(
                    task_id=uuid4(),
                    task_type="subagent",
                    parent_agent_id=agent_id,
                ),
            ),
        ),
        request_id="offboard-signals",
    )

    await service.publish_user_offboarding_runtime_cancellations(receipt)

    assert [kind for kind, _kwargs in calls] == ["chat", "business", "delegation", "subagent"]
    assert calls[0][1]["user_id"] == user_id
