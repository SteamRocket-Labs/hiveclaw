"""Single-use approval tickets bound to principals, input, and policy snapshots."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ApprovalTicketError(RuntimeError):
    """Raised when an approval cannot authorize exactly one execution."""


@dataclass(frozen=True, slots=True)
class ApprovalExecutionTicket:
    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    requested_by_user_id: uuid.UUID
    approved_by_user_id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    input_hash: str
    policy_snapshot_hash: str
    idempotency_key: str
    decision_id: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def normalize_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parsed = None
        if isinstance(parsed, dict):
            return dict(parsed)
    raise ApprovalTicketError("approval tool arguments must be an object")


def hash_tool_input(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = {"tool_name": str(tool_name).strip(), "arguments": dict(arguments)}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_policy_snapshot(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(snapshot)).encode("utf-8")).hexdigest()


async def build_live_approval_policy_snapshot(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_name: str,
) -> dict[str, Any]:
    """Read the exact live policy inputs whose drift invalidates a ticket."""
    from app.models.guard_policy import GuardPolicy
    from app.services.capability_gate import check_capability

    capability = await check_capability(db, tenant_id, agent_id, tool_name)
    guard_result = await db.execute(select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id))
    guard = guard_result.scalar_one_or_none()
    return {
        "schema": "hive.approval_policy_snapshot.v1",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "tool_name": str(tool_name),
        "capability": {
            "allowed": bool(getattr(capability, "allowed", False)),
            "denied": bool(getattr(capability, "denied", False)),
            "escalate_to_l3": bool(getattr(capability, "escalate_to_l3", False)),
            "name": str(getattr(capability, "capability", "") or ""),
            "reason": str(getattr(capability, "reason", "") or ""),
            "policy_found": bool(getattr(capability, "policy_found", False)),
        },
        "guard_policy": {
            "version": int(getattr(guard, "version", 0) or 0),
            "zone_guard": dict(getattr(guard, "zone_guard", {}) or {}),
            "egress_guard": dict(getattr(guard, "egress_guard", {}) or {}),
        },
    }


async def consume_approval_ticket(
    *,
    approval_id: uuid.UUID,
    expected_agent_id: uuid.UUID | None,
    expected_user_id: uuid.UUID | None,
) -> ApprovalExecutionTicket:
    """Claim one approved ticket through locator -> tenant-scoped execution."""
    from app.database import async_session, enter_rls_bypass, tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with async_session() as db:
        async with enter_rls_bypass(db, reason="approval ticket tenant locator") as locator_db:
            located = (
                await locator_db.execute(
                    select(ApprovalRequest.tenant_id, ApprovalRequest.agent_id).where(ApprovalRequest.id == approval_id)
                )
            ).one_or_none()
        await db.rollback()
    if located is None:
        raise ApprovalTicketError("approval ticket not found")
    tenant_id, agent_id = located
    if tenant_id is None:
        raise ApprovalTicketError("approval ticket has no tenant")
    if expected_agent_id is not None and agent_id != expected_agent_id:
        raise ApprovalTicketError("approval ticket agent mismatch")

    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update())
        approval = result.scalar_one_or_none()
        if approval is None:
            raise ApprovalTicketError("approval ticket not visible in tenant")
        if expected_user_id is not None and approval.resolved_by != expected_user_id:
            raise ApprovalTicketError("approval ticket approver mismatch")
        if approval.status != "approved":
            raise ApprovalTicketError(f"approval ticket is not approved: {approval.status}")
        now = datetime.now(timezone.utc)
        if approval.expires_at is None or approval.expires_at <= now:
            raise ApprovalTicketError("approval ticket expired")
        if approval.consumed_at is not None:
            raise ApprovalTicketError("approval ticket already consumed")
        tool_name = str(approval.tool_name or "").strip()
        if not tool_name:
            raise ApprovalTicketError("approval ticket has no tool")
        arguments = normalize_tool_arguments(approval.normalized_arguments)
        input_hash = hash_tool_input(tool_name, arguments)
        if not approval.input_hash or input_hash != approval.input_hash:
            raise ApprovalTicketError("approval ticket input hash mismatch")
        policy_snapshot = dict(approval.policy_snapshot or {})
        policy_snapshot_hash = hash_policy_snapshot(policy_snapshot)
        if not approval.policy_snapshot_hash or policy_snapshot_hash != approval.policy_snapshot_hash:
            raise ApprovalTicketError("approval ticket policy snapshot mismatch")
        live_policy_snapshot = await build_live_approval_policy_snapshot(
            db=db,
            tenant_id=tenant_id,
            agent_id=approval.agent_id,
            tool_name=tool_name,
        )
        if hash_policy_snapshot(live_policy_snapshot) != approval.policy_snapshot_hash:
            raise ApprovalTicketError("approval ticket policy changed after approval request")
        if approval.requested_by is None or approval.resolved_by is None:
            raise ApprovalTicketError("approval ticket principal binding is incomplete")
        if not str(approval.decision_id or "").strip():
            raise ApprovalTicketError("approval ticket decision binding is incomplete")

        approval.consumed_at = now
        approval.execution_status = "executing"
        idempotency_key = str(approval.execution_idempotency_key or f"approval:{approval.id}")
        approval.execution_idempotency_key = idempotency_key
        await db.flush()
        return ApprovalExecutionTicket(
            approval_id=approval.id,
            tenant_id=tenant_id,
            agent_id=approval.agent_id,
            requested_by_user_id=approval.requested_by,
            approved_by_user_id=approval.resolved_by,
            tool_name=tool_name,
            arguments=arguments,
            input_hash=input_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            idempotency_key=idempotency_key,
            decision_id=str(approval.decision_id),
        )


async def complete_approval_ticket(
    *,
    approval_id: uuid.UUID,
    tenant_id: uuid.UUID,
    status: str,
    result: str,
    receipt: dict[str, Any],
) -> None:
    from app.database import tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with tenant_scoped_session(tenant_id) as db:
        row = await db.get(ApprovalRequest, approval_id, with_for_update=True)
        if row is None or row.consumed_at is None:
            raise ApprovalTicketError("approval ticket completion has no consumed ticket")
        row.execution_status = str(status)
        row.execution_result = str(result)[:20_000]
        row.execution_receipt = dict(receipt)
        await db.flush()


async def reconcile_stuck_approval_tickets(
    *,
    older_than: datetime,
    limit: int = 100,
) -> int:
    """Quarantine crash-window tickets without replaying unknown side effects."""
    from collections import defaultdict

    from app.database import async_session, enter_rls_bypass, tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with async_session() as db:
        async with enter_rls_bypass(
            db,
            reason="approval ticket execution reconciliation locator",
        ) as locator_db:
            rows = (
                await locator_db.execute(
                    select(ApprovalRequest.id, ApprovalRequest.tenant_id)
                    .where(
                        ApprovalRequest.execution_status == "executing",
                        ApprovalRequest.consumed_at < older_than,
                        ApprovalRequest.tenant_id.is_not(None),
                    )
                    .order_by(ApprovalRequest.consumed_at.asc())
                    .limit(max(1, min(int(limit), 1000)))
                )
            ).all()
        await db.rollback()

    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for approval_id, tenant_id in rows:
        if tenant_id is not None:
            by_tenant[tenant_id].append(approval_id)

    reconciled = 0
    for tenant_id, approval_ids in by_tenant.items():
        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.id.in_(approval_ids),
                    ApprovalRequest.execution_status == "executing",
                    ApprovalRequest.consumed_at < older_than,
                )
                .with_for_update()
            )
            for row in result.scalars().all():
                receipt = dict(row.execution_receipt or {})
                receipt.update(
                    {
                        "status": "needs_reconciliation",
                        "side_effect_state": "unknown",
                        "automatic_replay": False,
                        "reconciled_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                row.execution_status = "needs_reconciliation"
                row.execution_receipt = receipt
                reconciled += 1
            await db.flush()
    return reconciled
