"""WorkflowEngine (§9 P3, §3.4-2): thin deterministic interpreter.

Kernel-style purity: ZERO DB imports — the journal and the leaf executor are
injected (the real-PG journal lives in services/workflow_runtime_service.py;
the production leaf executor binds to axis-1 ``spawn_subagent`` in P4).
Code owns the control flow; the LLM only ever runs inside a leaf.

P3 scope: ``sequence`` + structured ``condition`` + ``agent_step`` with
resume-by-journal (done step with matching ``input_hash`` + matching
``definition_hash`` is skipped and its output replayed). ``gate_step`` /
``wait_until_step`` / ``fanout_step`` conservatively SUSPEND the run —
P5/P7 implement them; suspending can never wrongly execute.

Template resolution is pure key lookup over ``{{args.x}}`` /
``{{steps.<id>.output...}}`` / ``{{item}}`` — reference substitution, not
expression evaluation (§3.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.runtime.workflow_compiler import CompiledWorkflow
from app.runtime.workflow_definition import (
    AgentStep,
    Condition,
    ConditionPredicate,
    LeafRef,
)

logger = logging.getLogger(__name__)

_TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*\}\}")

RunStatus = Literal["completed", "failed", "suspended", "killed"]


class WorkflowTemplateError(ValueError):
    """A template reference could not be resolved at runtime — fail loud."""


@dataclass(slots=True)
class LeafRequest:
    """What the engine hands to the injected leaf executor."""

    run_id: str
    step_id: str
    leaf: LeafRef
    task: str
    tenant_id: str | None = None


@dataclass(slots=True)
class LeafOutcome:
    ok: bool
    output: Any = None
    result_ref: str | None = None
    error: str | None = None
    tokens_used: int = 0


LeafExecutor = Callable[[LeafRequest], Awaitable[LeafOutcome]]
ShouldContinue = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class StepRecord:
    """Journal view the engine consumes for resume decisions."""

    step_id: str
    status: str
    input_hash: str | None = None
    definition_hash: str | None = None
    output: Any = None
    result_ref: str | None = None
    error: str | None = None


class WorkflowJournal(Protocol):
    """Persistence seam — implemented in-memory (tests) and on PG (service)."""

    async def load_steps(self, run_id: str) -> dict[str, StepRecord]: ...

    async def record_step_start(
        self, run_id: str, step_id: str, *, step_type: str, input_hash: str | None, definition_hash: str
    ) -> None: ...

    async def record_step_done(self, run_id: str, step_id: str, *, output: Any, result_ref: str | None) -> None: ...

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None: ...

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None: ...

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None: ...


@dataclass(slots=True)
class WorkflowRunOutcome:
    status: RunStatus
    reason: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


class InMemoryWorkflowJournal:
    """Control-flow test double (journal *persistence* is tested on real PG)."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, StepRecord]] = {}

    def _steps(self, run_id: str) -> dict[str, StepRecord]:
        return self._runs.setdefault(run_id, {})

    def statuses(self, run_id: str) -> dict[str, str]:
        return {sid: rec.status for sid, rec in self._steps(run_id).items()}

    async def load_steps(self, run_id: str) -> dict[str, StepRecord]:
        return dict(self._steps(run_id))

    async def record_step_start(
        self, run_id: str, step_id: str, *, step_type: str, input_hash: str | None, definition_hash: str
    ) -> None:
        self._steps(run_id)[step_id] = StepRecord(
            step_id=step_id, status="running", input_hash=input_hash, definition_hash=definition_hash
        )

    async def record_step_done(self, run_id: str, step_id: str, *, output: Any, result_ref: str | None) -> None:
        record = self._steps(run_id)[step_id]
        record.status = "done"
        record.output = output
        record.result_ref = result_ref

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None:
        record = self._steps(run_id).setdefault(step_id, StepRecord(step_id=step_id, status="running"))
        record.status = "failed"
        record.error = error

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None:
        self._steps(run_id)[step_id] = StepRecord(step_id=step_id, status="skipped", definition_hash=definition_hash)

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None:
        self._steps(run_id)[step_id] = StepRecord(step_id=step_id, status="suspended", error=reason)


# ── reference resolution (pure key lookup — §3.2) ─────────────────


def _resolve_path(ref: str, *, args: dict, outputs: dict[str, Any]) -> Any:
    parts = ref.split(".")
    root = parts[0]
    if root == "args":
        node: Any = args
        walk = parts[1:]
    elif root == "steps":
        if len(parts) < 3 or parts[2] != "output":
            raise WorkflowTemplateError(f"step reference {ref!r} must be steps.<id>.output[...]")
        if parts[1] not in outputs:
            raise WorkflowTemplateError(f"step reference {ref!r}: no recorded output for step {parts[1]!r}")
        node = outputs[parts[1]]
        walk = parts[3:]
    else:
        raise WorkflowTemplateError(f"unsupported reference root in {ref!r}")

    for part in walk:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise WorkflowTemplateError(f"reference {ref!r}: path segment {part!r} not found")
    return node


