from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_team_requested_set_commits_exact_recovery_intent_under_producer_lease(monkeypatch) -> None:
    import app.services.agent_team_runtime_service as runtime

    captured = []
    root_rows = []

    async def fake_register_runtime_root_item(_db, **kwargs):
        captured.append(kwargs)
        row = SimpleNamespace(
            state="requested",
            runtime_task_id=None,
            recovery_claimed_by=None,
            recovery_claim_expires_at=None,
        )
        root_rows.append(row)
        return row

    class DB:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    monkeypatch.setattr(runtime, "register_runtime_root_item", fake_register_runtime_root_item)
    db = DB()
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    member = SimpleNamespace(id=member_id, member_name="critic", chat_session_id=uuid4())
    budget_run_id = uuid4()

    await runtime._register_team_fanout_requested_set(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        team=SimpleNamespace(id=team_id, parent_session_id=uuid4()),
        operation_id="fanout-op",
        root_runtime_task_id=uuid4(),
        message="Review the exact artifact.",
        work_items=[
            {
                "member": member,
                "intent_key": f"team:{team_id}:member:{member_id}",
                "target_ref": f"team-member:{member_id}",
                "reservation_key": "reservation-key",
                "ordinal": 7,
                "message_sha256": "sha256",
            }
        ],
        source="unit_test",
        display_content="Review artifact",
        interrupt_requested=True,
        budget_run_id=budget_run_id,
        reserve_new_team_sessions=True,
    )

    assert db.commits == 1
    assert captured[0]["state"] == "requested"
    assert captured[0]["admission_disposition"] == "requested"
    assert captured[0]["metadata"] == {
        "schema": "hive.runtime_root_team_intent.v1",
        "operation_id": "fanout-op",
        "ordinal": 7,
        "team_id": str(team_id),
        "member_id": str(member_id),
        "member_name": "critic",
        "message": "Review the exact artifact.",
        "display_content": "Review artifact",
        "message_sha256": "sha256",
        "source": "unit_test",
        "budget_run_id": str(budget_run_id),
        "reserve_new_team_sessions": True,
        "interrupt_requested": True,
    }
    assert root_rows[0].recovery_claimed_by == "fanout-producer:fanout-op"
    assert root_rows[0].recovery_claim_expires_at is not None


def test_team_fanout_recovery_intent_requires_complete_exact_identity() -> None:
    from app.services.team_fanout_recovery import claimed_team_fanout_item

    row = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        root_runtime_task_id=uuid4(),
        source_agent_id=uuid4(),
        root_user_id=uuid4(),
        root_session_id=str(uuid4()),
        intent_key="team:one:member:two",
        metadata_json={
            "schema": "hive.runtime_root_team_intent.v1",
            "team_id": str(uuid4()),
            "member_id": str(uuid4()),
            "operation_id": "fanout-operation",
            "message": "Complete the exact delegated task.",
            "source": "agent_team",
            "ordinal": 7,
            "budget_run_id": str(uuid4()),
            "reserve_new_team_sessions": True,
            "interrupt_requested": False,
        },
        recovery_attempt_count=2,
    )

    claimed = claimed_team_fanout_item(row)

    assert claimed.id == row.id
    assert claimed.operation_id == "fanout-operation"
    assert claimed.message == "Complete the exact delegated task."
    assert claimed.ordinal == 7
    assert claimed.reserve_new_team_sessions is True
    assert claimed.attempt_count == 2


def test_team_fanout_recovery_intent_rejects_missing_message_without_inventing_work() -> None:
    from app.services.team_fanout_recovery import claimed_team_fanout_item

    row = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        root_runtime_task_id=uuid4(),
        source_agent_id=uuid4(),
        root_user_id=uuid4(),
        root_session_id=str(uuid4()),
        intent_key="team:one:member:two",
        metadata_json={
            "schema": "hive.runtime_root_team_intent.v1",
            "team_id": str(uuid4()),
            "member_id": str(uuid4()),
            "operation_id": "fanout-operation",
        },
        recovery_attempt_count=1,
    )

    with pytest.raises(ValueError, match="message"):
        claimed_team_fanout_item(row)


@pytest.mark.asyncio
async def test_team_fanout_recovery_retries_then_holds_instead_of_spinning_forever(monkeypatch):
    from app.services.team_fanout_recovery import TeamFanoutRecoveryService

    item = SimpleNamespace(id=uuid4(), attempt_count=3)
    retries = []

    async def fake_claim_batch(**_kwargs):
        return [item]

    async def fake_retry(claimed, *, error, now=None):
        retries.append((claimed, error))
        return "needs_reconciliation"

    async def fail_delivery(_claimed):
        raise RuntimeError("provider unavailable")

    service = TeamFanoutRecoveryService(max_attempts=3)
    monkeypatch.setattr(service, "claim_batch", fake_claim_batch)
    monkeypatch.setattr(service, "_retry_or_hold", fake_retry)

    result = await service.drain_once(worker_id="worker", deliver=fail_delivery)

    assert result == {
        "claimed": 1,
        "recovered": 0,
        "retried": 0,
        "needs_reconciliation": 1,
    }
    assert retries and "provider unavailable" in retries[0][1]
