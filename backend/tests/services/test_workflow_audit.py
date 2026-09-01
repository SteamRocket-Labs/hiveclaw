"""§9 P9 red tests: run lifecycle + fork audit trail on real PG.

Atomic promotion audit is asserted with its immutable proposal transaction in
``test_workflow_promotion_service.py``.
"""

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


async def test_run_lifecycle_writes_audit_with_required_fields(tenant_id, owner_sessionmaker, monkeypatch):
    # The started event uses the fail-soft operational writer while terminal
    # audit is committed atomically with the RuntimeTask terminal transition.
    # Point the operational writer at the test container, then assert both
    # production persistence paths from the database.
    import app.services.audit_logger as audit_logger_module

    monkeypatch.setattr(audit_logger_module, "async_session", owner_sessionmaker)

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(tenant_id=tenant_id, definition_data=_definition(), args={}, leaf_executor=leaf)
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action.in_(("workflow_run_started", "workflow_run_completed")),
                        AuditLog.details["run_id"].as_string() == str(handle.run_id),
                    )
                )
            )
            .scalars()
            .all()
        )

    by_action = {row.action: row for row in rows}
    assert set(by_action) == {"workflow_run_started", "workflow_run_completed"}
    assert by_action["workflow_run_completed"].id == uuid.uuid5(
        handle.run_id,
        "workflow-run:completed:audit",
    )
    assert by_action["workflow_run_completed"].tenant_id == tenant_id
    for row in rows:
        assert row.details["tenant_id"] == str(tenant_id)
        assert row.details["run_id"] == str(handle.run_id)
        assert row.details["definition_hash"]


async def test_fork_audit_carries_provenance(tenant_id, owner_sessionmaker, workflow_principals, monkeypatch):
    import app.services.audit_logger as audit_logger_module

    captured: list[tuple[str, dict]] = []

    async def capturing_audit(action, details=None, agent_id=None, user_id=None):
        captured.append((action, details or {}))

    monkeypatch.setattr(audit_logger_module, "write_audit_log", capturing_audit)

    service = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    record = await service.create_draft(
        tenant_id=tenant_id, definition_data=_definition("fork-audit"), visibility_scope="tenant"
    )
    await service.activate(record.id, tenant_id=tenant_id, actor_user_id=workflow_principals.user_id)
    agent = workflow_principals.agent_id
    await service.fork_to_ephemeral(tenant_id=tenant_id, name="fork-audit", agent_id=agent)

    fork_rows = [details for action, details in captured if action == "workflow_definition_forked"]
    assert len(fork_rows) == 1
    assert fork_rows[0]["forked_by_agent_id"] == str(agent)
    assert fork_rows[0]["definition_hash"] == record.definition_hash
