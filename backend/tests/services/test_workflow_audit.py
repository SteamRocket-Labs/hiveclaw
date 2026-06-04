"""§9 P9 red tests: run lifecycle + promote/fork audit trail on real PG."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.audit import AuditLog
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_definitions import WorkflowDefinitionService
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition(name: str = "audited") -> dict:
    return {
        "name": name,
        "args_schema": {},
        "steps": [
            {"id": "only", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Do it"},
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-audit", slug=f"wa-{tid.hex[:10]}"))
    return tid


async def _audit_rows(owner_sessionmaker, action: str) -> list[AuditLog]:
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        return list((await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all())


async def test_run_lifecycle_writes_audit_with_required_fields(tenant_id, owner_sessionmaker, monkeypatch):
    # write_audit_log opens its own session against the app engine — point it
    # at the test container by redirecting its session factory.
    import app.services.audit_logger as audit_logger_module

    captured: list[tuple[str, dict]] = []

    async def capturing_audit(action, details=None, agent_id=None, user_id=None):
        captured.append((action, details or {}))

    monkeypatch.setattr(audit_logger_module, "write_audit_log", capturing_audit)

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(tenant_id=tenant_id, definition_data=_definition(), args={}, leaf_executor=leaf)
    assert handle.outcome.status == "completed"

    actions = [action for action, _ in captured]
    assert "workflow_run_started" in actions
    assert "workflow_run_completed" in actions
    for action, details in captured:
        assert details["tenant_id"] == str(tenant_id)
        assert details["run_id"] == str(handle.run_id)
        assert "definition_hash" in details


async def test_promotion_audit_carries_approval_metadata(tenant_id, owner_sessionmaker, monkeypatch):
    import app.services.audit_logger as audit_logger_module

    captured: list[tuple[str, dict]] = []

    async def capturing_audit(action, details=None, agent_id=None, user_id=None):
        captured.append((action, details or {}))

    monkeypatch.setattr(audit_logger_module, "write_audit_log", capturing_audit)

    service = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    approver = uuid.uuid4()
    proposal = await service.submit_promote_proposal(
        tenant_id=tenant_id, agent_id=uuid.uuid4(), definition_data=_definition("promoted-audit")
    )
    await service.approve_promotion(proposal.id, tenant_id=tenant_id, approver_user_id=approver)

    promote_rows = [details for action, details in captured if action == "workflow_definition_promoted"]
    assert len(promote_rows) == 1
    assert promote_rows[0]["approver_user_id"] == str(approver)
    assert promote_rows[0]["tenant_id"] == str(tenant_id)
    assert promote_rows[0]["definition_hash"] == proposal.definition_hash


async def test_fork_audit_carries_provenance(tenant_id, owner_sessionmaker, monkeypatch):
    import app.services.audit_logger as audit_logger_module

    captured: list[tuple[str, dict]] = []

    async def capturing_audit(action, details=None, agent_id=None, user_id=None):
        captured.append((action, details or {}))

    monkeypatch.setattr(audit_logger_module, "write_audit_log", capturing_audit)

    service = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    record = await service.create_draft(
        tenant_id=tenant_id, definition_data=_definition("fork-audit"), visibility_scope="tenant"
    )
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())
    agent = uuid.uuid4()
    await service.fork_to_ephemeral(tenant_id=tenant_id, name="fork-audit", agent_id=agent)

    fork_rows = [details for action, details in captured if action == "workflow_definition_forked"]
    assert len(fork_rows) == 1
    assert fork_rows[0]["forked_by_agent_id"] == str(agent)
    assert fork_rows[0]["definition_hash"] == record.definition_hash
