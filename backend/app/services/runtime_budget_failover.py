"""Typed failover contract for runtime-budget root admission.

The budget plane is an authoritative resource/lifecycle boundary.  When it is
unavailable, a human-facing turn may still use the model for a direct answer,
but recursive/background work must remain impossible both at capability
assembly and immediately before a tool effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from app.services.runtime_budget_service import BudgetFailureContext, decide_budget_service_failure
from app.tools.result_envelope import ToolContentEnvelope, render_tool_error


RUNTIME_BUDGET_BINDING_SCHEMA = "hive.runtime_budget_binding.v1"
_RECOVERY = "retry_next_independent_turn"


@dataclass(frozen=True, slots=True)
class RuntimeBudgetRootBinding:
    status: str
    budget_run_id: uuid.UUID | None
    payload: dict[str, Any]
    fail_open: bool = False
    fail_closed: bool = False


def bound_runtime_budget_root_binding(budget_run_id: uuid.UUID) -> RuntimeBudgetRootBinding:
    return RuntimeBudgetRootBinding(
        status="bound",
        budget_run_id=budget_run_id,
        payload={
            "schema": RUNTIME_BUDGET_BINDING_SCHEMA,
            "status": "bound",
            "retryable": False,
            "work_amplifying_tools_disabled": False,
        },
    )


def inherited_runtime_budget_root_binding(budget_run_id: uuid.UUID) -> RuntimeBudgetRootBinding:
    binding = bound_runtime_budget_root_binding(budget_run_id)
    return RuntimeBudgetRootBinding(
        status="inherited",
        budget_run_id=binding.budget_run_id,
        payload={**binding.payload, "status": "inherited"},
    )


def not_applicable_runtime_budget_root_binding() -> RuntimeBudgetRootBinding:
    """Test/scaffold-only marker for non-SQLAlchemy persistence doubles."""

    return RuntimeBudgetRootBinding(
        status="not_applicable",
        budget_run_id=None,
        payload={
            "schema": RUNTIME_BUDGET_BINDING_SCHEMA,
            "status": "not_applicable",
            "retryable": False,
            "work_amplifying_tools_disabled": False,
        },
    )


def unavailable_runtime_budget_root_binding(
    *,
    source: str,
    interactive: bool,
    error: Exception,
) -> RuntimeBudgetRootBinding:
    decision = decide_budget_service_failure(
        BudgetFailureContext(
            source=source,
            interactive=interactive,
            work_amplifying=not interactive,
        )
    )
    payload = {
        "schema": RUNTIME_BUDGET_BINDING_SCHEMA,
        "status": "unavailable",
        "reason": decision.reason,
        "retryable": True,
        "interactive": interactive,
        "work_amplifying_tools_disabled": decision.disable_work_amplifying_tools,
        "error_class": type(error).__name__,
        "recovery": _RECOVERY,
    }
    return RuntimeBudgetRootBinding(
        status="unavailable",
        budget_run_id=None,
        payload=payload,
        fail_open=decision.fail_open,
        fail_closed=decision.fail_closed,
    )


def legacy_unbound_runtime_budget_root_binding() -> RuntimeBudgetRootBinding:
    """Contain pre-contract queued work that has no authoritative budget root."""

    binding = unavailable_runtime_budget_root_binding(
        source="legacy_runtime",
        interactive=False,
        error=RuntimeError("legacy runtime task has no budget binding"),
    )
    return RuntimeBudgetRootBinding(
        status=binding.status,
        budget_run_id=None,
        payload={
            **binding.payload,
            "reason": "legacy_budget_unbound",
            "interactive": False,
        },
        fail_open=False,
        fail_closed=True,
    )


def normalize_runtime_budget_root_binding(
    value: RuntimeBudgetRootBinding | uuid.UUID | None,
    *,
    source: str,
    interactive: bool,
) -> RuntimeBudgetRootBinding:
    """Normalize legacy/mocked helper results without treating ``None`` as safe."""

    if isinstance(value, RuntimeBudgetRootBinding):
        return value
    if isinstance(value, uuid.UUID):
        return bound_runtime_budget_root_binding(value)
    return unavailable_runtime_budget_root_binding(
        source=source,
        interactive=interactive,
        error=RuntimeError("runtime budget root binding was not returned"),
    )


def apply_runtime_budget_root_binding(
    metadata: dict[str, Any],
    binding: RuntimeBudgetRootBinding,
) -> dict[str, Any]:
    result = dict(metadata)
    if binding.status == "not_applicable":
        return result
    result["runtime_budget"] = dict(binding.payload)
    result["budget_interactive"] = bool(binding.payload.get("interactive"))
    if binding.budget_run_id is not None:
        result["budget_run_id"] = str(binding.budget_run_id)
    if binding.payload.get("work_amplifying_tools_disabled") is True:
        from app.tools.registry import work_amplifying_tool_exclusion_names

        existing = [str(name) for name in (result.get("excluded_tool_names") or ()) if str(name).strip()]
        result["excluded_tool_names"] = list(
            dict.fromkeys([*existing, *work_amplifying_tool_exclusion_names()])
        )
        result["budget_observability_degraded"] = True
    return result


def runtime_budget_payload(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = (metadata or {}).get("runtime_budget")
    if not isinstance(payload, dict) or payload.get("schema") != RUNTIME_BUDGET_BINDING_SCHEMA:
        return None
    return dict(payload)


def runtime_budget_blocks_amplification(metadata: dict[str, Any] | None) -> bool:
    payload = runtime_budget_payload(metadata)
    return bool(
        payload
        and payload.get("status") == "unavailable"
        and payload.get("work_amplifying_tools_disabled") is True
    )


def runtime_budget_model_notice(metadata: dict[str, Any] | None) -> str:
    payload = runtime_budget_payload(metadata)
    if not payload or payload.get("status") != "unavailable":
        return ""
    return (
        "[Runtime control availability]\n"
        "status=budget_service_unavailable; retryable=true. "
        "You may reason and answer directly from the authorized evidence. "
        "Work-amplifying capabilities are not available in this turn, including subagents, workflows, "
        "cross-agent calls, starting or resuming goals, creating triggers, and creating continuation wakeups; "
        "exact cancellation/stop/hold actions remain available. Do not claim that any unavailable action ran. "
        "A new independent turn may retry budget admission after this turn finishes."
    )


def unavailable_work_amplifying_tool_result(
    tool_name: str,
    metadata: dict[str, Any] | None,
) -> ToolContentEnvelope:
    payload = runtime_budget_payload(metadata) or {}
    reason = str(payload.get("reason") or "runtime_budget_service_unavailable")
    typed = {
        "status": "unavailable",
        "code": "runtime_budget_service_unavailable",
        "reason": reason,
        "retryable": True,
        "effect_started": False,
        "tool_name": tool_name,
    }
    return ToolContentEnvelope(
        text=render_tool_error(
            tool_name=tool_name,
            error_class="runtime_budget_service_unavailable",
            message="Runtime protection is temporarily unavailable; this work-amplifying action did not start.",
            retryable=True,
            actionable_hint="Finish the direct response and retry the action in a new independent turn.",
            extra=typed,
        ),
        metadata=typed,
    )
