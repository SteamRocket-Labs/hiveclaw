from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_root_coverage_conserves_requested_admitted_and_deferred() -> None:
    from app.services.runtime_root_ledger import summarize_runtime_root_items

    items = [
        *[SimpleNamespace(admission_disposition="admitted", state="completed") for _ in range(63)],
        *[SimpleNamespace(admission_disposition="deferred", state="waiting_approval") for _ in range(22)],
        *[SimpleNamespace(admission_disposition="not_admitted", state="not_admitted") for _ in range(15)],
    ]

    coverage = summarize_runtime_root_items(items)

    assert coverage.requested == 100
    assert coverage.admitted == 63
    assert coverage.deferred == 22
    assert coverage.not_admitted == 15
    assert coverage.requested == coverage.admitted + coverage.deferred + coverage.not_admitted
    assert coverage.expected == coverage.admitted
    assert coverage.terminal == 63


def test_unclassified_requested_item_is_recoverable_deferred_work() -> None:
    from app.services.runtime_root_ledger import summarize_runtime_root_items

    coverage = summarize_runtime_root_items([SimpleNamespace(admission_disposition="requested", state="requested")])

    assert coverage.to_dict() == {
        "requested": 1,
        "admitted": 0,
        "deferred": 1,
        "not_admitted": 0,
        "expected": 0,
        "terminal": 0,
        "running": 0,
        "waiting_approval": 0,
        "conserved": True,
    }


def test_root_path_cycle_is_durable_fact_not_process_memory() -> None:
    from app.services.runtime_root_ledger import build_runtime_root_path

    decision = build_runtime_root_path(
        ["agent:a", "agent:b", "workflow:review"],
        target_ref="agent:a",
    )

    assert decision.cycle_detected is True
    assert decision.reason_code == "runtime_root_cycle_detected"
    assert decision.path == ("agent:a", "agent:b", "workflow:review", "agent:a")


def test_root_terminal_transition_is_monotonic() -> None:
    from app.services.runtime_root_ledger import evaluate_runtime_root_transition

    late = evaluate_runtime_root_transition(current_state="killed", requested_state="completed")
    retry = evaluate_runtime_root_transition(current_state="needs_reconciliation", requested_state="running")

    assert late.applied is False
    assert late.reason_code == "terminal_state_already_sealed"
    assert late.effective_state == "killed"
    assert retry.applied is True
    assert retry.effective_state == "running"


@pytest.mark.parametrize("size", [1, 10, 25, 50, 100])
def test_mixed_root_fanout_capacity_curve_conserves_every_requested_item(size: int) -> None:
    from app.services.runtime_root_ledger import summarize_runtime_root_items

    work_types = ("direct", "team_member", "workflow")
    items = []
    for index in range(size):
        disposition = ("admitted", "deferred", "not_admitted")[index % 3]
        state = {
            "admitted": "completed" if index % 2 == 0 else "running",
            "deferred": "waiting_approval",
            "not_admitted": "not_admitted",
        }[disposition]
        items.append(
            SimpleNamespace(
                work_type=work_types[index % len(work_types)],
                admission_disposition=disposition,
                state=state,
            )
        )

    coverage = summarize_runtime_root_items(items)

    assert {item.work_type for item in items}.issubset(set(work_types))
    assert coverage.requested == size
    assert coverage.requested == coverage.admitted + coverage.deferred + coverage.not_admitted
    assert coverage.expected == coverage.admitted
    assert coverage.conserved is True


@pytest.mark.parametrize("sealed", ["completed", "failed", "killed", "skipped", "cancelled", "not_admitted"])
def test_every_root_terminal_state_rejects_late_completion(sealed: str) -> None:
    from app.services.runtime_root_ledger import evaluate_runtime_root_transition

    late = evaluate_runtime_root_transition(current_state=sealed, requested_state="completed")

    assert late.reason_code == "terminal_state_already_sealed"
    assert late.applied is False
    assert late.effective_state == sealed


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_pg_url")
async def test_real_pg_100_way_requested_set_conserves_and_is_tenant_isolated(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.tenant import Tenant
    from app.services.runtime_root_ledger import (
        read_runtime_root_coverage,
        register_runtime_root_item,
        transition_runtime_root_item,
    )

    tenant_id = uuid4()
    other_tenant_id = uuid4()
    root_id = uuid4()
    source_agent_id = uuid4()
    root_user_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add_all(
            [
                Tenant(id=tenant_id, name="Root Ledger Tenant", slug=f"root-ledger-{tenant_id.hex[:10]}"),
                Tenant(id=other_tenant_id, name="Other Tenant", slug=f"root-other-{other_tenant_id.hex[:10]}"),
            ]
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index in range(100):
            await register_runtime_root_item(
                db,
                tenant_id=tenant_id,
                root_runtime_task_id=root_id,
                source_agent_id=source_agent_id,
                root_user_id=root_user_id,
                root_session_id="root-session-100",
                intent_key=f"mixed:{index}",
                work_type=("direct", "team_member", "workflow")[index % 3],
                target_ref=f"target:{index}",
                path=(f"agent:{source_agent_id}",),
                state="requested",
                admission_disposition="requested",
                metadata={"ordinal": index},
            )
        await db.commit()

        for index in range(100):
            if index < 63:
                state = "queued"
            elif index < 85:
                state = "waiting_approval"
            else:
                state = "not_admitted"
            await transition_runtime_root_item(
                db,
                root_runtime_task_id=root_id,
                intent_key=f"mixed:{index}",
                requested_state=state,
            )
        coverage = await read_runtime_root_coverage(db, root_runtime_task_id=root_id)
        await db.commit()

    assert coverage.to_dict() == {
        "requested": 100,
        "admitted": 63,
        "deferred": 22,
        "not_admitted": 15,
        "expected": 63,
        "terminal": 0,
        "running": 63,
        "waiting_approval": 22,
        "conserved": True,
    }

    async with tenant_scoped_session(other_tenant_id, session_factory=app_user_sessionmaker) as db:
        leaked = (
            (await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.root_runtime_task_id == root_id)))
            .scalars()
            .all()
        )
    assert leaked == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_pg_url")
