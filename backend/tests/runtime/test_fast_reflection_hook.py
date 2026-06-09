from __future__ import annotations

import uuid
from pathlib import Path


async def test_response_complete_fast_reflection_hook_schedules_non_blocking(monkeypatch, tmp_path: Path) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response

    scheduled: list[dict[str, object]] = []

    def fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"status": "scheduled"}

    monkeypatch.setattr("app.runtime.hooks_setup.schedule_fast_reflection_candidate", fake_schedule)
    monkeypatch.setattr("app.runtime.hooks_setup._agent_data_root", lambda: tmp_path)

    agent_id = uuid.uuid4()
    await _fast_reflection_on_response(
        HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=str(agent_id),
            session_id="session-1",
            source="web",
            messages=[{"role": "user", "content": "错了，下次用 npm。"}],
            metadata={"tenant_id": str(uuid.uuid4())},
        )
    )

    assert len(scheduled) == 1
    assert scheduled[0]["data_root"] == tmp_path
    assert scheduled[0]["agent_id"] == agent_id
    assert scheduled[0]["session_id"] == "session-1"


def test_memory_hook_plan_registers_fast_reflection_handler() -> None:
    from app.runtime.hooks import HookEvent
    from app.runtime.hooks_setup import _MEMORY_HOOK_REGISTRATIONS, export_memory_hook_plan

    plan = export_memory_hook_plan()

    assert len(_MEMORY_HOOK_REGISTRATIONS) == 13
    assert any(
        item["event"] == HookEvent.RESPONSE_COMPLETE.value
        and item["key"] == "memory.response_complete.fast_reflection"
        and item["handler_name"] == "fast_reflection_on_response"
        for item in plan
    )
