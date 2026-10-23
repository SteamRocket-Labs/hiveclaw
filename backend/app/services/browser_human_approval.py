"""Single-use human browser effects on the existing enterprise approval ledger."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.services.approval_service import approval_service, can_resolve_approval
from app.services.approval_ticket import build_live_approval_policy_snapshot, hash_approval_execution_envelope, hash_policy_snapshot
from app.services.browser_control import BrowserControlError, _ARGUMENTS
from app.services.channel_secret_storage import decrypt_channel_secret, encrypt_channel_secret
from app.services.execution_environments import lock_environment
from app.services.environment_worker_witness import capture_worker_witness, observe_worker
from app.services.runtime_user_identity import load_scoped_runtime_user
from app.tools.guard_policy import evaluate_guard_policy

SCHEMA = "hive.browser.human_approval.v1"


class HumanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_tag: Literal[SCHEMA] = Field(alias="schema")
    tenant_id: UUID
    agent_id: UUID
    requester_user_id: UUID
    session_id: UUID
    request_id: UUID
    action: str
    expected_revision: int = Field(ge=0)
    expected_epoch: int = Field(ge=0)
    resource_id: UUID | None
    generation: int | None
    arguments_ciphertext: str
    tool_name: Literal["browser_observe", "browser_manage", "browser_action"]


def is_human_approval(ticket):
    return isinstance(ticket.execution_envelope, dict) and ticket.execution_envelope.get("schema") == SCHEMA


def envelope(ticket):
    try:
        data = HumanEnvelope.model_validate(ticket.execution_envelope)
        if (ticket.tool_name is not None or ticket.execution_envelope_hash != hash_approval_execution_envelope(
            ticket.execution_envelope) or ticket.input_hash != ticket.execution_envelope_hash
            or ticket.policy_snapshot_hash != hash_policy_snapshot(ticket.policy_snapshot or {})
            or ticket.tenant_id != data.tenant_id or ticket.agent_id != data.agent_id
            or ticket.requested_by != data.requester_user_id):
            raise ValueError("binding mismatch")
        return data
    except (TypeError, ValueError, ValidationError) as exc:
        raise BrowserControlError("browser_approval_binding_changed", 403) from exc


def tool_name(action):
    if action == "observe":
        return "browser_observe"
    if action in {"navigate", "click", "scroll", "type", "key", "select_page"}:
        return "browser_action"
    return "browser_manage" if action in {"start", "request_control", "take_control"} else None


def arguments_for(action, arguments):
    try:
        return _ARGUMENTS[action].model_validate(arguments).model_dump()
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise BrowserControlError("browser_arguments_invalid", 422) from exc


async def authorize(db, source, ticket):
    data = envelope(ticket)
    approver = await load_scoped_runtime_user(db, ticket.resolved_by, tenant_id=source.tenant_id)
    agent = await db.get(Agent, source.agent_id, populate_existing=True)
    if (data.tenant_id != source.tenant_id or data.agent_id != source.agent_id
        or data.requester_user_id != source.user_id or data.session_id != source.session_id
        or data.tool_name != source.tool_name or ticket.policy_snapshot_hash != source.policy_snapshot_hash
        or ticket.status != "approved" or ticket.execution_status != "executing" or ticket.consumed_at is None
        or ticket.expires_at is None or ticket.expires_at <= datetime.now(UTC)
        or approver is None or agent is None
        or not await can_resolve_approval(db, agent=agent, user=approver, approval=ticket)):
        raise BrowserControlError("browser_approval_revoked", 403)


def validate_effect(ticket, source, action, request_id, revision, epoch, arguments, row, actor_kind):
    data = envelope(ticket)
    if (actor_kind != "human" or data.request_id != UUID(str(request_id)) or data.action != action
        or data.expected_revision != revision or data.expected_epoch != epoch
        or data.resource_id != (row.id if row else None) or data.generation != (row.generation if row else None)
        or row is not None and (row.revision != revision or row.control_epoch != epoch)
        or data.requester_user_id != source.user_id or data.session_id != source.session_id
        or data.tenant_id != source.tenant_id or data.agent_id != source.agent_id):
        raise BrowserControlError("browser_approval_input_changed")
    try:
        approved = arguments_for(data.action, json.loads(decrypt_channel_secret(data.arguments_ciphertext)))
    except Exception as exc:
        raise BrowserControlError("browser_approval_input_unavailable", 503) from exc
    if approved != arguments_for(action, arguments):
        raise BrowserControlError("browser_approval_input_changed")


class BrowserApprovalRequired(BrowserControlError):
    def __init__(self, approval_id):
        super().__init__("browser_approval_required", 409)
        self.approval_id = approval_id


async def admit(source, manager, action, body):
    name = tool_name(action)
    if name is None:
        return source  # Safe release, disconnect and closure do not enlarge authority.
    pending_id = None
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_human_effect_policy") as db:
        await source.authorize(db)
        if body.approval_id is not None and body.arguments is None:
            previous = await db.get(ApprovalRequest, body.approval_id)
            if previous is None:
                raise BrowserControlError("browser_approval_unavailable", 404)
            bound = envelope(previous)
            if (bound.requester_user_id != source.user_id or bound.session_id != source.session_id
                or bound.tenant_id != source.tenant_id or bound.agent_id != source.agent_id):
                raise BrowserControlError("browser_approval_unavailable", 404)
            try:
                body.arguments = json.loads(decrypt_channel_secret(bound.arguments_ciphertext))
            except Exception as exc:
                raise BrowserControlError("browser_approval_input_unavailable", 503) from exc
        arguments = arguments_for(action, body.arguments or {})
        snapshot = await build_live_approval_policy_snapshot(
            db=db, tenant_id=source.tenant_id, agent_id=source.agent_id, tool_name=name)
        capability = snapshot["capability"]
        verdict = evaluate_guard_policy(tool_name=name, arguments={"action": action, "arguments": arguments,
            "expected_revision": body.expected_revision, "expected_epoch": body.expected_epoch},
            external_visible=name == "browser_action", snapshot=snapshot["guard_policy"])
        if capability["denied"] or verdict.decision == "deny":
            raise BrowserControlError("browser_capability_revoked", 403)
        source = replace(source, tool_name=name, policy_snapshot_hash=hash_policy_snapshot(snapshot))
        needs_approval = (capability["policy_found"] and capability["escalate_to_l3"]) or verdict.decision == "require_approval"
        if not needs_approval and body.approval_id is None:
            return source
        await lock_environment(db, source.tenant_id, source.agent_id)
        row = await manager._row(db, source, lock=True)
        key = f"browser-human:{source.tenant_id}:{source.session_id}:{source.user_id}:{body.request_id}"
        ticket = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == body.approval_id).with_for_update()) if body.approval_id else await db.scalar(
            select(ApprovalRequest).where(ApprovalRequest.execution_idempotency_key == key).with_for_update())
        if ticket is not None:
            data = envelope(ticket)
            if (ticket.tenant_id != source.tenant_id or ticket.agent_id != source.agent_id
                or data.tool_name != name or ticket.policy_snapshot_hash != source.policy_snapshot_hash):
                raise BrowserControlError("browser_approval_binding_changed", 403)
            validate_effect(ticket, source, action, body.request_id, body.expected_revision, body.expected_epoch,
                arguments, row, "human")
        elif body.approval_id is not None:
            raise BrowserControlError("browser_approval_unavailable", 404)
        else:
            data = HumanEnvelope.model_validate({"schema": SCHEMA, "tenant_id": source.tenant_id,
                "agent_id": source.agent_id, "requester_user_id": source.user_id, "session_id": source.session_id,
                "request_id": body.request_id, "action": action, "expected_revision": body.expected_revision,
                "expected_epoch": body.expected_epoch, "resource_id": row.id if row else None,
                "generation": row.generation if row else None, "tool_name": name,
                "arguments_ciphertext": encrypt_channel_secret(json.dumps(arguments, ensure_ascii=False))})
            raw = data.model_dump(mode="json", by_alias=True)
            created = await approval_service.request_approval(db, await db.get(Agent, source.agent_id),
                action_type=snapshot["capability"]["name"] or "computer.browser.control", details={
                    "requested_by": str(source.user_id), "session_id": str(source.session_id),
                    "reason": "Company policy requires approval for this exact human browser operation.",
                    "summary": f"Manual browser operation: {action}", "origin": {"type": "browser_human"},
                    "policy_snapshot": snapshot, "execution_envelope": raw})
            ticket = await db.get(ApprovalRequest, UUID(created["approval_id"]))
            ticket.execution_envelope_hash = ticket.input_hash = hash_approval_execution_envelope(raw)
            ticket.execution_idempotency_key = key
        if body.approval_id is None:
            pending_id = ticket.id
        else:
            if ticket.consumed_at is not None:
                raise BrowserControlError("browser_approval_consumed")
            if ticket.status != "approved" or ticket.execution_status != "approved":
                raise BrowserControlError("browser_approval_not_approved", 403)
            if ticket.expires_at is None or ticket.expires_at <= datetime.now(UTC):
                raise BrowserControlError("browser_approval_expired", 403)
            ticket.consumed_at = datetime.now(UTC)
            ticket.execution_status = "executing"
            ticket.execution_receipt = {"status": "executing", "automatic_replay": False,
                "owner_witness": capture_worker_witness(), "origin_session_id": str(source.session_id)}
            await authorize(db, source, ticket)
            source = replace(source, approval_id=ticket.id)
    if pending_id is not None:
        raise BrowserApprovalRequired(pending_id)
    return source


async def check_effect(db, source, action, request_id, revision, epoch, arguments, row, actor_kind):
    if source.approval_id is None:
        return
    ticket = await db.get(ApprovalRequest, source.approval_id, populate_existing=True)
    if ticket is not None and is_human_approval(ticket):
        await authorize(db, source, ticket)
        validate_effect(ticket, source, action, request_id, revision, epoch, arguments, row, actor_kind)


async def complete(source, *, effect, code=None):
    if source.approval_id is None:
        return
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_human_approval_result") as db:
        ticket = await db.get(ApprovalRequest, source.approval_id, with_for_update=True)
        if ticket is None or not is_human_approval(ticket) or ticket.execution_status != "executing":
            return
        ticket.execution_status = "needs_reconciliation" if effect == "unknown" else "failed" if code else "succeeded"
        ticket.execution_result = code or "Browser operation acknowledged; business outcome unverified."
        ticket.execution_receipt = {**dict(ticket.execution_receipt or {}), "status": ticket.execution_status, "effect": effect,
            "automatic_replay": False, "business_outcome": "unverified", "origin_session_id": str(source.session_id)}


async def references(source):
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_human_approval_references") as db:
        await source.authorize(db)
        rows = list(await db.scalars(select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == source.tenant_id, ApprovalRequest.agent_id == source.agent_id,
            ApprovalRequest.requested_by == source.user_id,
            ApprovalRequest.execution_envelope["schema"].as_string() == SCHEMA,
            ApprovalRequest.execution_envelope["session_id"].as_string() == str(source.session_id),
        ).order_by(ApprovalRequest.created_at.desc()).limit(21)))
        values = []
        for row in rows[:20]:
            if row.execution_status == "executing" and row.consumed_at is not None:
                row = await db.get(ApprovalRequest, row.id, with_for_update=True, populate_existing=True)
                if row.execution_status == "executing" and row.consumed_at is not None:
                    receipt = dict(row.execution_receipt or {})
                    reason, absent = observe_worker(receipt.get("owner_witness"))
                    expired = row.expires_at is not None and row.expires_at <= datetime.now(UTC)
                    if absent or expired:
                        row.execution_status = "needs_reconciliation"
                        row.execution_result = "The consumed browser operation has no confirmed terminal receipt. Inspect its outcome before another operation."
                        row.execution_receipt = {**receipt, "status": "needs_reconciliation",
                            "effect": "unknown", "side_effect_state": "unknown", "automatic_replay": False,
                            "reason_code": reason if absent else "browser_approval_expired",
                            "reconciled_at": datetime.now(UTC).isoformat()}
            item = {"id": str(row.id), "status": row.status, "execution_status": row.execution_status}
            try:
                data = envelope(row)
                item.update({"action": data.action, "request_id": str(data.request_id),
                    "expected_revision": data.expected_revision, "expected_epoch": data.expected_epoch})
            except BrowserControlError:
                item["unavailable"] = True
            values.append(item)
        return {"items": values, "has_more": len(rows) > 20}
