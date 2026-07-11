from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_approval_ticket_hash_is_canonical_and_binds_tool_name() -> None:
    from app.services.approval_ticket import hash_tool_input

    assert hash_tool_input("write_file", {"content": "x", "path": "a"}) == hash_tool_input(
        "write_file", {"path": "a", "content": "x"}
    )
    assert hash_tool_input("edit_file", {"path": "a", "content": "x"}) != hash_tool_input(
        "write_file", {"path": "a", "content": "x"}
    )


async def _seed_approved_ticket(owner_sessionmaker, *, expires_at: datetime | None = None):
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.approval_ticket import (
        build_approval_execution_envelope,
        build_live_approval_policy_snapshot,
        hash_approval_execution_envelope,
        hash_policy_snapshot,
        hash_tool_input,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    arguments = {"path": "workspace/notes.md", "content": "approved"}
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Approval Ticket", slug=f"approval-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"approval-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@approval.test",
                password_hash="x",
                display_name="Approval Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Approval Agent", creator_id=user_id))
        await db.flush()
        policy_snapshot = await build_live_approval_policy_snapshot(
            db=db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_name="write_file",
        )
        execution_envelope = build_approval_execution_envelope(
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/approval-ticket-workspace"),
                session_id=f"channel-session:{approval_id}",
                origin_channel="test",
            ),
            tool_call_id=f"tool-call:{approval_id}",
            emit_runtime_hooks=True,
        )
        db.add(
            ApprovalRequest(
                id=approval_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                action_type="workspace.write",
                status="approved",
                resolved_at=datetime.now(timezone.utc),
                resolved_by=user_id,
                tool_name="write_file",
                normalized_arguments=arguments,
                input_hash=hash_tool_input("write_file", arguments),
                policy_snapshot_hash=hash_policy_snapshot(policy_snapshot),
                policy_snapshot=policy_snapshot,
                execution_envelope=execution_envelope,
                execution_envelope_hash=hash_approval_execution_envelope(execution_envelope),
                requested_by=user_id,
                expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=30),
                execution_status="approved",
                execution_idempotency_key=f"approval:{approval_id}",
                decision_id=f"decision:{approval_id}",
                details={"tool": "write_file", "args": arguments},
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id, approval_id, arguments


@pytest.mark.asyncio
async def test_consume_approval_ticket_binds_principal_input_policy_expiry_and_replay(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.services.approval_ticket import ApprovalTicketError, consume_approval_ticket

    tenant_id, user_id, agent_id, approval_id, arguments = await _seed_approved_ticket(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    ticket = await consume_approval_ticket(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        expected_user_id=user_id,
    )
    assert ticket.tenant_id == tenant_id
    assert ticket.agent_id == agent_id
    assert ticket.approved_by_user_id == user_id
    assert ticket.tool_name == "write_file"
    assert ticket.arguments == arguments
    assert ticket.idempotency_key == f"approval:{approval_id}"
    assert ticket.decision_id == f"decision:{approval_id}"
    assert ticket.execution_envelope["tool_call_id"] == f"tool-call:{approval_id}"
    assert ticket.execution_envelope_hash

    with pytest.raises(ApprovalTicketError, match="already consumed"):
        await consume_approval_ticket(
            approval_id=approval_id,
            expected_agent_id=agent_id,
            expected_user_id=user_id,
        )

    with pytest.raises(ApprovalTicketError, match="agent mismatch"):
        await consume_approval_ticket(
            approval_id=approval_id,
            expected_agent_id=uuid.uuid4(),
            expected_user_id=user_id,
        )


@pytest.mark.asyncio
async def test_approval_ticket_keeps_requester_authority_distinct_from_approver(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.user import User
    from app.services.approval_ticket import consume_approval_ticket

    tenant_id, requester_id, agent_id, approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    approver_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            User(
                id=approver_id,
                username=f"approver-{approver_id.hex[:8]}",
                email=f"{approver_id.hex[:8]}@approval.test",
                password_hash="x",
                display_name="Approval Admin",
                tenant_id=tenant_id,
            )
        )
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        approval.resolved_by = approver_id
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    ticket = await consume_approval_ticket(
        approval_id=approval_id,
        expected_agent_id=agent_id,
        expected_user_id=approver_id,
    )

    assert ticket.requested_by_user_id == requester_id
    assert ticket.approved_by_user_id == approver_id


@pytest.mark.asyncio
async def test_consume_approval_ticket_rejects_expired_and_mutated_requests(owner_sessionmaker, monkeypatch) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.services.approval_ticket import ApprovalTicketError, consume_approval_ticket

    _, user_id, agent_id, expired_id, _ = await _seed_approved_ticket(
        owner_sessionmaker,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    with pytest.raises(ApprovalTicketError, match="expired"):
        await consume_approval_ticket(
            approval_id=expired_id,
            expected_agent_id=agent_id,
            expected_user_id=user_id,
        )

    _, user_id2, agent_id2, mutated_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        row = await db.get(ApprovalRequest, mutated_id)
        assert row is not None
        row.normalized_arguments = {"path": "workspace/other.md", "content": "mutated"}
        await db.commit()
    with pytest.raises(ApprovalTicketError, match="input hash mismatch"):
        await consume_approval_ticket(
            approval_id=mutated_id,
            expected_agent_id=agent_id2,
            expected_user_id=user_id2,
        )


@pytest.mark.asyncio
async def test_consume_approval_ticket_rejects_live_policy_drift(owner_sessionmaker, monkeypatch) -> None:
    import app.database as database
    from app.models.capability_policy import CapabilityPolicy
    from app.services.approval_ticket import ApprovalTicketError, consume_approval_ticket

    tenant_id, user_id, agent_id, approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(
            CapabilityPolicy(
                tenant_id=tenant_id,
                agent_id=agent_id,
                capability="workspace.file.write",
                allowed=True,
                requires_approval=False,
                conditions={},
            )
        )
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    with pytest.raises(ApprovalTicketError, match="policy changed after approval request"):
        await consume_approval_ticket(
            approval_id=approval_id,
            expected_agent_id=agent_id,
            expected_user_id=user_id,
        )


@pytest.mark.asyncio
async def test_consume_approval_ticket_rechecks_cancelled_task_and_exhausted_budget(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_ticket import (
        ApprovalTicketError,
        build_approval_execution_envelope,
        consume_approval_ticket,
        hash_approval_execution_envelope,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id, user_id, agent_id, cancelled_approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    cancelled_task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=cancelled_task_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_id,
                tenant_id=tenant_id,
                status="killed",
                root_idempotency_key=f"test-task:{cancelled_task_id}",
                config_snapshot_hash="config",
                policy_snapshot_hash="policy",
            )
        )
        row = await db.get(ApprovalRequest, cancelled_approval_id)
        assert row is not None
        envelope = build_approval_execution_envelope(
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=Path("/tmp/approval-ticket-workspace"),
                runtime_task_id=str(cancelled_task_id),
            ),
            tool_call_id=f"tool-call:{cancelled_approval_id}",
            emit_runtime_hooks=True,
        )
        row.execution_envelope = envelope
        row.execution_envelope_hash = hash_approval_execution_envelope(envelope)
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    with pytest.raises(ApprovalTicketError, match="runtime task was cancelled"):
        await consume_approval_ticket(
            approval_id=cancelled_approval_id,
            expected_agent_id=agent_id,
            expected_user_id=user_id,
        )

    tenant_id2, user_id2, agent_id2, budget_approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    budget_run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeBudgetRun(
                id=budget_run_id,
                tenant_id=tenant_id2,
                root_run_kind="approval-test",
                root_run_key=f"approval-test:{budget_run_id}",
                root_agent_id=agent_id2,
                root_user_id=user_id2,
                status="exhausted",
            )
        )
        row = await db.get(ApprovalRequest, budget_approval_id)
        assert row is not None
        envelope = build_approval_execution_envelope(
            context=ToolExecutionContext(
                agent_id=agent_id2,
                user_id=user_id2,
                tenant_id=str(tenant_id2),
                workspace=Path("/tmp/approval-ticket-workspace"),
                budget_run_id=str(budget_run_id),
            ),
            tool_call_id=f"tool-call:{budget_approval_id}",
            emit_runtime_hooks=True,
        )
        row.execution_envelope = envelope
        row.execution_envelope_hash = hash_approval_execution_envelope(envelope)
        await db.commit()

    with pytest.raises(ApprovalTicketError, match="budget is not active: exhausted"):
        await consume_approval_ticket(
            approval_id=budget_approval_id,
            expected_agent_id=agent_id2,
            expected_user_id=user_id2,
        )


@pytest.mark.asyncio
async def test_reconcile_stuck_approval_ticket_marks_uncertain_without_replay(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.audit import ApprovalRequest
    from app.services.approval_ticket import reconcile_stuck_approval_tickets

    tenant_id, _, _, approval_id, _ = await _seed_approved_ticket(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        row = await db.get(ApprovalRequest, approval_id)
        assert row is not None
        row.consumed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        row.execution_status = "executing"
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    count = await reconcile_stuck_approval_tickets(
        older_than=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert count == 1
    async with owner_sessionmaker() as db:
        row = await db.get(ApprovalRequest, approval_id)
        assert row is not None
        assert row.execution_status == "needs_reconciliation"
        assert row.execution_receipt["side_effect_state"] == "unknown"
        assert row.execution_receipt["automatic_replay"] is False
        assert row.tenant_id == tenant_id
