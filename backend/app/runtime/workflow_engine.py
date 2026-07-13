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

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.runtime.workflow_compiler import CompiledWorkflow
from app.runtime.workflow_definition import (
    AgentStep,
    Condition,
    ConditionPredicate,
    FanoutStep,
    GateStep,
    LeafRef,
    WaitSignalStep,
    WaitUntilStep,
)

logger = logging.getLogger(__name__)

_TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*\}\}")

RunStatus = Literal["completed", "failed", "suspended", "killed"]


class WorkflowTemplateError(ValueError):
    """A template reference could not be resolved at runtime — fail loud."""


_WORKFLOW_LEAF_RECOVERY_NAMESPACE = uuid.UUID("6f8d4e61-4f22-5d85-9d3e-bc7396fbe2ea")


@dataclass(frozen=True, slots=True)
class WorkflowLeafRecoveryIdentity:
    """Canonical durable identity for one workflow leaf invocation."""

    runtime_task_id: str
    session_id: str
    trace_id: str
    step_id: str
    leaf_id: str


def workflow_leaf_recovery_identity(
    run_id: str | uuid.UUID,
    step_id: str,
    leaf_id: str | None = None,
) -> WorkflowLeafRecoveryIdentity:
    """Derive a restart-stable, fanout-safe recovery lane from journal keys."""

    run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    normalized_step_id = str(step_id or "").strip()
    if not normalized_step_id:
        raise ValueError("workflow recovery identity requires a step_id")
    normalized_leaf_id = str(leaf_id or "singleton").strip() or "singleton"
    lane_id = uuid.uuid5(
        _WORKFLOW_LEAF_RECOVERY_NAMESPACE,
        f"{run_uuid.hex}:{normalized_step_id}:{normalized_leaf_id}",
    )
    return WorkflowLeafRecoveryIdentity(
        runtime_task_id=run_uuid.hex,
        session_id=f"workflow-leaf-{lane_id.hex}",
        trace_id=f"workflow:{run_uuid.hex}:{normalized_step_id}:{normalized_leaf_id}",
        step_id=normalized_step_id,
        leaf_id=normalized_leaf_id,
    )


@dataclass(slots=True)
class LeafRequest:
    """What the engine hands to the injected leaf executor."""

    run_id: str
    step_id: str
    leaf: LeafRef
    task: str
    tenant_id: str | None = None
    # fanout item identity (None for plain agent_steps) — v1 decision 6
    leaf_id: str | None = None


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
class LeafRecord:
    """Leaf-journal view for fanout resume decisions (v1 decision 6)."""

    leaf_id: str
    status: str
    input_hash: str | None = None
    definition_hash: str | None = None
    output: Any = None
    result_ref: str | None = None
    error: str | None = None


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

    async def load_leaf_calls(self, run_id: str, step_id: str) -> dict[str, LeafRecord]: ...

    async def record_leaf_start(
        self,
        run_id: str,
        step_id: str,
        leaf_id: str,
        *,
        input_hash: str | None,
        definition_hash: str,
        idempotency_key: str,
    ) -> None: ...

    async def record_leaf_done(
        self, run_id: str, step_id: str, leaf_id: str, *, output: Any, tokens_used: int
    ) -> None: ...

    async def record_leaf_failed(self, run_id: str, step_id: str, leaf_id: str, *, error: str) -> None: ...


class QuotaReserver(Protocol):
    """Per-run budget envelope (§3.4-4): pre-reserve before every leaf spawn,
    settle with actual usage after. The PG implementation holds a Postgres
    advisory lock around the conditional deduction (P5)."""

    async def reserve(self, run_id: str, *, reservation_key: str) -> bool: ...

    async def mark_execution_started(
        self,
        run_id: str,
        *,
        reservation_key: str,
        step_id: str,
        leaf_id: str | None,
        input_hash: str,
    ) -> None: ...

    async def mark_execution_unknown(self, run_id: str, *, reservation_key: str, error: str) -> None: ...

    async def settle(self, run_id: str, actual_tokens: int, *, reservation_key: str) -> None: ...


