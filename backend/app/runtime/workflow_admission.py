"""Workflow admission (§9 P2): budget / fanout / wall-clock / args preflight.

Admission runs with run context (args, tenant leaf catalog) right before a
run is created; per-(tenant, run) it HARD-REJECTS — never WARN-and-continue
(§5). Thresholds come from Settings (env-overridable), never hardcoded.
The quota pre-deduction under advisory lock lands in P5; this layer is the
static preflight in front of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.runtime.workflow_compiler import CompiledWorkflow
from app.runtime.workflow_definition import ArgSpec
from app.runtime.workflow_definition import AgentStep, FanoutStep, WaitUntilStep

_ARG_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class WorkflowAdmissionError(ValueError):
    """Raised when a run fails preflight — the run must not be created."""


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    max_run_budget_tokens: int
    max_fanout_items: int
    max_concurrency: int
    max_leaf_calls: int
    max_wall_clock_seconds: int

    @classmethod
    def from_settings(cls, settings: Any) -> "AdmissionLimits":
        return cls(
            max_run_budget_tokens=settings.WORKFLOW_MAX_RUN_BUDGET_TOKENS,
            max_fanout_items=settings.WORKFLOW_MAX_FANOUT_ITEMS,
            max_concurrency=settings.WORKFLOW_MAX_CONCURRENCY,
            max_leaf_calls=settings.WORKFLOW_MAX_LEAF_CALLS,
            max_wall_clock_seconds=settings.WORKFLOW_MAX_WALL_CLOCK_SECONDS,
        )


@dataclass(slots=True)
class AdmissionResult:
    admitted: bool
    planned_leaf_calls: int
    budget_tokens: int


def _resolve_args_path(path: str, args: dict) -> Any:
    parts = path.split(".")
    node: Any = args
    for part in parts[1:]:  # skip the 'args' root
        if not isinstance(node, dict) or part not in node:
            raise WorkflowAdmissionError(f"args reference {path!r} cannot be resolved from provided args")
        node = node[part]
    return node


def estimate_wait_seconds(step: WaitUntilStep, *, args: dict, now: datetime | None = None) -> int:
    """Return the wall-clock wait implied by a wait step.

    ``delay_seconds`` is already a bounded integer. ``until`` can be an ISO
    timestamp literal or an ``args.*`` reference; both must be resolved during
    admission so absolute waits cannot bypass wall-clock/risk caps.
    """
    if step.delay_seconds is not None:
        return step.delay_seconds

    target = step.until or ""
    if target.startswith("args."):
        target = str(_resolve_args_path(target, args))
    try:
        parsed = datetime.fromisoformat(target)
    except ValueError as exc:
        raise WorkflowAdmissionError(f"wait_until: invalid timestamp {target!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return max(0, int((parsed - current).total_seconds()))


def _validate_args(compiled: CompiledWorkflow, args: dict) -> None:
    for key, spec in compiled.definition.args_schema.items():
        if key not in args:
            if spec.required:
                raise WorkflowAdmissionError(f"args: missing required argument {key!r}")
            continue
        if not _ARG_TYPE_CHECKS[spec.type](args[key]):
            raise WorkflowAdmissionError(
                f"args: argument {key!r} must be of type {spec.type}, got {type(args[key]).__name__}"
            )
    unknown = set(args) - set(compiled.definition.args_schema)
    if unknown:
        raise WorkflowAdmissionError(f"args: unknown arguments {sorted(unknown)}")


def _normalize_arg_value(spec: ArgSpec, value: Any) -> Any:
    if spec.type == "array" and isinstance(value, dict) and set(value) == {"item"} and isinstance(value.get("item"), list):
        return value["item"]
    return value


def normalize_workflow_args(compiled: CompiledWorkflow, args: dict) -> dict:
    if not isinstance(args, dict):
        return args
    normalized = dict(args)
    for key, spec in compiled.definition.args_schema.items():
        if key in normalized:
            normalized[key] = _normalize_arg_value(spec, normalized[key])
    return normalized


def admit_workflow(
    compiled: CompiledWorkflow,
    *,
    args: dict,
    limits: AdmissionLimits,
    allowed_leaves: set[str] | None = None,
) -> AdmissionResult:
    args = normalize_workflow_args(compiled, args)
    _validate_args(compiled, args)

    budget = compiled.definition.default_budget.max_total_tokens
    if budget > limits.max_run_budget_tokens:
        raise WorkflowAdmissionError(f"budget: requested {budget} tokens exceeds limit {limits.max_run_budget_tokens}")

    if allowed_leaves is not None:
        unauthorized = compiled.leaf_names - allowed_leaves
        if unauthorized:
            raise WorkflowAdmissionError(f"leaf: unauthorized leaf capabilities {sorted(unauthorized)}")

    planned_leaves = 0
    total_wait_seconds = 0
    for step in compiled.definition.steps:
        if isinstance(step, AgentStep):
            planned_leaves += 1
        elif isinstance(step, FanoutStep):
            items = _resolve_args_path(step.items_from, args)
            if not isinstance(items, list):
                raise WorkflowAdmissionError(
                    f"args: fanout items reference {step.items_from!r} must resolve to an array"
                )
            if len(items) > limits.max_fanout_items:
                raise WorkflowAdmissionError(
                    f"fanout: {len(items)} items exceeds limit {limits.max_fanout_items} (step {step.id!r})"
                )
            if step.max_concurrency > limits.max_concurrency:
                raise WorkflowAdmissionError(
                    f"concurrency: {step.max_concurrency} exceeds limit {limits.max_concurrency} (step {step.id!r})"
                )
            planned_leaves += len(items)
        elif isinstance(step, WaitUntilStep):
            total_wait_seconds += estimate_wait_seconds(step, args=args)

    if planned_leaves > limits.max_leaf_calls:
        raise WorkflowAdmissionError(f"leaf: planned {planned_leaves} leaf calls exceed limit {limits.max_leaf_calls}")

    wall_clock_budget = compiled.definition.default_budget.max_wall_clock_seconds
    if wall_clock_budget is not None and wall_clock_budget > limits.max_wall_clock_seconds:
        raise WorkflowAdmissionError(
            f"wall clock: budget {wall_clock_budget}s exceeds limit {limits.max_wall_clock_seconds}s"
        )
    if total_wait_seconds > limits.max_wall_clock_seconds:
        raise WorkflowAdmissionError(
            f"wall clock: total wait {total_wait_seconds}s exceeds limit {limits.max_wall_clock_seconds}s"
        )

    return AdmissionResult(admitted=True, planned_leaf_calls=planned_leaves, budget_tokens=budget)
