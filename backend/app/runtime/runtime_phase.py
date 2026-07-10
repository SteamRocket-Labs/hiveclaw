"""RuntimePhase — first-class runtime phase contract (2026-07-09 unified design).

One clean backend->frontend phase signal shared by three consumers:

- Session expression renders each phase (design §3.3);
- Goal finalization contributes the ``summarizing`` lane (§2);
- Hook governance contributes ``hook_evaluating`` (§1).

Phase events are live UI signals: they are broadcast over the web chat event
stream but never persisted as transcript events — replay derives phases from
the durable event stream itself (frontend state machine in chatRuntime.ts).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger


class RuntimePhase(str, Enum):
    """Turn/run lifecycle phases (§3.3). Values are the wire contract."""

    QUEUED = "queued"
    RESUMING = "resuming"
    STARTING = "starting"
    THINKING = "thinking"
    RESPONDING = "responding"
    TOOL_RUNNING = "tool_running"
    HOOK_EVALUATING = "hook_evaluating"
    COMPACTING = "compacting"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_BUDGET = "awaiting_budget"
    SUMMARIZING = "summarizing"
    CONTINUATION_GAP = "continuation_gap"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PHASES: frozenset[RuntimePhase] = frozenset({RuntimePhase.DONE, RuntimePhase.FAILED, RuntimePhase.CANCELLED})

PHASE_EVENT_TYPE = "phase"


def build_phase_event(
    phase: RuntimePhase | str,
    *,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the wire event for a phase transition. Raises on unknown phases."""
    resolved = RuntimePhase(phase)
    event: dict[str, Any] = {"type": PHASE_EVENT_TYPE, "phase": resolved.value}
    if run_id:
        event["run_id"] = str(run_id)
    if detail:
        event["detail"] = dict(detail)
    return event


class RunPhaseEmitter:
    """Single-point, deduplicating phase broadcaster for one run.

    - The same (phase, detail) pair is emitted at most once in a row.
    - The same phase with a *new* detail re-broadcasts (tool A -> tool B).
    - Terminal phases seal the emitter; later transitions are ignored.
    - Broadcast failures are swallowed: phase is an L2 UI signal and must
      never break the L1 run it describes.
    """

    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        run_id: str | None = None,
    ) -> None:
        self._broadcast = broadcast
        self._run_id = str(run_id) if run_id else None
        self._current: RuntimePhase | None = None
        self._current_detail: dict[str, Any] | None = None

    @property
    def current(self) -> RuntimePhase | None:
        return self._current

    async def transition(
        self,
        phase: RuntimePhase | str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Advance to ``phase``; returns True when a phase event was emitted."""
        resolved = RuntimePhase(phase)
        if self._current in TERMINAL_PHASES:
            return False
        normalized_detail = dict(detail) if detail else None
        if self._current == resolved and self._current_detail == normalized_detail:
            return False
        self._current = resolved
        self._current_detail = normalized_detail
        try:
            await self._broadcast(build_phase_event(resolved, run_id=self._run_id, detail=normalized_detail))
        except Exception as exc:  # noqa: BLE001 - UI signal must not break the run
            logger.warning(
                "[RuntimePhase] broadcast failed for run {} phase {}: {}",
                self._run_id,
                resolved.value,
                exc,
            )
        return True