def workflow_quota_logical_key(
    run_id: str,
    step_id: str,
    *,
    leaf_id: str | None,
    input_hash: str,
) -> str:
    """Stable identity for one logical leaf input across process restarts.

    The persistent reserver appends a monotonically allocated attempt suffix.
    An unresolved attempt is reused after a reserve->journal crash; once it is
    settled, a subsequent actual retry receives the next attempt number.
    """

    return ":".join(
        (
            str(run_id),
            str(step_id),
            str(leaf_id or "singleton"),
            str(input_hash),
        )
    )


class InMemoryQuotaReserver:
    """Control-flow test double for the quota envelope."""

    def __init__(self, *, allocated: int, leaf_estimate: int) -> None:
        self.allocated = allocated
        self.leaf_estimate = leaf_estimate
        self.consumed = 0
        self._attempts: dict[str, list[dict[str, int | bool]]] = {}

    async def reserve(self, run_id: str, *, reservation_key: str) -> bool:
        attempts = self._attempts.setdefault(reservation_key, [])
        if attempts and not bool(attempts[-1]["settled"]):
            return True
        if self.consumed + self.leaf_estimate > self.allocated:
            return False
        self.consumed += self.leaf_estimate
        attempts.append({"estimated": self.leaf_estimate, "settled": False})
        return True

    async def settle(self, run_id: str, actual_tokens: int, *, reservation_key: str) -> None:
        attempts = self._attempts.get(reservation_key) or []
        if not attempts or bool(attempts[-1]["settled"]):
            return
        estimate = int(attempts[-1]["estimated"])
        self.consumed += actual_tokens - estimate
        attempts[-1]["settled"] = True

    async def mark_execution_started(
        self,
        run_id: str,
        *,
        reservation_key: str,
        step_id: str,
        leaf_id: str | None,
        input_hash: str,
    ) -> None:
        del run_id, reservation_key, step_id, leaf_id, input_hash

    async def mark_execution_unknown(self, run_id: str, *, reservation_key: str, error: str) -> None:
        del run_id, reservation_key, error


async def _quota_execution_started(
    quota: QuotaReserver | None,
    run_id: str,
    *,
    reservation_key: str,
    step_id: str,
    leaf_id: str | None,
    input_hash: str,
) -> None:
    if quota is None:
        return
    marker = getattr(quota, "mark_execution_started", None)
    if marker is not None:
        await marker(
            run_id,
            reservation_key=reservation_key,
            step_id=step_id,
            leaf_id=leaf_id,
            input_hash=input_hash,
        )


async def _quota_execution_unknown(
    quota: QuotaReserver | None,
    run_id: str,
    *,
    reservation_key: str,
    error: BaseException,
) -> None:
    if quota is None:
        return
    marker = getattr(quota, "mark_execution_unknown", None)
    if marker is None:
        raise RuntimeError("quota boundary cannot persist an unknown execution outcome") from error
    await marker(run_id, reservation_key=reservation_key, error=str(error)[:4000])


@dataclass(slots=True)
class GateDecision:
    """Tri-state gate verdict: approved runs on, pending suspends, rejected fails."""

    approved: bool = False
    pending: bool = False
    rejected: bool = False
    reason: str | None = None


class GateDecider(Protocol):
    """gate_step arbitration seam — the PG implementation binds to
    CoordinationCheckpoint (§6 / P7); in-memory doubles drive engine tests."""

    async def check(self, run_id: str, step_id: str, *, reason: str) -> GateDecision: ...


class SignalWaitRegistrar(Protocol):
    """wait_signal seam (§9 P11): record that a suspended run waits for a
    Signal; the persistent consumer matches arriving PG signals against these
    registrations and resumes the run."""

    async def register_wait(self, run_id: str, *, step_id: str, signal_type: str) -> None: ...


class WaitScheduler(Protocol):
    """Time-suspend seam: record when a suspended run should be resumed.
    P7 keeps an equivalent scheduling record (run metadata resume_at); the
    once-trigger binding lands with P8 (§6.2 — once is ONLY a time resume)."""

    async def schedule_resume(self, run_id: str, *, step_id: str, resume_at: datetime) -> None: ...

    async def clear_resume(self, run_id: str, *, step_id: str) -> None: ...


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class WorkflowRunOutcome:
    status: RunStatus
    reason: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


