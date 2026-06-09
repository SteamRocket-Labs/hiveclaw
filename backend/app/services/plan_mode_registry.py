"""Wire concrete Plan Mode handoff handlers onto a :class:`PlanModeService`.

Phase 1 ships :class:`PlanModeService` with an *empty* handoff registry so an
un-wired handoff resolves to ``skipped`` rather than silently succeeding. This
module is the startup seam that registers the concrete handlers. It is called
once during app startup against the shared service the REST API uses, so a
confirmed plan handed off over HTTP actually creates its enabled trigger.

Kept as its own module (rather than inline in ``main.py``) so the wiring is
unit-testable and ``main.py`` only gains a single call.
"""

from __future__ import annotations

from app.services.deep_research.plan_mode import register_deep_research_handoff
from app.services.plan_mode_delegation_handoff import register_delegation_handoff
from app.services.plan_mode_detached_handoff import register_detached_runtime_task_handoff
from app.services.plan_mode_handoff import register_scheduled_trigger_handoff
from app.services.plan_mode_service import PlanModeService
from app.services.plan_mode_session_handoff import register_continue_current_session_handoff


def register_plan_mode_handoffs(service: PlanModeService) -> None:
    """Register every concrete handoff handler onto ``service`` (idempotent)."""
    register_scheduled_trigger_handoff(service)
    register_deep_research_handoff(service)
    register_delegation_handoff(service)
    # CC-align §4.2/§4.3: live-chat continuation (also handling the legacy
    # ``long_task`` target) + an explicit detached-background fail-closed stub, so
    # every target now resolves to a handler instead of silently ``skipped``.
    register_continue_current_session_handoff(service)
    register_detached_runtime_task_handoff(service)
