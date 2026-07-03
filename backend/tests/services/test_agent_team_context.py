from __future__ import annotations

from types import SimpleNamespace
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


class _TeamRowsSession:
    def __init__(self, *, team, member):
        self._team = team
        self._member = member
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self.execute_calls += 1
        if self.execute_calls in {1, 2}:
            return _ScalarsResult([])
        if self.execute_calls == 3:
            return _ScalarsResult([self._team])
        return _ScalarsResult([self._member])


class _ChildTeamRowsSession:
    def __init__(self, *, team, member):
        self._team = team
        self._member = member
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self.execute_calls += 1
        if self.execute_calls in {1, 2, 3}:
            return _ScalarsResult([])
        if self.execute_calls == 4:
            return _ScalarsResult([self._member])
        if self.execute_calls == 5:
            return _ScalarsResult([self._team])
        return _ScalarsResult([self._member])


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
    assert "Session/T0" in block
    assert "truth source" not in block


def test_render_team_context_block_surfaces_shared_task_list() -> None:
    from app.services.agent_team_context import render_team_context_block

    block = render_team_context_block(
        teams=[
            {
                "id": uuid4(),
                "name": "Review Team",
                "status": "active",
                "members": [{"member_name": "critic", "status": "idle", "chat_session_id": str(uuid4())}],
            }
        ],
        shared_tasks=[
            {
                "id": "todo-1",
                "title": "Review prompt and hook parity",
                "status": "pending",
                "owner": "critic",
                "description": "Compare against CC baseline.",
            }
        ],
    )

    assert "## Team Shared Task List" in block
    assert "Review prompt and hook parity" in block
    assert "owner=critic" in block
    assert "pending" in block


@pytest.mark.asyncio
async def test_prompt_facing_team_context_reads_agent_team_rows(monkeypatch) -> None:
    from app.services import agent_team_context
    from app.services.agent_team_context import build_prompt_facing_team_context

    agent_id = uuid4()
    team_id = uuid4()
    session_id = uuid4()
    member_session_id = uuid4()
    team = SimpleNamespace(id=team_id, name="Review Team", status="active", parent_session_id=session_id)
    member = SimpleNamespace(
        id=uuid4(),
        team_id=team_id,
        member_name="critic",
        member_role="Review prompt and hook gaps",
        chat_session_id=member_session_id,
        status="idle",
        runtime_task_id=None,
        runtime_task_type="team_member",
    )
    shared_session = _TeamRowsSession(team=team, member=member)
    monkeypatch.setattr(agent_team_context, "tenant_scoped_session", lambda _tenant_id: shared_session)

    rendered = await build_prompt_facing_team_context(
        agent_id=agent_id,
        tenant_id=uuid4(),
        session_id=session_id,
    )

    assert "## Agent Team Workspace" in rendered
    assert "Review Team" in rendered
    assert "critic" in rendered
    assert "Review prompt and hook gaps" in rendered
    assert str(member_session_id) in rendered


@pytest.mark.asyncio
async def test_prompt_facing_team_context_resolves_team_from_member_session_and_parent_ledger(monkeypatch) -> None:
    from app.services import agent_team_context
    from app.services.agent_team_context import build_prompt_facing_team_context

    agent_id = uuid4()
    team_id = uuid4()
    parent_session_id = uuid4()
    member_session_id = uuid4()
    team = SimpleNamespace(id=team_id, name="Review Team", status="active", parent_session_id=parent_session_id)
    member = SimpleNamespace(
        id=uuid4(),
        team_id=team_id,
        member_name="critic",
        member_role="Review prompt and hook gaps",
        chat_session_id=member_session_id,
        status="idle",
        runtime_task_id=None,
        runtime_task_type="team_member",
    )
    shared_session = _ChildTeamRowsSession(team=team, member=member)
    ledger_calls = []

    def fake_read_agent_work_ledger_view(**kwargs):
        ledger_calls.append(kwargs)
        return {
            "todo_items": [
                {
                    "id": "todo-1",
                    "title": "Review prompt and hook parity",
                    "status": "pending",
                    "owner": "critic",
                }
            ]
        }

    monkeypatch.setattr(agent_team_context, "tenant_scoped_session", lambda _tenant_id: shared_session)
    monkeypatch.setattr(agent_team_context, "read_agent_work_ledger_view", fake_read_agent_work_ledger_view)

    rendered = await build_prompt_facing_team_context(
        agent_id=agent_id,
        tenant_id=uuid4(),
        session_id=member_session_id,
    )

    assert "## Agent Team Workspace" in rendered
    assert "Review Team" in rendered
    assert "critic" in rendered
    assert "## Team Shared Task List" in rendered
    assert "Review prompt and hook parity" in rendered
    assert "owner=critic" in rendered
    assert ledger_calls[0]["agent_id"] == agent_id
    assert ledger_calls[0]["session_id"] == str(parent_session_id)


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
