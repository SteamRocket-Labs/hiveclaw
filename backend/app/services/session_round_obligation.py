"""Durable, coexisting Session V2 round obligations and assembly plans.

This module owns only mechanical continuation facts.  It never ranks semantic
importance or infers intent from natural-language content.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import (
    SessionModelResult,
    SessionNextRoundPlan,
    SessionRoundObligation,
    SessionTurnInput,
)


OBLIGATION_KINDS = frozenset({"tool_followup", "pending_input", "hook_retry", "compact_continue"})
UNRESOLVED_STATES = ("pending", "claimed", "needs_reconciliation")
_SOURCE_ORDER = {"tool_followup": 0, "compact_continue": 1, "hook_retry": 2, "pending_input": 3}


class AssemblyPlanDrift(RuntimeError):
    """A committed plan no longer matches authoritative generations."""


class AssemblyPlanNeedsReconciliation(RuntimeError):
    """Provider dispatch may have consumed a plan and cannot be guessed."""


@dataclass(frozen=True, slots=True)
class ObligationSpec:
    kind: str
    source_generation: int
    source_ref: str
    payload: Mapping[str, Any]


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    raw = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_obligation_id(result_id: uuid.UUID, spec: ObligationSpec) -> uuid.UUID:
    return uuid.uuid5(
        result_id,
        f"round-obligation:{spec.kind}:{spec.source_generation}:{spec.source_ref}",
    )


def _plan_id(run_id: uuid.UUID, next_round_id: str, generation: int, plan_hash: str) -> uuid.UUID:
    return uuid.uuid5(run_id, f"next-round-plan:{next_round_id}:{generation}:{plan_hash}")


def _normalise_spec(value: ObligationSpec | Mapping[str, Any], *, default_generation: int) -> ObligationSpec:
    if isinstance(value, ObligationSpec):
        spec = value
    else:
        payload = dict(value.get("payload") or value)
        spec = ObligationSpec(
            kind=str(value.get("kind") or ""),
            source_generation=int(value.get("source_generation") or default_generation),
            source_ref=str(value.get("source_ref") or payload.get("source_ref") or ""),
            payload=payload,
        )
    if spec.kind not in OBLIGATION_KINDS:
        raise ValueError(f"unsupported round obligation kind: {spec.kind}")
    if spec.source_generation < 1 or not spec.source_ref:
        raise ValueError("round obligation requires positive source_generation and source_ref")
    return ObligationSpec(
        kind=spec.kind,
        source_generation=int(spec.source_generation),
        source_ref=spec.source_ref,
        payload=_canonical(dict(spec.payload)),
    )


async def discover_round_obligations(
    db: AsyncSession,
    *,
    result: SessionModelResult,
    response: Mapping[str, Any],
    explicit: Iterable[ObligationSpec | Mapping[str, Any]] = (),
) -> list[ObligationSpec]:
    """Discover all mechanical obligations without collapsing them to one action."""

    specs = [_normalise_spec(item, default_generation=max(1, int(result.version))) for item in explicit]
    tool_calls = list(response.get("tool_calls") or [])
    if tool_calls:
        invocation_ids: list[str] = []
        provider_tool_use_ids: list[str] = []
        for index, call in enumerate(tool_calls):
            provider_id = (
                str((call or {}).get("id") or f"index:{index}") if isinstance(call, dict) else f"index:{index}"
            )
            provider_tool_use_ids.append(provider_id)
            invocation_ids.append(str(uuid.uuid5(result.id, f"runtime-tool-invocation:{provider_id}")))
        specs.append(
            ObligationSpec(
                kind="tool_followup",
                source_generation=max(1, int(result.version)),
                source_ref=f"provider-tools:{result.provider_request_id}",
                payload={
                    "invocation_ids": invocation_ids,
                    "provider_tool_use_ids": provider_tool_use_ids,
                    "tool_pair_fence_ref": f"tool-pair:{result.id}:{_sha256(provider_tool_use_ids)}",
                },
            )
        )

    pending_inputs = list(
        (
            await db.execute(
                select(SessionTurnInput)
                .where(
                    SessionTurnInput.tenant_id == result.tenant_id,
                    SessionTurnInput.session_id == result.session_id,
                    SessionTurnInput.target_run_id == result.run_id,
                    SessionTurnInput.status == "queued",
                    SessionTurnInput.queue_priority.in_(("now", "next")),
                )
                .order_by(SessionTurnInput.queue_priority, SessionTurnInput.queue_ordinal)
            )
        ).scalars()
    )
    for row in pending_inputs:
        specs.append(
            ObligationSpec(
                kind="pending_input",
                source_generation=max(1, int(row.version)),
                source_ref=f"session-input:{row.id}",
                payload={
                    "input_ids": [str(row.id)],
                    "mailbox_generation": int(row.version),
                    "max_queue_priority": row.queue_priority,
                    "queue_ordinal": int(row.queue_ordinal),
                },
            )
        )

    deduplicated: dict[tuple[str, int, str], ObligationSpec] = {}
    for raw in specs:
        spec = _normalise_spec(raw, default_generation=max(1, int(result.version)))
        key = (spec.kind, spec.source_generation, spec.source_ref)
        previous = deduplicated.get(key)
        if previous is not None and _sha256(previous.payload) != _sha256(spec.payload):
            raise AssemblyPlanNeedsReconciliation("same obligation identity has conflicting payload")
        deduplicated[key] = spec
    return [deduplicated[key] for key in sorted(deduplicated)]


async def persist_round_obligations(
    db: AsyncSession,
    *,
    result: SessionModelResult,
    specs: Sequence[ObligationSpec | Mapping[str, Any]],
) -> list[SessionRoundObligation]:
    """Read-or-create every obligation using its stable source identity."""

    rows: list[SessionRoundObligation] = []
    for raw in specs:
        spec = _normalise_spec(raw, default_generation=max(1, int(result.version)))
        row_id = _stable_obligation_id(result.id, spec)
        row = await db.scalar(
            select(SessionRoundObligation)
            .where(
                SessionRoundObligation.source_result_id == result.id,
                SessionRoundObligation.kind == spec.kind,
                SessionRoundObligation.source_generation == spec.source_generation,
                SessionRoundObligation.source_ref == spec.source_ref,
            )
            .with_for_update()
        )
        if row is None:
            row = SessionRoundObligation(
                id=row_id,
                tenant_id=result.tenant_id,
                session_id=result.session_id,
                turn_id=result.turn_id,
                run_id=result.run_id,
                source_result_id=result.id,
                kind=spec.kind,
                source_generation=spec.source_generation,
                source_ref=spec.source_ref,
                payload_json=dict(spec.payload),
                state="pending",
                version=1,
            )
            db.add(row)
            await db.flush()
        elif _sha256(row.payload_json or {}) != _sha256(spec.payload):
            row.state = "needs_reconciliation"
            row.recovery_owner = "session_round_obligation:identity_payload_conflict"
            row.version = int(row.version) + 1
            raise AssemblyPlanNeedsReconciliation("same obligation identity has conflicting payload")
        rows.append(row)
    return rows


async def current_run_fences(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, int]:
    task = await db.scalar(
        select(RuntimeTask).where(RuntimeTask.id == run_id, RuntimeTask.tenant_id == tenant_id).with_for_update()
    )
    if task is None:
        raise ValueError("runtime task not found for round fences")
    metadata = dict(task.metadata_json or {})
    stored = dict(metadata.get("session_v2_generations") or {})
    mailbox_generation = int(
        await db.scalar(
            select(func.coalesce(func.max(SessionTurnInput.version), 0)).where(
                SessionTurnInput.tenant_id == tenant_id,
                SessionTurnInput.target_run_id == run_id,
            )
        )
        or 0
    )
    obligation_generations = dict(
        (
            await db.execute(
                select(
                    SessionRoundObligation.kind, func.coalesce(func.max(SessionRoundObligation.source_generation), 0)
                )
                .where(
                    SessionRoundObligation.tenant_id == tenant_id,
                    SessionRoundObligation.run_id == run_id,
                    SessionRoundObligation.state.in_(UNRESOLVED_STATES),
                )
                .group_by(SessionRoundObligation.kind)
            )
        ).all()
    )
    return {
        "run_frontier_generation": int(stored.get("run_frontier_generation") or task.claim_version or 0),
        "tool_pair_generation": int(
            max(int(stored.get("tool_pair_generation") or 0), int(obligation_generations.get("tool_followup") or 0))
        ),
        "input_mailbox_generation": int(max(int(stored.get("input_mailbox_generation") or 0), mailbox_generation)),
        "hook_generation": int(
            max(int(stored.get("hook_generation") or 0), int(obligation_generations.get("hook_retry") or 0))
        ),
        "compaction_generation": int(
            max(int(stored.get("compaction_generation") or 0), int(obligation_generations.get("compact_continue") or 0))
        ),
        "cancellation_generation": int(stored.get("cancellation_generation") or 0),
    }


async def unresolved_round_obligations(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    lock: bool = True,
) -> list[SessionRoundObligation]:
    statement = (
        select(SessionRoundObligation)
        .where(
            SessionRoundObligation.tenant_id == tenant_id,
            SessionRoundObligation.run_id == run_id,
            SessionRoundObligation.state.in_(UNRESOLVED_STATES),
        )
        .order_by(SessionRoundObligation.kind, SessionRoundObligation.id)
    )
    if lock:
        statement = statement.with_for_update()
    return list((await db.execute(statement)).scalars())


def _ordered_sources(obligations: Sequence[SessionRoundObligation]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for row in sorted(
        obligations,
        key=lambda value: (
            _SOURCE_ORDER[value.kind],
            int((value.payload_json or {}).get("queue_ordinal") or 0),
            str(value.id),
        ),
    ):
        role = {
            "tool_followup": "tool_result",
            "pending_input": "pending_input",
            "hook_retry": "hook_feedback",
            "compact_continue": "post_compact_context",
        }[row.kind]
        ordered.append({"role": role, "ref": row.source_ref, "obligation_id": str(row.id)})
    return ordered


async def commit_next_round_plan(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    source_result_id: uuid.UUID,
    next_round_id: str,
) -> SessionNextRoundPlan:
    """Commit an immutable complete plan, abandoning only undispatched drifted plans."""

    obligations = await unresolved_round_obligations(db, tenant_id=tenant_id, run_id=run_id)
    fences = await current_run_fences(db, tenant_id=tenant_id, run_id=run_id)
    obligation_ids = [str(row.id) for row in obligations]
    ordered_sources = _ordered_sources(obligations)
    plan_payload = {
        "source_result_id": str(source_result_id),
        "next_round_id": next_round_id,
        "obligation_ids": obligation_ids,
        "ordered_sources": ordered_sources,
        "fences": fences,
    }
    plan_hash = _sha256(plan_payload)
    existing = list(
        (
            await db.execute(
                select(SessionNextRoundPlan)
                .where(
                    SessionNextRoundPlan.tenant_id == tenant_id,
                    SessionNextRoundPlan.run_id == run_id,
                    SessionNextRoundPlan.next_round_id == next_round_id,
                )
                .order_by(SessionNextRoundPlan.plan_generation.desc())
                .with_for_update()
            )
        ).scalars()
    )
    for row in existing:
        if row.plan_hash == plan_hash and row.state in {"committed", "dispatched", "needs_reconciliation"}:
            return row
    current = next(
        (row for row in existing if row.state in {"prepared", "committed", "dispatched", "needs_reconciliation"}), None
    )
    if current is not None:
        if current.state in {"dispatched", "needs_reconciliation"}:
            current.state = "needs_reconciliation"
            current.version = int(current.version) + 1
            raise AssemblyPlanNeedsReconciliation("dispatched next-round plan drifted")
        current.state = "abandoned"
        current.version = int(current.version) + 1
    generation = max((int(row.plan_generation) for row in existing), default=0) + 1
    plan = SessionNextRoundPlan(
        id=_plan_id(run_id, next_round_id, generation, plan_hash),
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        source_result_id=source_result_id,
        next_round_id=next_round_id,
        plan_generation=generation,
        obligation_ids_json=obligation_ids,
        ordered_sources_json=ordered_sources,
        fences_json=fences,
        plan_hash=plan_hash,
        state="committed",
        version=1,
    )
    db.add(plan)
    await db.flush()
    return plan


async def dispatch_committed_plan(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    claim_owner: str,
    lease_seconds: int = 300,
) -> SessionNextRoundPlan:
    plan = await db.scalar(select(SessionNextRoundPlan).where(SessionNextRoundPlan.id == plan_id).with_for_update())
    if plan is None:
        raise ValueError("next-round plan not found")
    if plan.state == "dispatched":
        return plan
    if plan.state != "committed":
        raise AssemblyPlanDrift(f"next-round plan is not committed: {plan.state}")
    current = await current_run_fences(db, tenant_id=plan.tenant_id, run_id=plan.run_id)
    if dict(plan.fences_json or {}) != current:
        plan.state = "abandoned"
        plan.version = int(plan.version) + 1
        raise AssemblyPlanDrift("next-round plan fences changed before dispatch")
    now = datetime.now(timezone.utc)
    lease = now + timedelta(seconds=max(1, int(lease_seconds)))
    rows = list(
        (
            await db.execute(
                select(SessionRoundObligation)
                .where(SessionRoundObligation.id.in_([uuid.UUID(value) for value in plan.obligation_ids_json or []]))
                .with_for_update()
            )
        ).scalars()
    )
    if len(rows) != len(plan.obligation_ids_json or []) or any(row.state != "pending" for row in rows):
        plan.state = "abandoned"
        plan.version = int(plan.version) + 1
        raise AssemblyPlanDrift("next-round obligations changed before dispatch")
    for row in rows:
        row.state = "claimed"
        row.claim_owner = claim_owner
        row.claim_lease_expires_at = lease
        row.version = int(row.version) + 1
    plan.state = "dispatched"
    plan.version = int(plan.version) + 1
    return plan


async def settle_dispatched_plan(
    db: AsyncSession,
    *,
    plan_id: uuid.UUID,
    provider_response_ref: str,
) -> list[SessionRoundObligation]:
    plan = await db.scalar(select(SessionNextRoundPlan).where(SessionNextRoundPlan.id == plan_id).with_for_update())
    if plan is None:
        raise ValueError("next-round plan not found")
    if plan.state == "needs_reconciliation":
        raise AssemblyPlanNeedsReconciliation("plan delivery is ambiguous")
    if plan.state != "dispatched":
        raise AssemblyPlanDrift(f"cannot settle plan in state {plan.state}")
    rows = list(
        (
            await db.execute(
                select(SessionRoundObligation)
                .where(SessionRoundObligation.id.in_([uuid.UUID(value) for value in plan.obligation_ids_json or []]))
                .with_for_update()
            )
        ).scalars()
    )
    for row in rows:
        if row.state == "settled":
            continue
        if row.state != "claimed":
            row.state = "needs_reconciliation"
            row.recovery_owner = "session_round_obligation:settlement_state_drift"
            row.version = int(row.version) + 1
            raise AssemblyPlanNeedsReconciliation("claimed obligation state drifted")
        row.state = "settled"
        row.settlement_ref = provider_response_ref
        row.claim_owner = None
        row.claim_lease_expires_at = None
        row.recovery_owner = None
        row.version = int(row.version) + 1
    return rows


async def recover_expired_obligations_once(
    db: AsyncSession,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Claim stale obligations conservatively from durable request/result evidence."""

    now = now or datetime.now(timezone.utc)
    stale = list(
        (
            await db.execute(
                select(SessionRoundObligation)
                .where(
                    SessionRoundObligation.state == "claimed",
                    SessionRoundObligation.claim_lease_expires_at <= now,
                )
                .order_by(SessionRoundObligation.claim_lease_expires_at)
                .limit(max(1, int(limit)))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    settled = reconciled = 0
    for row in stale:
        plan = await db.scalar(
            select(SessionNextRoundPlan).where(
                SessionNextRoundPlan.run_id == row.run_id,
                SessionNextRoundPlan.obligation_ids_json.contains([str(row.id)]),
            )
        )
        result = None
        if plan is not None:
            result = await db.scalar(
                select(SessionModelResult).where(
                    SessionModelResult.run_id == row.run_id,
                    SessionModelResult.model_request_snapshot_json["assembly_plan_id"].astext == str(plan.id),
                    SessionModelResult.state.in_(("sealed", "round_committed")),
                )
            )
        if result is not None and (result.seal_json or {}).get("provider_response_ref"):
            row.state = "settled"
            row.settlement_ref = str(result.seal_json["provider_response_ref"])
            row.claim_owner = None
            row.claim_lease_expires_at = None
            row.recovery_owner = worker_id
            settled += 1
        else:
            row.state = "needs_reconciliation"
            row.claim_owner = None
            row.claim_lease_expires_at = None
            row.recovery_owner = worker_id
            reconciled += 1
        row.version = int(row.version) + 1
    return {"settled": settled, "needs_reconciliation": reconciled}


__all__ = [
    "AssemblyPlanDrift",
    "AssemblyPlanNeedsReconciliation",
    "ObligationSpec",
    "commit_next_round_plan",
    "current_run_fences",
    "discover_round_obligations",
    "dispatch_committed_plan",
    "persist_round_obligations",
    "recover_expired_obligations_once",
    "settle_dispatched_plan",
    "unresolved_round_obligations",
]