def resolve_template(text: str, *, args: dict, outputs: dict[str, Any], item: Any = None) -> str:
    """Substitute ``{{...}}`` placeholders by key lookup. No expressions."""

    def _sub(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref == "item":
            value: Any = item
        else:
            value = _resolve_path(ref, args=args, outputs=outputs)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    return _TEMPLATE_REF_RE.sub(_sub, text)


# ── condition evaluation (comparison AST — §10 decision 2) ────────


def _evaluate_predicate(predicate: ConditionPredicate, *, args: dict, outputs: dict[str, Any]) -> bool:
    try:
        value = _resolve_path(predicate.field, args=args, outputs=outputs)
        resolved = True
    except WorkflowTemplateError:
        value = None
        resolved = False

    op = predicate.op
    if op == "exists":
        return resolved
    if not resolved:
        return False
    expected = predicate.value
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op in ("gt", "lt", "gte", "lte"):
        try:
            if op == "gt":
                return value > expected
            if op == "lt":
                return value < expected
            if op == "gte":
                return value >= expected
            return value <= expected
        except TypeError:
            return False
    if op == "contains":
        try:
            return expected in value
        except TypeError:
            return False
    if op == "in":
        try:
            return value in expected
        except TypeError:
            return False
    raise WorkflowTemplateError(f"unsupported condition op {op!r}")  # pragma: no cover — schema forbids


def evaluate_condition(condition: Condition, *, args: dict, outputs: dict[str, Any]) -> bool:
    if condition.predicate is not None:
        return _evaluate_predicate(condition.predicate, args=args, outputs=outputs)
    if condition.not_ is not None:
        return not evaluate_condition(condition.not_, args=args, outputs=outputs)
    if condition.all is not None:
        return all(evaluate_condition(c, args=args, outputs=outputs) for c in condition.all)
    if condition.any is not None:
        return any(evaluate_condition(c, args=args, outputs=outputs) for c in condition.any)
    raise WorkflowTemplateError("empty condition node")  # pragma: no cover — schema forbids


# ── the interpreter ───────────────────────────────────────────────


def _agent_step_input_hash(task_text: str, leaf: LeafRef, definition_hash: str) -> str:
    payload = json.dumps(
        {"task": task_text, "leaf": leaf.name, "definition_hash": definition_hash},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _always_continue() -> bool:
    return True


async def execute_workflow(
    compiled: CompiledWorkflow,
    *,
    run_id: str,
    args: dict,
    journal: WorkflowJournal,
    leaf_executor: LeafExecutor,
    should_continue: ShouldContinue | None = None,
    tenant_id: str | None = None,
) -> WorkflowRunOutcome:
    """Deterministically interpret the compiled definition against the journal.

    Resume contract: a journaled ``done`` step is REUSED (its output replayed,
    the leaf NOT re-executed) iff its ``input_hash`` and ``definition_hash``
    both still match; anything else re-runs. Steps run strictly in sequence.
    """
    check = should_continue or _always_continue
    existing = await journal.load_steps(run_id)
    outputs: dict[str, Any] = {}

    for step in compiled.definition.steps:
        if not await check():
            return WorkflowRunOutcome(status="killed", reason="kill requested", outputs=outputs)

        # Condition gate (skip branch, journal the skip).
        if step.when is not None and not evaluate_condition(step.when, args=args, outputs=outputs):
            await journal.record_step_skipped(run_id, step.id, definition_hash=compiled.definition_hash)
            continue

        if isinstance(step, AgentStep):
            try:
                task_text = resolve_template(step.task, args=args, outputs=outputs)
            except WorkflowTemplateError as exc:
                await journal.record_step_failed(run_id, step.id, error=str(exc))
                return WorkflowRunOutcome(status="failed", reason=str(exc), outputs=outputs)

            input_hash = _agent_step_input_hash(task_text, step.leaf, compiled.definition_hash)
            prior = existing.get(step.id)
            if (
                prior is not None
                and prior.status == "done"
                and prior.input_hash == input_hash
                and prior.definition_hash == compiled.definition_hash
            ):
                outputs[step.id] = prior.output
                continue  # resume: replay journaled output, never re-execute

            await journal.record_step_start(
                run_id, step.id, step_type=step.type, input_hash=input_hash, definition_hash=compiled.definition_hash
            )
            outcome = await leaf_executor(
                LeafRequest(run_id=run_id, step_id=step.id, leaf=step.leaf, task=task_text, tenant_id=tenant_id)
            )
            if not outcome.ok:
                error = outcome.error or "leaf execution failed"
                await journal.record_step_failed(run_id, step.id, error=error)
                return WorkflowRunOutcome(status="failed", reason=error, outputs=outputs)

            await journal.record_step_done(run_id, step.id, output=outcome.output, result_ref=outcome.result_ref)
            outputs[step.id] = outcome.output
            continue

        # P3 placeholder semantics: gate / wait / fanout suspend (P5/P7
        # implement them) — conservative, never wrongly executes.
        reason = f"step type {step.type!r} suspends in P3 (implemented in P5/P7)"
        await journal.record_step_suspended(run_id, step.id, reason=reason)
        return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

    return WorkflowRunOutcome(status="completed", outputs=outputs)
