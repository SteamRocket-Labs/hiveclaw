"""Discarded recall bodies still contribute typed availability output."""

import uuid
from contextlib import asynccontextmanager

from app.tools.runtime import ToolExecutionContext


async def test_formal_search_keeps_discarded_legacy_and_session_availability(monkeypatch, tmp_path):
    from app import database
    from app.memory import plane_read
    from app.services import team_memory_runtime
    from app.tools.handlers import memory

    @asynccontextmanager
    async def no_database(*_args, **_kwargs):
        raise AssertionError("This rendered-output unit test has no database dependency")
        yield  # pragma: no cover

    monkeypatch.setattr(database, "tenant_scoped_session", no_database)

    async def no_entries(*_args, **_kwargs):
        return []

    async def session_hit(*_args, **_kwargs):
        return [{"transcript": "LEGACY_SESSION_BODY"}]

    async def retained_output(_name, _agent, _arguments, _context, content, **_kwargs):
        return content

    monkeypatch.setattr(memory, "_scoped_explicit_entries", no_entries)
    monkeypatch.setattr(team_memory_runtime, "read_entries", no_entries)
    monkeypatch.setattr(plane_read, "search_plane_facts", lambda *_args, **_kwargs: [
        {"content": "LEGACY_FACT_BODY", "sensitivity": "PL1_public"}])
    monkeypatch.setattr(memory, "search_session_history", session_hit)
    monkeypatch.setattr(memory, "_record_memory_output", retained_output)
    context = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), tenant_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()), runtime_task_id=str(uuid.uuid4()), workspace=tmp_path)
    result = await memory.search_memory(context.agent_id, {"query": "sales", "scope": "all"},
        context.tenant_id, execution_context=context)
    assert "LEGACY_FACT_BODY" not in result and "LEGACY_SESSION_BODY" not in result
    assert '"code": "legacy_memory_sources_unverified", "count": 1' in result
    assert '"code": "session_recall_sources_unverified", "count": 1' in result
    monkeypatch.setattr(plane_read, "search_plane_facts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(memory, "search_session_history", no_entries)
    skipped = await memory.search_memory(context.agent_id, {"query": "sales", "scope": "all"},
        context.tenant_id, execution_context=context)
    assert skipped != result
    assert "legacy_memory_sources_unverified" not in skipped and "session_recall_sources_unverified" not in skipped
