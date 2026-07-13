"""Single runtime-budget admission contract for work-amplifying execution.

Domain services still own their execution semantics. This small boundary owns
only the mechanical reserve / wait-for-approval / settle protocol so Subagent,
Agent Team, Workflow, Delegation, and Trigger cannot invent incompatible
budget behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal
import uuid

from app.services.runtime_budget_service import (
    RuntimeBudgetApprovalRequired,
    RuntimeBudgetReservation,
    RuntimeBudgetReservationResult,
    RuntimeBudgetService,
    RuntimeBudgetSettlement,
)

ExecutionAdmissionStatus = Literal["not_required", "admitted", "waiting_budget_approval"]


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionDecision:
    status: ExecutionAdmissionStatus
    reservation: RuntimeBudgetReservation | None = None
    result: RuntimeBudgetReservationResult | None = None
    budget_run_id: uuid.UUID | None = None
    denied_dimensions: tuple[str, ...] = ()
    user_message: str | None = None

    @property
    def admitted(self) -> bool:
        return self.status in {"not_required", "admitted"}

    @property
    def waiting(self) -> bool:
        return self.status == "waiting_budget_approval"


class ExecutionAdmission:
    """Reserve and settle one work-amplifying execution unit exactly once."""

    def __init__(self, budget_service: RuntimeBudgetService | None = None) -> None:
        self._budget_service = budget_service

    async def admit(
        self,
        reservation: RuntimeBudgetReservation | None,
    ) -> ExecutionAdmissionDecision:
        if reservation is None:
            return ExecutionAdmissionDecision(status="not_required")
        service = self._budget_service or RuntimeBudgetService()
        try:
            result = await service.reserve(reservation)
        except RuntimeBudgetApprovalRequired as exc:
            return ExecutionAdmissionDecision(
                status="waiting_budget_approval",
                reservation=reservation,
                budget_run_id=exc.budget_run_id or reservation.budget_run_id,
                denied_dimensions=tuple(exc.dimensions),
                user_message="运行额度已达上限，已请求管理员批准；当前工作尚未执行。",
            )
        effective_key = str(getattr(result, "reservation_key", None) or reservation.reservation_key)
        effective_reservation = (
            reservation
            if effective_key == reservation.reservation_key
            else replace(reservation, reservation_key=effective_key)
        )
        return ExecutionAdmissionDecision(
            status="admitted",
            reservation=effective_reservation,
            result=result,
            budget_run_id=getattr(result, "budget_run_id", reservation.budget_run_id),
            denied_dimensions=tuple(getattr(result, "denied_dimensions", ()) or ()),
        )

    async def settle(
        self,
        decision: ExecutionAdmissionDecision,
        *,
        actual_tokens: int = 0,
        actual_cache_miss_tokens: int = 0,
        actual_subagents: int = 0,
        actual_team_sessions: int = 0,
        actual_delegations: int = 0,
        actual_background_tasks: int = 0,
        actual_continuation_wakes: int = 0,
        actual_provider_calls: int = 0,
        reason: str | None = None,
        runtime_task_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> None:
        if decision.status != "admitted" or decision.reservation is None:
            return
        reservation = decision.reservation
        service = self._budget_service or RuntimeBudgetService()
        await service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=reservation.budget_run_id,
                reservation_key=reservation.reservation_key,
                actual_tokens=actual_tokens,
                actual_cache_miss_tokens=actual_cache_miss_tokens,
                actual_subagents=actual_subagents,
                actual_team_sessions=actual_team_sessions,
                actual_delegations=actual_delegations,
                actual_background_tasks=actual_background_tasks,
                actual_continuation_wakes=actual_continuation_wakes,
                actual_provider_calls=actual_provider_calls,
                reason=reason,
                runtime_task_id=runtime_task_id or reservation.runtime_task_id,
                metadata=metadata if metadata is not None else reservation.metadata,
            )
        )
