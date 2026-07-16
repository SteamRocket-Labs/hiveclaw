"""Mechanical root admission, coverage, path, and terminal facts.

This service deliberately does not decide *what* work is important.  It only
preserves the platform-owned facts required to keep mixed direct/Subagent/
Team/Workflow/A2A fan-out recoverable and auditable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from app.models.runtime_root_item import RuntimeRootItem


RUNTIME_ROOT_TERMINAL_STATES = frozenset({"completed", "failed", "killed", "skipped", "cancelled", "not_admitted"})
RUNTIME_ROOT_STATES = frozenset(
    {
        "requested",
        "waiting_approval",
        "queued",
        "running",
        "completed",
        "failed",
        "killed",
        "skipped",
        "cancelled",
        "suspended",
        "needs_reconciliation",
        "not_admitted",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeRootCoverage:
    requested: int
    admitted: int
    deferred: int
    not_admitted: int
    expected: int
    terminal: int
    running: int
    waiting_approval: int

    @property
    def conserved(self) -> bool:
        return self.requested == self.admitted + self.deferred + self.not_admitted

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "requested": self.requested,
            "admitted": self.admitted,
            "deferred": self.deferred,
            "not_admitted": self.not_admitted,
            "expected": self.expected,
            "terminal": self.terminal,
            "running": self.running,
            "waiting_approval": self.waiting_approval,
            "conserved": self.conserved,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRootPathDecision:
    path: tuple[str, ...]
    cycle_detected: bool
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class RuntimeRootTransitionDecision:
    applied: bool
    effective_state: str
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeRootIntentSpec:
    """Typed caller-owned identity for one mechanical requested work item."""

    intent_key: str
    work_type: str
    target_ref: str
    path: tuple[str, ...] = ()
    state: str = "queued"
    admission_disposition: str | None = None
    reason_code: str | None = None
    approval_ref: str | None = None
    budget_reservation_key: str | None = None
    metadata: dict[str, Any] | None = None


def _uuid(value: uuid.UUID | str | None, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def summarize_runtime_root_items(items: Iterable[Any]) -> RuntimeRootCoverage:
    rows = list(items)
    dispositions = [str(getattr(item, "admission_disposition", "requested") or "requested") for item in rows]
    states = [str(getattr(item, "state", "requested") or "requested") for item in rows]
    coverage = RuntimeRootCoverage(
        requested=len(rows),
        admitted=sum(value == "admitted" for value in dispositions),
        # A crash may leave a requested row before an admission decision is
        # attached.  It is mechanically deferred, never silently omitted from
        # the conservation equation.
        deferred=sum(value in {"requested", "deferred"} for value in dispositions),
        not_admitted=sum(value == "not_admitted" for value in dispositions),
        expected=sum(value == "admitted" for value in dispositions),
        # terminal measures admitted child executions only, so convergence can
        # be tested as terminal == expected.  Rejected work remains visible in
        # not_admitted and must never inflate completed-child progress.
        terminal=sum(
            disposition == "admitted" and state in RUNTIME_ROOT_TERMINAL_STATES
            for disposition, state in zip(dispositions, states, strict=True)
        ),
        running=sum(value in {"queued", "running", "needs_reconciliation"} for value in states),
        waiting_approval=sum(value == "waiting_approval" for value in states),
    )
    if not coverage.conserved:
        raise ValueError("runtime root coverage is not conserved")
    return coverage


def build_runtime_root_path(existing_path: Sequence[str] | None, *, target_ref: str) -> RuntimeRootPathDecision:
    normalized = tuple(str(value).strip() for value in (existing_path or ()) if str(value).strip())
    target = str(target_ref or "").strip()
    if not target:
        raise ValueError("target_ref is required")
    path = (*normalized, target)
    cycle_detected = target in normalized
    return RuntimeRootPathDecision(
        path=path,
        cycle_detected=cycle_detected,
        reason_code="runtime_root_cycle_detected" if cycle_detected else None,
    )


def evaluate_runtime_root_transition(*, current_state: str, requested_state: str) -> RuntimeRootTransitionDecision:
    current = str(current_state or "").strip()
    requested = str(requested_state or "").strip()
    if current not in RUNTIME_ROOT_STATES:
        raise ValueError(f"unsupported current root item state: {current!r}")
    if requested not in RUNTIME_ROOT_STATES:
        raise ValueError(f"unsupported requested root item state: {requested!r}")
    if current in RUNTIME_ROOT_TERMINAL_STATES:
        return RuntimeRootTransitionDecision(
            applied=False,
            effective_state=current,
            reason_code="terminal_state_already_sealed",
        )
    if current == requested:
        return RuntimeRootTransitionDecision(applied=False, effective_state=current, reason_code="state_unchanged")
    return RuntimeRootTransitionDecision(applied=True, effective_state=requested)


def _disposition_for_state(state: str, current: str = "requested") -> str:
    if state == "waiting_approval":
        return "deferred"
    if state == "not_admitted":
        return "not_admitted"
    if state == "requested":
        return "deferred" if current == "requested" else current
    return "admitted"


async def register_runtime_root_item(
    db: Any,
    *,
    tenant_id: uuid.UUID | str,
    root_runtime_task_id: uuid.UUID | str,
    source_agent_id: uuid.UUID | str | None,
    intent_key: str,
    work_type: str,
    target_ref: str,
    path: Sequence[str] | None = None,
    parent_runtime_task_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    root_user_id: uuid.UUID | str | None = None,
    root_session_id: str | None = None,
    state: str = "requested",
    admission_disposition: str | None = None,
    reason_code: str | None = None,
    budget_reservation_key: str | None = None,
    approval_ref: str | None = None,
    child_session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeRootItem:
    root_id = _uuid(root_runtime_task_id, field="root_runtime_task_id")
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    source_uuid = _uuid(source_agent_id, field="source_agent_id") if source_agent_id else None
    key = str(intent_key or "").strip()
    kind = str(work_type or "").strip()
    if not key or not kind:
        raise ValueError("intent_key and work_type are required")
    path_decision = build_runtime_root_path(path, target_ref=target_ref)
    effective_state = "not_admitted" if path_decision.cycle_detected else state
    if effective_state not in RUNTIME_ROOT_STATES:
        raise ValueError(f"unsupported root item state: {effective_state!r}")
    effective_reason = path_decision.reason_code or (str(reason_code or "").strip() or None)
    disposition = admission_disposition or _disposition_for_state(effective_state)
    if disposition not in {"requested", "admitted", "deferred", "not_admitted"}:
        raise ValueError(f"unsupported admission disposition: {disposition!r}")

    existing = (
        await db.execute(
            select(RuntimeRootItem)
            .where(
                RuntimeRootItem.tenant_id == tenant_uuid,
                RuntimeRootItem.root_runtime_task_id == root_id,
                RuntimeRootItem.intent_key == key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.work_type != kind or existing.target_ref != str(target_ref).strip():
            raise ValueError("runtime root intent key is already bound to different work")
        requested_runtime_task_id = _uuid(runtime_task_id, field="runtime_task_id") if runtime_task_id else None
        if (
            existing.runtime_task_id is not None
            and requested_runtime_task_id is not None
            and existing.runtime_task_id != requested_runtime_task_id
        ):
            raise ValueError("runtime root intent is already bound to a different RuntimeTask")
        if metadata:
            existing.metadata_json = {**dict(existing.metadata_json or {}), **metadata}
            existing.version = int(existing.version or 0) + 1
            await db.flush()
        return existing

    item = RuntimeRootItem(
        tenant_id=tenant_uuid,
        root_runtime_task_id=root_id,
        parent_runtime_task_id=_uuid(parent_runtime_task_id, field="parent_runtime_task_id")
        if parent_runtime_task_id
        else None,
        runtime_task_id=_uuid(runtime_task_id, field="runtime_task_id") if runtime_task_id else None,
        source_agent_id=source_uuid,
        root_user_id=_uuid(root_user_id, field="root_user_id") if root_user_id else None,
        root_session_id=str(root_session_id or "").strip() or None,
        intent_key=key,
        work_type=kind,
        target_ref=str(target_ref).strip(),
        path_json=list(path_decision.path),
        state=effective_state,
        admission_disposition=disposition,
        reason_code=effective_reason,
        budget_reservation_key=str(budget_reservation_key or "").strip() or None,
        approval_ref=str(approval_ref or "").strip() or None,
        child_session_id=str(child_session_id or "").strip() or None,
        metadata_json=dict(metadata or {}),
        terminal_at=datetime.now(timezone.utc) if effective_state in RUNTIME_ROOT_TERMINAL_STATES else None,
    )
    db.add(item)
    await db.flush()
    return item


async def register_runtime_task_root_item(
    db: Any,
    *,
    task: Any,
    intent: RuntimeRootIntentSpec,
) -> RuntimeRootItem:
    """Bind an already-staged RuntimeTask and its root item in one transaction."""

    runtime_task_id = _uuid(getattr(task, "id", None), field="runtime_task_id")
    root_runtime_task_id = getattr(task, "root_runtime_task_id", None) or runtime_task_id
    parent_runtime_task_id = root_runtime_task_id if root_runtime_task_id != runtime_task_id else None
    item = await register_runtime_root_item(
        db,
        tenant_id=getattr(task, "tenant_id", None),
        root_runtime_task_id=root_runtime_task_id,
        parent_runtime_task_id=parent_runtime_task_id,
        runtime_task_id=runtime_task_id,
        source_agent_id=getattr(task, "parent_agent_id", None),
        root_user_id=getattr(task, "root_user_id", None),
        root_session_id=getattr(task, "root_session_id", None),
        intent_key=intent.intent_key,
        work_type=intent.work_type,
        target_ref=intent.target_ref,
        path=intent.path,
        state=intent.state,
        admission_disposition=intent.admission_disposition,
        reason_code=intent.reason_code,
        budget_reservation_key=intent.budget_reservation_key or getattr(task, "budget_reservation_key", None),
        approval_ref=intent.approval_ref,
        child_session_id=getattr(task, "child_session_id", None),
        metadata={
            "runtime_task_type": str(getattr(task, "task_type", "") or ""),
            **dict(intent.metadata or {}),
        },
    )
    if item.runtime_task_id is not None and item.runtime_task_id != runtime_task_id:
        raise ValueError("runtime root intent is already bound to a different RuntimeTask")
    # Team fan-out first commits the complete requested set with no child task.
    # Binding an admitted/waiting child later must update that same durable row,
    # never create a second expected item or leave the placeholder detached.
    if item.runtime_task_id != runtime_task_id or item.state != intent.state:
        rebound, _ = await transition_runtime_root_item(
            db,
            root_runtime_task_id=root_runtime_task_id,
            intent_key=intent.intent_key,
            requested_state=intent.state,
            runtime_task_id=runtime_task_id,
            child_session_id=getattr(task, "child_session_id", None),
            reason_code=intent.reason_code,
            approval_ref=intent.approval_ref,
            metadata={
                "runtime_task_type": str(getattr(task, "task_type", "") or ""),
                **dict(intent.metadata or {}),
            },
        )
        if rebound is not None:
            item = rebound
    return item


async def transition_runtime_root_item(
    db: Any,
    *,
    root_runtime_task_id: uuid.UUID | str,
    intent_key: str,
    requested_state: str,
    runtime_task_id: uuid.UUID | str | None = None,
    child_session_id: str | None = None,
    reason_code: str | None = None,
    approval_ref: str | None = None,
    result_refs: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[RuntimeRootItem | None, RuntimeRootTransitionDecision]:
    root_id = _uuid(root_runtime_task_id, field="root_runtime_task_id")
    item = (
        await db.execute(
            select(RuntimeRootItem)
            .where(
                RuntimeRootItem.root_runtime_task_id == root_id,
                RuntimeRootItem.intent_key == str(intent_key),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        return None, RuntimeRootTransitionDecision(False, requested_state, "root_item_not_found")
    requested_runtime_task_id = _uuid(runtime_task_id, field="runtime_task_id") if runtime_task_id else None
    if (
        item.runtime_task_id is not None
        and requested_runtime_task_id is not None
        and item.runtime_task_id != requested_runtime_task_id
    ):
        raise ValueError("runtime root intent is already bound to a different RuntimeTask")
    decision = evaluate_runtime_root_transition(current_state=item.state, requested_state=requested_state)
    if not decision.applied:
        if decision.reason_code == "terminal_state_already_sealed" and requested_state != item.state:
            item.metadata_json = {
                **dict(item.metadata_json or {}),
                "late_terminal_attempt": {
                    "requested_state": requested_state,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            item.version = int(item.version or 0) + 1
            await db.flush()
        elif decision.reason_code == "state_unchanged":
            changed = False
            if requested_runtime_task_id is not None:
                item.runtime_task_id = requested_runtime_task_id
                changed = True
            if child_session_id is not None:
                item.child_session_id = str(child_session_id).strip() or None
                changed = True
            if reason_code is not None:
                item.reason_code = str(reason_code).strip() or None
                changed = True
            if approval_ref is not None:
                item.approval_ref = str(approval_ref).strip() or None
                changed = True
            if result_refs is not None:
                item.result_refs_json = list(
                    dict.fromkeys([*list(item.result_refs_json or []), *map(str, result_refs)])
                )
                changed = True
            if metadata:
                item.metadata_json = {**dict(item.metadata_json or {}), **metadata}
                changed = True
            if changed:
                if item.state == "requested":
                    item.admission_disposition = "deferred"
                item.version = int(item.version or 0) + 1
                await db.flush()
        return item, decision
    item.state = decision.effective_state
    item.admission_disposition = _disposition_for_state(item.state, item.admission_disposition)
    if item.state != "requested":
        item.recovery_claimed_by = None
        item.recovery_claim_expires_at = None
        item.next_recovery_at = None
    if requested_runtime_task_id is not None:
        item.runtime_task_id = requested_runtime_task_id
    if child_session_id is not None:
        item.child_session_id = str(child_session_id).strip() or None
    if reason_code is not None:
        item.reason_code = str(reason_code).strip() or None
    if approval_ref is not None:
        item.approval_ref = str(approval_ref).strip() or None
    if result_refs is not None:
        item.result_refs_json = list(dict.fromkeys([*list(item.result_refs_json or []), *map(str, result_refs)]))
    if metadata:
        item.metadata_json = {**dict(item.metadata_json or {}), **metadata}
    item.version = int(item.version or 0) + 1
    if item.state in RUNTIME_ROOT_TERMINAL_STATES:
        item.terminal_at = datetime.now(timezone.utc)
    await db.flush()
    return item, decision


async def transition_runtime_root_item_by_task(
    db: Any,
    *,
    runtime_task_id: uuid.UUID | str,
    requested_state: str,
    reason_code: str | None = None,
    approval_ref: str | None = None,
    result_refs: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[RuntimeRootItem | None, RuntimeRootTransitionDecision]:
    task_uuid = _uuid(runtime_task_id, field="runtime_task_id")
    item = (
        await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task_uuid).with_for_update())
    ).scalar_one_or_none()
    if item is None:
        return None, RuntimeRootTransitionDecision(False, requested_state, "root_item_not_found")
    return await transition_runtime_root_item(
        db,
        root_runtime_task_id=item.root_runtime_task_id,
        intent_key=item.intent_key,
        requested_state=requested_state,
        runtime_task_id=task_uuid,
        reason_code=reason_code,
        approval_ref=approval_ref,
        result_refs=result_refs,
        metadata=metadata,
    )


async def read_runtime_root_coverage(
    db: Any,
    *,
    root_runtime_task_id: uuid.UUID | str,
) -> RuntimeRootCoverage:
    root_id = _uuid(root_runtime_task_id, field="root_runtime_task_id")
    rows = list(
        (
            await db.execute(
                select(RuntimeRootItem)
                .where(RuntimeRootItem.root_runtime_task_id == root_id)
                .order_by(RuntimeRootItem.created_at, RuntimeRootItem.id)
            )
        )
        .scalars()
        .all()
    )
    return summarize_runtime_root_items(rows)
