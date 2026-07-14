from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_pre_invocation_finalization_preserves_full_response_summary():
    from app.services import web_chat_run_orchestrator as orchestrator

    captured: dict = {}

    async def finalize_with_assistant(**kwargs):
        captured.update(kwargs)
        return True

    async def emit_terminal_hook(**_kwargs):
        return None

    async def broadcast(*_args):
        return None

    state = SimpleNamespace(
        run_uuid=uuid4(),
        agent=SimpleNamespace(id=uuid4()),
        actor_user_id=uuid4(),
        session_id=str(uuid4()),
        metadata={},
        runtime_session_context=SimpleNamespace(source="web"),
        ports=SimpleNamespace(
            terminal=SimpleNamespace(
                finalize_with_assistant=finalize_with_assistant,
                emit_terminal_hook=emit_terminal_hook,
            ),
            events=SimpleNamespace(
                broadcast=broadcast,
                build_done=lambda response: {"type": "done", "response": response},
            ),
        ),
    )
    full_response = "plan response\n" + ("P" * 1000) + "\nEND_OF_PLAN_RESPONSE"

    await orchestrator._finalize_pre_invocation_response(
        state,
        full_response,
        status="completed",
        reason="invoke_complete",
    )

    assert captured["content"] == full_response
    assert captured["result_summary"] == full_response
