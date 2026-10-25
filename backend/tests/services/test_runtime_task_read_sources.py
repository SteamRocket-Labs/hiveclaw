"""A published result cannot attest a different body in an earlier task snapshot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_summary", ["Published report.", "Unreceipted earlier report."])
async def test_task_snapshot_body_requires_its_exact_published_result(monkeypatch, observed_summary):
    from app.agents import orchestrator
    from app.services import delegation_input_origin, runtime_result_sources
    from app.services.runtime_task_read_sources import _task_sources

    task = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), task_type="delegation", status="completed",
        parent_agent_id=uuid4(), child_agent_id=uuid4(), parent_session_id=str(uuid4()), child_session_id=str(uuid4()),
        root_user_id=uuid4(), trace_id="native-snapshot", metadata_json={}, prompt="Produce the authorized report.",
        result_summary=observed_summary)
    result = SimpleNamespace(id=uuid4(), sha256="a" * 64)
    monkeypatch.setattr(orchestrator, "_delegation_projection_request_from_record",
        lambda _record: SimpleNamespace(session_id=task.child_session_id))
    monkeypatch.setattr(delegation_input_origin, "load_delegation_instruction_origin",
        AsyncMock(return_value=SimpleNamespace(sources=[], unbound=())))
    monkeypatch.setattr(runtime_result_sources, "load_canonical_result_sources",
        AsyncMock(return_value=(SimpleNamespace(result_summary="Published report."), [], set())))
    monkeypatch.setattr(runtime_result_sources, "runtime_result_publication_bound", AsyncMock(return_value=True))

    sources = await _task_sources(SimpleNamespace(scalar=AsyncMock(return_value=result)), task, task.parent_session_id)
    if observed_summary == "Published report.":
        assert sources and sources[1]
        assert sources[0] == [{"source_kind": "runtime_result",
            "source_id": f"{task.parent_agent_id}\n{task.parent_session_id}\n{result.id}", "source_version": result.sha256}]
    else:
        assert sources is None
