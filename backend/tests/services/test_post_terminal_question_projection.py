"""A late human review needs an exact delivered boundary before guest projection."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services.runtime_control_bus import _is_settled_question_followup


@pytest.mark.parametrize("case", ["valid", "agent_authored", "running", "foreign", "pending", "missing_frontier", "before_terminal"])
async def test_late_question_projection_requires_a_delivered_scope_bound_frontier(case):
    tenant, agent, session, run, boundary_id, terminal = (uuid.uuid4() for _ in range(6))
    event = SimpleNamespace(item_kind="user_question", actor_type="user", run_id=run,
        tenant_id=tenant, agent_id=agent, session_id=session, sequence=23)
    task = SimpleNamespace(id=run, tenant_id=tenant, parent_agent_id=agent, child_agent_id=agent,
        parent_session_id=str(session), child_session_id=str(session), status="completed", completed_at=datetime.now(timezone.utc))
    binding = {"terminal_sequence": 22, "terminal_event_id": str(terminal)}
    boundary = SimpleNamespace(id=boundary_id, binding_json=binding, delivery_receipt_json={**binding,
        "boundary_id": str(boundary_id), "t0_boundary_id": str(boundary_id)})
    if case == "agent_authored":
        event.actor_type = "agent"
    if case == "running":
        task.status = "running"
    if case == "foreign":
        task.tenant_id = uuid.uuid4()
    if case == "pending":
        boundary = None
    if case == "missing_frontier":
        boundary.delivery_receipt_json.pop("terminal_event_id")
        boundary.binding_json.pop("terminal_event_id")
    if case == "before_terminal":
        event.sequence = 21
    db = SimpleNamespace(get=AsyncMock(return_value=task), scalar=AsyncMock(return_value=boundary))
    assert await _is_settled_question_followup(db, transcript_event=event) is (case == "valid")
