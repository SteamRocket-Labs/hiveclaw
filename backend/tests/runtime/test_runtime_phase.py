"""RuntimePhase first-class contract tests (2026-07-09 unified design §3.3).

One clean phase signal shared by three consumers: Session expression renders
each phase, Goal finalization contributes `summarizing`, Hook governance
contributes `hook_evaluating`. These tests pin the enum surface, the wire
event shape, and the single-point deduplicating emitter semantics.
"""

from __future__ import annotations

import pytest

from app.runtime.runtime_phase import (
    TERMINAL_PHASES,
    RunPhaseEmitter,
    RuntimePhase,
    build_phase_event,
)


def test_runtime_phase_enum_matches_design_contract():
    """§3.3 phase list is a wire contract consumed by the frontend state machine."""
    assert {phase.value for phase in RuntimePhase} == {
        "queued",
        "resuming",
        "starting",
        "thinking",
        "responding",
        "tool_running",
        "hook_evaluating",
        "compacting",
        "awaiting_approval",
        "awaiting_budget",
        "summarizing",
        "continuation_gap",
        "done",
        "failed",
        "cancelled",
    }


def test_terminal_phases_are_exactly_done_failed_cancelled():
    assert TERMINAL_PHASES == frozenset(
        {RuntimePhase.DONE, RuntimePhase.FAILED, RuntimePhase.CANCELLED}
    )


def test_build_phase_event_shape():
    event = build_phase_event(
        RuntimePhase.TOOL_RUNNING,
        run_id="abc123",
        detail={"tool_name": "write_file"},
    )
    assert event == {
        "type": "phase",
        "phase": "tool_running",
        "run_id": "abc123",
        "detail": {"tool_name": "write_file"},
    }


def test_build_phase_event_accepts_string_phase_and_omits_empty_fields():
    event = build_phase_event("thinking")
    assert event == {"type": "phase", "phase": "thinking"}


def test_build_phase_event_rejects_unknown_phase():
    with pytest.raises(ValueError):
        build_phase_event("warp_speed")


class _Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_emitter_broadcasts_transitions_and_dedupes_same_phase():
    collector = _Collector()
    emitter = RunPhaseEmitter(collector, run_id="run-1")

    assert await emitter.transition(RuntimePhase.STARTING) is True
    assert await emitter.transition(RuntimePhase.THINKING) is True
    # Same phase again: deduplicated, nothing broadcast.
    assert await emitter.transition(RuntimePhase.THINKING) is False
    assert await emitter.transition(RuntimePhase.RESPONDING) is True

    assert [event["phase"] for event in collector.events] == [
        "starting",
        "thinking",
        "responding",
    ]
    assert all(event["type"] == "phase" for event in collector.events)
    assert all(event["run_id"] == "run-1" for event in collector.events)


@pytest.mark.asyncio
async def test_emitter_same_phase_with_new_detail_rebroadcasts():
    """tool_running(write_file) -> tool_running(read_file) is a visible transition."""
    collector = _Collector()
    emitter = RunPhaseEmitter(collector, run_id="run-1")

    await emitter.transition(RuntimePhase.TOOL_RUNNING, detail={"tool_name": "write_file"})
    assert (
        await emitter.transition(RuntimePhase.TOOL_RUNNING, detail={"tool_name": "write_file"})
        is False
    )
    assert (
        await emitter.transition(RuntimePhase.TOOL_RUNNING, detail={"tool_name": "read_file"})
        is True
    )
    assert [event["detail"]["tool_name"] for event in collector.events] == [
        "write_file",
        "read_file",
    ]


@pytest.mark.asyncio
async def test_emitter_seals_after_terminal_phase():
    collector = _Collector()
    emitter = RunPhaseEmitter(collector, run_id="run-1")

    await emitter.transition(RuntimePhase.RESPONDING)
    await emitter.transition(RuntimePhase.DONE)
    # Terminal phases seal the emitter: no further transitions escape.
    assert await emitter.transition(RuntimePhase.THINKING) is False
    assert await emitter.transition(RuntimePhase.FAILED) is False

    assert [event["phase"] for event in collector.events] == ["responding", "done"]
    assert emitter.current is RuntimePhase.DONE


@pytest.mark.asyncio
async def test_emitter_awaiting_approval_is_not_terminal():
    """awaiting_approval parks the session but a follow-up run may resume phases."""
    collector = _Collector()
    emitter = RunPhaseEmitter(collector, run_id="run-1")

    await emitter.transition(RuntimePhase.AWAITING_APPROVAL)
    assert await emitter.transition(RuntimePhase.THINKING) is True
    assert [event["phase"] for event in collector.events] == ["awaiting_approval", "thinking"]


@pytest.mark.asyncio
async def test_emitter_swallows_broadcast_failures():
    """Phase is a UI signal (L2); a broken broadcast must never break the run (L1)."""

    async def broken_broadcast(_event: dict) -> None:
        raise RuntimeError("socket exploded")

    emitter = RunPhaseEmitter(broken_broadcast, run_id="run-1")
    assert await emitter.transition(RuntimePhase.STARTING) is True
    # State still advances so dedup keeps working even when broadcast fails.
    assert emitter.current is RuntimePhase.STARTING
