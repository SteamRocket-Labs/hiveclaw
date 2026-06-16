from __future__ import annotations

from uuid import uuid4

import pytest


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SharedCoordinationSession:
    def __init__(self):
        self.added = []
        self.execute_calls = 0
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):  # pragma: no cover
        pass

    async def execute(self, _stmt):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ScalarsResult([])
        return _ScalarsResult(self.added)


def test_render_team_context_block_surfaces_runtime_tasks_and_mailbox() -> None:
    from app.services.agent_team_context import render_team_context_block

    run_id = uuid4()
    block = render_team_context_block(
        tasks=[
            {
                "id": run_id,
                "task_type": "subagent",
                "status": "running",
                "child_agent_name": "researcher",
                "result_summary": "",
            }
        ],
        signals=[
            {
                "signal_type": "subagent_completed",
                "from_agent_id": "researcher",
                "content": "researcher completed market scan",
                "thread_id": str(run_id),
            }
        ],
    )

    assert "## Team Context" in block
    assert "subagent" in block
    assert "researcher" in block
    assert "running" in block
    assert "## Teammate Mailbox" in block
    assert "subagent_completed" in block
    assert "researcher completed market scan" in block
    assert "poll" not in block.lower()


@pytest.mark.asyncio
async def test_agent_runtime_context_includes_team_context(monkeypatch) -> None:
    from app.services import agent_context

    async def fake_team_context(**kwargs):
        assert kwargs["agent_id"]
        assert kwargs["tenant_id"] == "tenant-1"
        assert kwargs["session_id"] == "session-1"
        return "## Team Context\n- teammate state"

    async def fake_runtime_sections(*_args, **_kwargs):
        return ["\n## Current Time\n2026-06-15 00:00:00 (UTC)"]

    monkeypatch.setattr(agent_context, "_build_runtime_metadata_sections", fake_runtime_sections)
    monkeypatch.setattr(agent_context, "build_prompt_facing_team_context", fake_team_context)

    rendered = await agent_context.build_agent_runtime_context(
        uuid4(),
        current_user_name="Rocky",
        tenant_id="tenant-1",
        session_id="session-1",
    )

    assert "## Team Context" in rendered
    assert "teammate state" in rendered


@pytest.mark.asyncio
async def test_default_gateway_scope_writes_mailbox_signal_read_by_team_context(monkeypatch) -> None:
    from app.agents import coordination_wiring
    from app.agents.coordination_wiring import gateway_scope
    from app.config import Settings
    from app.services import agent_team_context
    from app.services.agent_team_context import build_prompt_facing_team_context

    assert Settings().COORDINATION_BACKEND == "postgres"

    shared_session = _SharedCoordinationSession()
    monkeypatch.setattr(coordination_wiring, "tenant_scoped_session", lambda _tenant_id: shared_session)
    monkeypatch.setattr(agent_team_context, "tenant_scoped_session", lambda _tenant_id: shared_session)

    tenant_id = uuid4()
    sender_id = uuid4()
    receiver_id = uuid4()

    async with gateway_scope(tenant_id=tenant_id) as gateway:
        await gateway.send_signal(
            from_agent_id=str(sender_id),
            to_agent_id=str(receiver_id),
            content="researcher completed market scan",
            signal_type="subagent_completed",
            thread_id="thread-1",
        )

    rendered = await build_prompt_facing_team_context(
        agent_id=receiver_id,
        tenant_id=tenant_id,
        session_id="thread-1",
    )

    assert "## Teammate Mailbox" in rendered
    assert "subagent_completed" in rendered
    assert "researcher completed market scan" in rendered
    assert shared_session.commits == 1
