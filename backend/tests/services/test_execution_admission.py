from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_execution_admission_projects_approved_reservation_and_exact_settlement():
    from app.services.execution_admission import ExecutionAdmission
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetReservationResult

    captured: dict = {}

    class BudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=False,
                idempotent=False,
                budget_run_id=reservation.budget_run_id,
            )

        async def settle(self, settlement):
            captured["settlement"] = settlement

    reservation = RuntimeBudgetReservation(
        budget_run_id=uuid.uuid4(),
        reservation_key="subagent:one",
        subagents=1,
        background_tasks=1,
        reason="subagent_start",
    )
    admission = ExecutionAdmission(BudgetService())

    decision = await admission.admit(reservation)
    await admission.settle(
        decision,
        actual_subagents=1,
        actual_background_tasks=1,
        reason="subagent_completed",
    )

    assert decision.status == "admitted"
    assert decision.reservation is reservation
    assert captured["settlement"].reservation_key == "subagent:one"
    assert captured["settlement"].actual_subagents == 1
    assert captured["settlement"].actual_background_tasks == 1


@pytest.mark.asyncio
async def test_execution_admission_returns_waiting_without_hiding_approval_identity():
    from app.services.execution_admission import ExecutionAdmission
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired, RuntimeBudgetReservation

    budget_run_id = uuid.uuid4()

    class BudgetService:
        async def reserve(self, reservation):
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["team_sessions"],
            )

    reservation = RuntimeBudgetReservation(
        budget_run_id=budget_run_id,
        reservation_key="team:one",
        team_sessions=1,
    )
    decision = await ExecutionAdmission(BudgetService()).admit(reservation)

    assert decision.status == "waiting_budget_approval"
    assert decision.budget_run_id == budget_run_id
    assert decision.denied_dimensions == ("team_sessions",)
    assert decision.user_message == "运行额度已达上限，已请求管理员批准；当前工作尚未执行。"


@pytest.mark.asyncio
async def test_execution_admission_without_budget_is_explicitly_unmetered():
    from app.services.execution_admission import ExecutionAdmission

    decision = await ExecutionAdmission().admit(None)

    assert decision.status == "not_required"
    assert decision.reservation is None