class InMemoryWorkflowJournal:
    """Control-flow test double (journal *persistence* is tested on real PG)."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, StepRecord]] = {}
        self._leaf_runs: dict[tuple[str, str], dict[str, LeafRecord]] = {}

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
        record = self._steps(run_id).setdefault(step_id, StepRecord(step_id=step_id, status="suspended"))
        record.status = "suspended"
        record.error = reason  # field-level update mirrors the PG journal: input_hash survives

    def _leaves(self, run_id: str, step_id: str) -> dict[str, LeafRecord]:
        return self._leaf_runs.setdefault((run_id, step_id), {})

    async def load_leaf_calls(self, run_id: str, step_id: str) -> dict[str, LeafRecord]:
        return dict(self._leaves(run_id, step_id))

    async def record_leaf_start(
        self,
        run_id: str,
        step_id: str,
        leaf_id: str,
        *,
        input_hash: str | None,
        definition_hash: str,
        idempotency_key: str,
    ) -> None:
        self._leaves(run_id, step_id)[leaf_id] = LeafRecord(
            leaf_id=leaf_id, status="running", input_hash=input_hash, definition_hash=definition_hash
        )

    async def record_leaf_done(self, run_id: str, step_id: str, leaf_id: str, *, output: Any, tokens_used: int) -> None:
        record = self._leaves(run_id, step_id)[leaf_id]
        record.status = "done"
        record.output = output

    async def record_leaf_failed(self, run_id: str, step_id: str, leaf_id: str, *, error: str) -> None:
        record = self._leaves(run_id, step_id).setdefault(leaf_id, LeafRecord(leaf_id=leaf_id, status="running"))
        record.status = "failed"
        record.error = error


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


def _leaf_input_hash(task_text: str, leaf: LeafRef, definition_hash: str, leaf_id: str) -> str:
    payload = json.dumps(
        {"task": task_text, "leaf": leaf.name, "definition_hash": definition_hash, "leaf_id": leaf_id},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _agent_step_input_hash(task_text: str, leaf: LeafRef, definition_hash: str) -> str:
    payload = json.dumps(
        {"task": task_text, "leaf": leaf.name, "definition_hash": definition_hash},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_wait_target(step: WaitUntilStep, *, args: dict, current: datetime) -> datetime:
    """delay_seconds → now+delay; until → ISO literal or args.* reference."""
    from datetime import timedelta

    if step.delay_seconds is not None:
        return current + timedelta(seconds=step.delay_seconds)
    target = step.until or ""
    if target.startswith("args."):
        resolved = _resolve_path(target, args=args, outputs={})
        target = str(resolved)
    parsed = datetime.fromisoformat(target)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _always_continue() -> bool:
    return True


async def _execute_fanout_step(
    step: FanoutStep,
    *,
    compiled_hash: str,
    run_id: str,
    args: dict,
    outputs: dict[str, Any],
    journal: WorkflowJournal,
    leaf_executor: LeafExecutor,
    should_continue: ShouldContinue,
    tenant_id: str | None,
    quota: QuotaReserver | None,
) -> WorkflowRunOutcome | None:
    """Bounded fanout/map with leaf-level journal (v1 decision 6).

    Returns a terminal WorkflowRunOutcome on failure/suspension, or None when
    the step completed and the outer loop should continue. Done leaves with
    matching input/definition hashes are replayed (output reused, executor
    NOT called, quota NOT charged); failures are isolated per leaf — every
    leaf gets its attempt, then the step fails if any did."""
    try:
        items = _resolve_path(step.items_from, args=args, outputs=outputs)
    except WorkflowTemplateError as exc:
        await journal.record_step_failed(run_id, step.id, error=str(exc))
        return WorkflowRunOutcome(status="failed", reason=str(exc), outputs=outputs)
    if not isinstance(items, list):
        error = f"fanout items reference {step.items_from!r} must resolve to an array"
        await journal.record_step_failed(run_id, step.id, error=error)
        return WorkflowRunOutcome(status="failed", reason=error, outputs=outputs)

    existing = await journal.load_leaf_calls(run_id, step.id)
    await journal.record_step_start(
        run_id, step.id, step_type=step.type, input_hash=None, definition_hash=compiled_hash
    )

    semaphore = asyncio.Semaphore(step.max_concurrency)
    results: list[Any] = [None] * len(items)
    failures: list[str] = []
    budget_exhausted = asyncio.Event()
    stop_requested = asyncio.Event()
    leaf_errors: list[BaseException] = []

    async def run_leaf(index: int, item: Any) -> None:
        leaf_id = f"item-{index}"
        try:
            task_text = resolve_template(step.per_item_task, args=args, outputs=outputs, item=item)
        except WorkflowTemplateError as exc:
            try:
                await journal.record_leaf_failed(run_id, step.id, leaf_id, error=str(exc))
                failures.append(f"{leaf_id}: {exc}")
            except Exception as journal_exc:
                stop_requested.set()
                leaf_errors.append(journal_exc)
            return
        input_hash = _leaf_input_hash(task_text, step.leaf, compiled_hash, leaf_id)
        prior = existing.get(leaf_id)
        if (
            prior is not None
            and prior.status == "done"
            and prior.input_hash == input_hash
            and prior.definition_hash == compiled_hash
        ):
            results[index] = prior.output
            return  # replay: executor not called, quota not charged

        quota_key: str | None = None
        reservation_live = False
        execution_started = False
        try:
            async with semaphore:
                if budget_exhausted.is_set() or stop_requested.is_set() or not await should_continue():
                    stop_requested.set()
                    return
                quota_key = workflow_quota_logical_key(
                    run_id,
                    step.id,
                    leaf_id=leaf_id,
                    input_hash=input_hash,
                )
                if quota is not None and not await quota.reserve(run_id, reservation_key=quota_key):
                    budget_exhausted.set()
                    return
                reservation_live = quota is not None
                try:
                    await journal.record_leaf_start(
                        run_id,
                        step.id,
                        leaf_id,
                        input_hash=input_hash,
                        definition_hash=compiled_hash,
                        idempotency_key=f"{step.id}:{leaf_id}:{input_hash[:16]}",
                    )
                    if not await should_continue():
                        stop_requested.set()
                        if quota is not None:
                            await quota.settle(run_id, 0, reservation_key=quota_key)
                            reservation_live = False
                        return
                    await _quota_execution_started(
                        quota,
                        run_id,
                        reservation_key=quota_key,
                        step_id=step.id,
                        leaf_id=leaf_id,
                        input_hash=input_hash,
                    )
                    execution_started = True
                except (Exception, asyncio.CancelledError):
                    if quota is not None and reservation_live and not execution_started:
                        await quota.settle(run_id, 0, reservation_key=quota_key)
                        reservation_live = False
                    raise
                try:
                    outcome = await leaf_executor(
                        LeafRequest(
                            run_id=run_id,
                            step_id=step.id,
                            leaf=step.leaf,
                            task=task_text,
                            tenant_id=tenant_id,
                            leaf_id=leaf_id,
                        )
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    await _quota_execution_unknown(quota, run_id, reservation_key=quota_key, error=exc)
                    reservation_live = False
                    raise
                if quota is not None:
                    try:
                        await quota.settle(run_id, outcome.tokens_used, reservation_key=quota_key)
                        reservation_live = False
                    except (Exception, asyncio.CancelledError) as exc:
                        await _quota_execution_unknown(quota, run_id, reservation_key=quota_key, error=exc)
                        reservation_live = False
                        raise
                if outcome.ok:
                    await journal.record_leaf_done(
                        run_id, step.id, leaf_id, output=outcome.output, tokens_used=outcome.tokens_used
                    )
                    results[index] = outcome.output
                else:
                    error = outcome.error or "leaf execution failed"
                    await journal.record_leaf_failed(run_id, step.id, leaf_id, error=error)
                    failures.append(f"{leaf_id}: {error}")
                if not await should_continue():
                    stop_requested.set()
        except (Exception, asyncio.CancelledError) as exc:
            # Never let one infrastructure boundary unwind ``gather`` while
            # sibling leaves are still live. Stop admission of new leaves,
            # remember the failure, and let every already-started sibling
            # quiesce before the caller aggregates evidence/releases its lease.
            stop_requested.set()
            leaf_errors.append(exc)

    await asyncio.gather(*(run_leaf(index, item) for index, item in enumerate(items)))

    if leaf_errors:
        raise leaf_errors[0]
    if stop_requested.is_set() or not await should_continue():
        return WorkflowRunOutcome(status="killed", reason="kill requested", outputs=outputs)
    if budget_exhausted.is_set():
        reason = "budget exhausted: run quota cannot cover the next leaf"
        await journal.record_step_suspended(run_id, step.id, reason=reason)
        return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)
    if failures:
        error = "; ".join(failures[:5])
        await journal.record_step_failed(run_id, step.id, error=error)
        return WorkflowRunOutcome(status="failed", reason=error, outputs=outputs)

    await journal.record_step_done(run_id, step.id, output=results, result_ref=None)
    outputs[step.id] = results
    return None


async def execute_workflow(
    compiled: CompiledWorkflow,
    *,
    run_id: str,
    args: dict,
    journal: WorkflowJournal,
    leaf_executor: LeafExecutor,
    should_continue: ShouldContinue | None = None,
    tenant_id: str | None = None,
    quota: QuotaReserver | None = None,
    gate_decider: GateDecider | None = None,
    wait_scheduler: WaitScheduler | None = None,
    signal_wait_registrar: SignalWaitRegistrar | None = None,
    now: Clock | None = None,
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

            # v1 decision 1: retry is reversible-only (the compiler refuses
            # retry on external/irreversible steps, so attempts>1 here is
            # always safe to re-run).
            attempts = 1 + (step.retry.max_attempts if step.retry is not None else 0)
            outcome: LeafOutcome | None = None
            quota_key = workflow_quota_logical_key(
                run_id,
                step.id,
                leaf_id=None,
                input_hash=input_hash,
            )
            for attempt in range(attempts):
                if not await check():
                    return WorkflowRunOutcome(status="killed", reason="kill requested", outputs=outputs)
                if quota is not None and not await quota.reserve(run_id, reservation_key=quota_key):
                    reason = "budget exhausted: run quota cannot cover the next leaf"
                    await journal.record_step_suspended(run_id, step.id, reason=reason)
                    return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

                reservation_live = quota is not None
                execution_started = False
                try:
                    await journal.record_step_start(
                        run_id,
                        step.id,
                        step_type=step.type,
                        input_hash=input_hash,
                        definition_hash=compiled.definition_hash,
                    )
                    if not await check():
                        if quota is not None:
                            await quota.settle(run_id, 0, reservation_key=quota_key)
                            reservation_live = False
                        return WorkflowRunOutcome(status="killed", reason="kill requested", outputs=outputs)
                    await _quota_execution_started(
                        quota,
                        run_id,
                        reservation_key=quota_key,
                        step_id=step.id,
                        leaf_id=None,
                        input_hash=input_hash,
                    )
                    execution_started = True
                except (Exception, asyncio.CancelledError):
                    if quota is not None and reservation_live and not execution_started:
                        await quota.settle(run_id, 0, reservation_key=quota_key)
                    raise
                try:
                    outcome = await leaf_executor(
                        LeafRequest(
                            run_id=run_id,
                            step_id=step.id,
                            leaf=step.leaf,
                            task=task_text,
                            tenant_id=tenant_id,
                        )
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    await _quota_execution_unknown(quota, run_id, reservation_key=quota_key, error=exc)
                    raise
                if quota is not None:
                    try:
                        await quota.settle(run_id, outcome.tokens_used, reservation_key=quota_key)
                    except (Exception, asyncio.CancelledError) as exc:
                        await _quota_execution_unknown(quota, run_id, reservation_key=quota_key, error=exc)
                        raise
                if outcome.ok:
                    break
                logger.warning(
                    "[Workflow] step %s attempt %d/%d failed: %s", step.id, attempt + 1, attempts, outcome.error
                )
            assert outcome is not None
            if not outcome.ok:
                error = outcome.error or "leaf execution failed"
                await journal.record_step_failed(run_id, step.id, error=error)
                return WorkflowRunOutcome(status="failed", reason=error, outputs=outputs)

            await journal.record_step_done(run_id, step.id, output=outcome.output, result_ref=outcome.result_ref)
            outputs[step.id] = outcome.output
            continue

        if isinstance(step, FanoutStep):
            result = await _execute_fanout_step(
                step,
                compiled_hash=compiled.definition_hash,
                run_id=run_id,
                args=args,
                outputs=outputs,
                journal=journal,
                leaf_executor=leaf_executor,
                should_continue=check,
                tenant_id=tenant_id,
                quota=quota,
            )
            if result is not None:
                return result
            continue

        if isinstance(step, GateStep):
            prior = existing.get(step.id)
            if prior is not None and prior.status == "done" and prior.definition_hash == compiled.definition_hash:
                outputs[step.id] = prior.output
                continue  # gate already approved in a previous pass
            if gate_decider is None:
                reason = f"gate step {step.id!r} has no decider bound — suspending (fail-closed)"
                await journal.record_step_suspended(run_id, step.id, reason=reason)
                return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)
            decision = await gate_decider.check(run_id, step.id, reason=step.reason)
            if decision.approved:
                await journal.record_step_start(
                    run_id, step.id, step_type=step.type, input_hash=None, definition_hash=compiled.definition_hash
                )
                output = {"approved": True, "reason": step.reason}
                await journal.record_step_done(run_id, step.id, output=output, result_ref=None)
                outputs[step.id] = output
                continue
            if decision.rejected:
                error = decision.reason or f"gate {step.id!r} rejected"
                await journal.record_step_failed(run_id, step.id, error=error)
                return WorkflowRunOutcome(status="failed", reason=error, outputs=outputs)
            reason = decision.reason or f"gate {step.id!r} awaiting approval: {step.reason}"
            await journal.record_step_suspended(run_id, step.id, reason=reason)
            return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

        if isinstance(step, WaitUntilStep):
            prior = existing.get(step.id)
            if prior is not None and prior.status == "done" and prior.definition_hash == compiled.definition_hash:
                outputs[step.id] = prior.output
                continue
            clock = now or _utc_now
            current = clock()
            # The wait target is FIXED on first suspension (journaled in
            # input_hash) — recomputing delay_seconds on every resume would
            # push the target forever forward and the run would never land.
            resume_at: datetime | None = None
            if prior is not None and prior.input_hash:
                try:
                    resume_at = datetime.fromisoformat(prior.input_hash)
                except ValueError:
                    resume_at = None
            if resume_at is None:
                resume_at = _resolve_wait_target(step, args=args, current=current)
                await journal.record_step_start(
                    run_id,
                    step.id,
                    step_type=step.type,
                    input_hash=resume_at.isoformat(),
                    definition_hash=compiled.definition_hash,
                )
            if resume_at <= current:
                output = {"waited_until": resume_at.isoformat()}
                await journal.record_step_done(run_id, step.id, output=output, result_ref=None)
                if wait_scheduler is not None:
                    await wait_scheduler.clear_resume(run_id, step_id=step.id)
                outputs[step.id] = output
                continue
            if wait_scheduler is not None:
                await wait_scheduler.schedule_resume(run_id, step_id=step.id, resume_at=resume_at)
            reason = f"waiting until {resume_at.isoformat()}"
            await journal.record_step_suspended(run_id, step.id, reason=reason)
            return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

        if isinstance(step, WaitSignalStep):
            prior = existing.get(step.id)
            if prior is not None and prior.status == "done" and prior.definition_hash == compiled.definition_hash:
                outputs[step.id] = prior.output
                continue  # the consumer marked the signal consumed → replay
            await journal.record_step_start(
                run_id,
                step.id,
                step_type=step.type,
                input_hash=step.signal_type,
                definition_hash=compiled.definition_hash,
            )
            if signal_wait_registrar is not None:
                await signal_wait_registrar.register_wait(run_id, step_id=step.id, signal_type=step.signal_type)
            reason = f"waiting for signal {step.signal_type!r}"
            await journal.record_step_suspended(run_id, step.id, reason=reason)
            return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

        # Unknown step type (future schema additions): suspend, never guess.
        reason = f"step type {step.type!r} is not executable by this engine version"
        await journal.record_step_suspended(run_id, step.id, reason=reason)
        return WorkflowRunOutcome(status="suspended", reason=reason, outputs=outputs)

    return WorkflowRunOutcome(status="completed", outputs=outputs)
