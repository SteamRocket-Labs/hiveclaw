"""The startup registry wires concrete handoff handlers onto the shared service.

This is the seam that turns Phase-1's empty handler registry into a working
``scheduled_trigger`` handoff: at startup the API's shared
:class:`PlanModeService` must have the ``scheduled_trigger`` target registered,
so a confirmed plan handed off through the REST API actually creates its enabled
trigger instead of resolving to ``skipped``.
"""

from __future__ import annotations

from app.services.deep_research.plan_mode import deep_research_handoff_handler
from app.services.plan_mode_delegation_handoff import delegation_handoff_handler
from app.services.plan_mode_detached_handoff import detached_runtime_task_handoff
from app.services.plan_mode_agent_team_handoff import agent_team_handoff
from app.services.plan_mode_handoff import scheduled_trigger_handoff_handler
from app.services.plan_mode_registry import register_plan_mode_handoffs
from app.services.plan_mode_service import PlanModeService
from app.services.plan_mode_session_handoff import continue_current_session_handoff


def test_register_plan_mode_handoffs_registers_scheduled_trigger():
    service = PlanModeService()
    register_plan_mode_handoffs(service)

    # The scheduled_trigger target now resolves to the concrete handler.
    assert service._handoff_handlers["scheduled_trigger"] is scheduled_trigger_handoff_handler
    assert service._handoff_handlers["deep_research"] is deep_research_handoff_handler
    assert service._handoff_handlers["delegation"] is delegation_handoff_handler


def test_register_plan_mode_handoffs_registers_continuation_and_detached_targets():
    # CC-align §4.2/§4.3: every target must resolve to a handler — no more silent
    # ``no_handler_registered`` -> ``skipped``. The legacy ``long_task`` target is
    # routed to the same continuation handler (compat).
    service = PlanModeService()
    register_plan_mode_handoffs(service)

    assert service._handoff_handlers["continue_current_session"] is continue_current_session_handoff
    assert service._handoff_handlers["long_task"] is continue_current_session_handoff
    assert service._handoff_handlers["detached_runtime_task"] is detached_runtime_task_handoff
    assert service._handoff_handlers["agent_team"] is agent_team_handoff


def test_every_intent_handoff_target_has_registered_handler():
    """G/H.3 totality guard: every seedable intent's handoff target must resolve
    to a registered handler — no dead ``tool_action`` (or any other) target that
    silently degrades to ``no_handler_registered`` -> ``skipped``.

    Iterates the canonical ``_INTENT_HANDOFF_TARGET`` map (the single source the
    skeleton seeds from) against the wired registry so a future intent that maps
    to an unregistered target fails here instead of in production.
    """
    from app.services.plan_mode_core import _INTENT_HANDOFF_TARGET

    service = PlanModeService()
    register_plan_mode_handoffs(service)
    registered = set(service._handoff_handlers)

    for intent, target in _INTENT_HANDOFF_TARGET.items():
        assert target in registered, f"intent {intent!r} -> handoff target {target!r} has no registered handler"


def test_register_plan_mode_handoffs_is_idempotent():
    service = PlanModeService()
    register_plan_mode_handoffs(service)
    register_plan_mode_handoffs(service)
    assert service._handoff_handlers["scheduled_trigger"] is scheduled_trigger_handoff_handler
    assert service._handoff_handlers["deep_research"] is deep_research_handoff_handler
    assert service._handoff_handlers["delegation"] is delegation_handoff_handler


def test_api_shared_service_gets_handler_registered():
    """Registering against the API's shared service makes the REST handoff
    endpoint use the real handler (not the Phase-1 no-op)."""
    import app.api.plans as plans_api

    register_plan_mode_handoffs(plans_api.get_plan_mode_service())
    assert "scheduled_trigger" in plans_api.get_plan_mode_service()._handoff_handlers
    assert "deep_research" in plans_api.get_plan_mode_service()._handoff_handlers
    assert "delegation" in plans_api.get_plan_mode_service()._handoff_handlers
