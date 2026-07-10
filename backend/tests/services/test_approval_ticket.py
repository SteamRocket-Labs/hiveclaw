from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        build_live_approval_policy_snapshot,
        hash_policy_snapshot,
        hash_tool_input,
    )

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
