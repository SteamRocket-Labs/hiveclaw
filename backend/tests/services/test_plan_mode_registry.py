"""The startup registry wires concrete handoff handlers onto the shared service.

This is the seam that turns Phase-1's empty handler registry into a working
``objective_trigger`` handoff: at startup the API's shared
:class:`PlanModeService` must have the ``objective_trigger`` target registered,
so a confirmed plan handed off through the REST API actually creates the
objective + trigger instead of resolving to ``skipped``.
"""

from __future__ import annotations

from app.services.deep_research.plan_mode import deep_research_handoff_handler
from app.services.plan_mode_handoff import objective_trigger_handoff_handler
from app.services.plan_mode_registry import register_plan_mode_handoffs
from app.services.plan_mode_service import PlanModeService


def test_register_plan_mode_handoffs_registers_objective_trigger():
    service = PlanModeService()
    register_plan_mode_handoffs(service)

    # The objective_trigger target now resolves to the concrete handler.
    assert service._handoff_handlers["objective_trigger"] is objective_trigger_handoff_handler
    assert service._handoff_handlers["deep_research"] is deep_research_handoff_handler


def test_register_plan_mode_handoffs_is_idempotent():
    service = PlanModeService()
    register_plan_mode_handoffs(service)
    register_plan_mode_handoffs(service)
    assert service._handoff_handlers["objective_trigger"] is objective_trigger_handoff_handler
    assert service._handoff_handlers["deep_research"] is deep_research_handoff_handler


def test_api_shared_service_gets_handler_registered():
    """Registering against the API's shared service makes the REST handoff
    endpoint use the real handler (not the Phase-1 no-op)."""
    import app.api.plans as plans_api

    register_plan_mode_handoffs(plans_api.get_plan_mode_service())
    assert "objective_trigger" in plans_api.get_plan_mode_service()._handoff_handlers
    assert "deep_research" in plans_api.get_plan_mode_service()._handoff_handlers
