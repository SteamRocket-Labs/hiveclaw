"""Wire concrete Plan Mode handoff handlers onto a :class:`PlanModeService`.

Phase 1 ships :class:`PlanModeService` with an *empty* handoff registry so an
un-wired handoff resolves to ``skipped`` rather than silently succeeding. This
module is the startup seam (Phase 4) that registers the concrete handlers — for
now the single ``objective_trigger`` target (§13). It is called once during
app startup against the shared service the REST API uses, so a confirmed plan
handed off over HTTP actually creates the objective + enabled trigger.

Kept as its own module (rather than inline in ``main.py``) so the wiring is
unit-testable and ``main.py`` only gains a single call.
"""

from __future__ import annotations

from app.services.deep_research.plan_mode import register_deep_research_handoff
from app.services.plan_mode_handoff import register_objective_trigger_handoff
from app.services.plan_mode_service import PlanModeService


def register_plan_mode_handoffs(service: PlanModeService) -> None:
    """Register every concrete handoff handler onto ``service`` (idempotent)."""
    register_objective_trigger_handoff(service)
    register_deep_research_handoff(service)
