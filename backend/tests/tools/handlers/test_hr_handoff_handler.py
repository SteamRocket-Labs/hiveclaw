from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.tools.result_envelope import ToolContentEnvelope
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


@pytest.mark.asyncio
async def test_hr_handoff_keeps_full_ui_receipt_out_of_model_visible_result(monkeypatch) -> None:
    from app import database
    from app.services import hr_creation_handoff_service
    from app.tools.handlers.communication import start_hr_agent_handoff

    receipt = {
        "ok": True,
        "status": "hr_handoff_started",
        "hr_agent_id": "bef8b286-b923-4e29-84c9-022f995ae6b3",
        "hr_session_id": "2eb843de-6f8c-52cf-aeda-a17cd08f26da",
        "source_agent_name": "Source Agent",
        "message": "The creation request is ready in HR Agent.",
        "creation_brief_sha256": "724d3ce32853c2dd8ef7e4d396cb6513",
        "replayed": False,
    }

    @asynccontextmanager
    async def fake_tenant_scoped_session(*_args, **_kwargs):
        yield object()

    async def fake_start_handoff(_db, **_kwargs):
        return receipt

    monkeypatch.setattr(database, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(hr_creation_handoff_service, "start_hr_creation_handoff", fake_start_handoff)

    request = ToolExecutionRequest(
        tool_name="start_hr_agent_handoff",
        arguments={"creation_brief": "Create a test feedback employee."},
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=Path("/tmp/hr-handoff-handler-test"),
            session_id=str(uuid.uuid4()),
            runtime_task_id=str(uuid.uuid4()),
        ),
    )

    result = await start_hr_agent_handoff(request)

    assert isinstance(result, ToolContentEnvelope)
    assert json.loads(str(result)) == receipt
    assert result.model_visible_text is not None
    model_result = json.loads(result.model_visible_text)
    assert model_result == {
        "ok": True,
        "status": "hr_handoff_ready",
        "message": "The user-facing HR review action is available in the handoff card. Briefly direct the user to that action without restating internal receipt fields.",
    }
    for internal_value in (
        receipt["hr_agent_id"],
        receipt["hr_session_id"],
        receipt["creation_brief_sha256"],
        receipt["status"],
    ):
        assert internal_value not in result.model_visible_text
