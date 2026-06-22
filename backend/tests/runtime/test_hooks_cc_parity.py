from __future__ import annotations

import pytest

from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult


def test_cc_core_lifecycle_hook_events_are_exposed() -> None:
    values = {event.value for event in HookEvent}

    assert {
        "user_prompt_submit",
        "session_start",
        "session_end",
        "stop",
        "stop_failure",
        "subagent_start",
        "subagent_stop",
        "pre_compaction",
        "post_compaction",
    }.issubset(values)


def test_cc_lifecycle_hook_context_carries_standard_payload_fields() -> None:
    ctx = HookContext(
        event=HookEvent.SUBAGENT_STOP,
        session_id="parent-session",
        prompt="Inspect this",
        last_assistant_message="Done",
        stop_hook_active=True,
        agent_type="explorer",
        agent_transcript_path="/tmp/t0/source.md",
        metadata={"runtime_task_id": "rt-1"},
    )

    assert ctx.prompt == "Inspect this"
    assert ctx.last_assistant_message == "Done"
    assert ctx.stop_hook_active is True
    assert ctx.agent_type == "explorer"
    assert ctx.agent_transcript_path == "/tmp/t0/source.md"
    assert ctx.metadata["runtime_task_id"] == "rt-1"


@pytest.mark.asyncio
async def test_stop_hook_can_return_blocking_result() -> None:
    registry = HookRegistry()

    def stop_blocker(ctx: HookContext) -> HookResult:
        return HookResult(block=True, reason="final answer needs verification")

    registry.register(HookEvent.STOP, stop_blocker)

    result = await registry.emit(
        HookContext(
            event=HookEvent.STOP,
            session_id="s1",
            last_assistant_message="draft answer",
            stop_hook_active=False,
        )
    )

    assert result is not None
    assert result.block is True
    assert result.reason == "final answer needs verification"


@pytest.mark.asyncio
async def test_stop_handler_failure_emits_stop_failure() -> None:
    registry = HookRegistry()
    calls: list[tuple[str, str]] = []

    def broken(ctx: HookContext) -> None:
        raise RuntimeError("boom")

    def failure(ctx: HookContext) -> None:
        calls.append((ctx.event.value, ctx.error or ""))

    registry.register(HookEvent.STOP, broken)
    registry.register(HookEvent.STOP_FAILURE, failure)

    await registry.emit(HookContext(event=HookEvent.STOP, session_id="s1", last_assistant_message="draft"))

    assert calls == [("stop_failure", "RuntimeError: boom")]
