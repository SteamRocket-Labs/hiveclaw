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

    async def fake_learning_brain(**kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["messages"] == [{"role": "user", "content": "错了，下次用 npm。"}]
        return {
            "method": "learning_brain_agent",
            "signal_type": "user_preference_correction",
            "lesson": "Use npm for this repository.",
            "confidence": 0.87,
            "learning_brain_decision": {
                "schema": "fast_reflection_learning_brain_decision.v1",
                "signal_type": "user_preference_correction",
                "lesson": "Use npm for this repository.",
                "confidence": 0.87,
                "container": "session_learning",
                "promotion_intent": "project_only",
                "rationale": "User corrected the project package manager.",
                "evidence_refs": ["message:0"],
                "boundary_checks": {"not_direct_memory_write": True},
            },
        }

    monkeypatch.setattr("app.runtime.hooks_setup.schedule_fast_reflection_candidate", fake_schedule)
    monkeypatch.setattr("app.runtime.hooks_setup._run_fast_reflection_learning_brain", fake_learning_brain)
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
    assert scheduled[0]["metadata"]["fast_reflection_classification"]["method"] == "learning_brain_agent"
    assert scheduled[0]["metadata"]["fast_reflection_classification"]["lesson"] == "Use npm for this repository."
    assert (
        scheduled[0]["metadata"]["fast_reflection_classification"]["learning_brain_decision"]["container"]
        == "session_learning"
    )


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
