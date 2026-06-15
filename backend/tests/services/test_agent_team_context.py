from __future__ import annotations

from uuid import uuid4

import pytest


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