async def test_team_fanout_recovery_claim_is_leased_and_reclaimed_after_expiry(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.team_fanout_recovery import TeamFanoutRecoveryService

    tenant_id = uuid4()
    root_id = uuid4()
    source_agent_id = uuid4()
    root_user_id = uuid4()
    team_id = uuid4()
    member_id = uuid4()
    now = datetime.now(UTC)
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Recovery Tenant", slug=f"recovery-{tenant_id.hex[:10]}"))
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        item = await register_runtime_root_item(
            db,
            tenant_id=tenant_id,
            root_runtime_task_id=root_id,
            source_agent_id=source_agent_id,
            root_user_id=root_user_id,
            root_session_id=str(uuid4()),
            intent_key=f"team:{team_id}:member:{member_id}",
            work_type="team_member",
            target_ref=f"team-member:{member_id}",
            path=(f"agent:{source_agent_id}", f"team:{team_id}"),
            state="requested",
            admission_disposition="requested",
            metadata={
                "schema": "hive.runtime_root_team_intent.v1",
                "team_id": str(team_id),
                "member_id": str(member_id),
                "operation_id": "fanout-op",
                "message": "recover me",
                "source": "unit_test",
            },
        )
        item.recovery_claimed_by = "fanout-producer"
        item.recovery_claim_expires_at = now + timedelta(seconds=5)
        item_id = item.id
        await db.commit()

    service = TeamFanoutRecoveryService(session_factory=owner_sessionmaker, lease_seconds=5)
    producer_protected = await service.claim_batch(
        worker_id="premature-worker",
        now=now + timedelta(seconds=4),
        limit=1,
    )
    first = await service.claim_batch(
        worker_id="crashed-worker",
        now=now + timedelta(seconds=6),
        limit=1,
    )
    before_expiry = await service.claim_batch(
        worker_id="other-worker",
        now=now + timedelta(seconds=10),
        limit=1,
    )
    reclaimed = await service.claim_batch(
        worker_id="other-worker",
        now=now + timedelta(seconds=12),
        limit=1,
    )

    assert producer_protected == []
    assert [item.id for item in first] == [item_id]
    assert before_expiry == []
    assert [item.id for item in reclaimed] == [item_id]
    assert reclaimed[0].attempt_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_pg_url")
async def test_root_intent_cannot_be_rebound_to_a_different_runtime_task(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_root_ledger import (
        RuntimeRootIntentSpec,
        register_runtime_root_item,
        register_runtime_task_root_item,
    )

    tenant_id = uuid4()
    root_id = uuid4()
    parent_agent_id = uuid4()
    root_user_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Binding Tenant", slug=f"binding-{tenant_id.hex[:10]}"))
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await register_runtime_root_item(
            db,
            tenant_id=tenant_id,
            root_runtime_task_id=root_id,
            source_agent_id=parent_agent_id,
            root_user_id=root_user_id,
            root_session_id="root-session",
            intent_key="stable-intent",
            work_type="subagent",
            target_ref="subagent:critic",
            state="requested",
            admission_disposition="requested",
        )
        tasks = [
            RuntimeTask(
                id=uuid4(),
                tenant_id=tenant_id,
                task_type="subagent",
                status="pending",
                parent_agent_id=parent_agent_id,
                root_user_id=root_user_id,
                root_session_id="root-session",
                root_runtime_task_id=root_id,
            )
            for _ in range(2)
        ]
        db.add_all(tasks)
        await db.flush()
        intent = RuntimeRootIntentSpec(
            intent_key="stable-intent",
            work_type="subagent",
            target_ref="subagent:critic",
        )
        await register_runtime_task_root_item(db, task=tasks[0], intent=intent)

        with pytest.raises(ValueError, match="different RuntimeTask"):
            await register_runtime_task_root_item(db, task=tasks[1], intent=intent)
