"""Recoverable explicit-memory changes; content activation follows both commits."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import uuid

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.memory.explicit_overlay import (
    ExplicitMemoryOverlayResult, _stage_explicit_overlay_status, _target_hint_for_category,
    load_explicit_overlay_entries, load_explicit_overlay_entries_by_ids,
    prepare_explicit_memory_entry, stage_explicit_memory_entry,
)
from app.models.organization_identity import ContentInputReceipt
from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
from app.services.explicit_memory_authority import (
    AUTHORITY_SCHEMA, _actor, _authorize_entry, _prepared_entry, _result, _writer_input,
    audience_for_session, authorize_memory_audience, latest_memory_receipt, memory_effect_actor, memory_ref, source_dependency,
)
from app.services.session_model_inputs import _unique_dependencies


class MemoryOperationError(ContentInputReceiptError):
    def __init__(self, code, message, operation_id, status_code=503):
        super().__init__(code, message, status_code)
        self.operation_id = str(operation_id)


async def _lock(db, tenant_id, agent_id, key):
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"explicit-memory:{tenant_id}:{agent_id}:{key}"})


def _owned(row, *, tenant_id, agent_id, actor_id):
    a = row.authority_json if row is not None else {}
    if (row is None or row.tenant_id != tenant_id or row.carrier_kind != "explicit_memory"
        or a.get("schema") != AUTHORITY_SCHEMA or a.get("agent_id") != str(agent_id)
        or a.get("tenant_id") != str(tenant_id) or a.get("owner_user_id") != str(actor_id)):
        raise ContentInputReceiptError("explicit_memory_operation_denied", "This memory operation is not owned by this requester.", 403)


def _outcome(row):
    a = row.authority_json
    return {"schema": "hive.explicit_memory_operation_result.v1", "operation_id": str(row.id),
        "status": a["state"], "kind": a.get("operation_kind"),
        "source_session_id": a.get("source_session_id"),
        "memory_id": a.get("metadata", {}).get("id") or a.get("target", {}).get("entry_id"),
        "target_memory_id": a.get("target", {}).get("entry_id")}


async def _reserve_target(db, request, row, *, root, actor, dependencies):
    a = deepcopy(row.authority_json)
    target_id = request.arguments.get("memory_id") or request.arguments.get("id")
    await _lock(db, row.tenant_id, request.context.agent_id, target_id)
    previous = await latest_memory_receipt(db, row.tenant_id, request.context.agent_id, target_id)
    if previous is None:
        raise ContentInputReceiptError("explicit_memory_source_unverified", "The target source needs reconciliation.", 403)
    await authorize_memory_audience(db, tenant_id=row.tenant_id, agent_id=request.context.agent_id,
        request=request, authority=previous.authority_json, mutation=True)
    privacy_withdrawal = a["operation_kind"] == "retire" and request.arguments.get("reason") == "privacy"
    allowed_states = {"active", "retired"} if privacy_withdrawal else {"active"}
    if previous.authority_json.get("state") not in allowed_states:
        raise ContentInputReceiptError("explicit_memory_target_changed", "The target is not active. Inspect its current operation before changing it.")
    entries = load_explicit_overlay_entries_by_ids(root, request.context.agent_id, [target_id])
    if a["operation_kind"] == "update":
        if not entries:
            raise ContentInputReceiptError("explicit_memory_target_missing", "The target content needs restoration before correction.")
        await _authorize_entry(db, tenant_id=row.tenant_id, agent_id=request.context.agent_id,
            request=request, entry=entries[0], receipt=previous)
        dependencies = _unique_dependencies([*dependencies, source_dependency(previous)])
    metadata = deepcopy(previous.authority_json["metadata"])
    reason = request.arguments.get("reason") or "user_requested"
    final_state = "revoked" if a["operation_kind"] == "retire" and reason == "privacy" else "retired"
    new_metadata = {**metadata, "status": "retired", "status_reason": reason,
        "accepted_blocks": "", "updated_at": datetime.now(timezone.utc).isoformat()}
    if a["operation_kind"] == "update":
        new_metadata["status_reason"] = f"superseded_by:{a['entry_id']}"
    pending_id = uuid.uuid5(row.id, "target-reservation")
    pending_authority = {**deepcopy(previous.authority_json), "state": "prepared",
        "operation_id": str(row.id), "operation_kind": "target_reservation", "planned_state": final_state,
        "metadata": new_metadata}
    pending = ContentInputReceipt(id=pending_id, tenant_id=row.tenant_id, carrier_kind="explicit_memory",
        carrier_ref=previous.carrier_ref, revision=previous.revision + 1,
        content_sha256=previous.content_sha256, dependencies_json=deepcopy(previous.dependencies_json),
        authority_json=pending_authority)
    db.add(pending)
    a["target"] = {"entry_id": target_id, "previous_receipt_id": str(previous.id),
        "previous_state": previous.authority_json["state"],
        "pending_receipt_id": str(pending_id), "metadata": metadata, "new_metadata": new_metadata,
        "final_state": final_state}
    row.authority_json = a
    return dependencies


async def _release_target(db, row):
    target = row.authority_json.get("target")
    if not target:
        return
    pending = await db.get(ContentInputReceipt, uuid.UUID(target["pending_receipt_id"]), populate_existing=True)
    if pending is not None and pending.authority_json.get("state") == "prepared":
        latest = await latest_memory_receipt(db, row.tenant_id, uuid.UUID(row.authority_json["agent_id"]), target["entry_id"])
        if latest is None or latest.id != pending.id or pending.authority_json.get("operation_id") != str(row.id):
            raise ContentInputReceiptError("explicit_memory_target_changed", "The target reservation must be reconciled before release.")
        authority = deepcopy(pending.authority_json)
        authority.update(state=target["previous_state"], metadata=target["metadata"])
        authority.pop("planned_state", None)
        authority.pop("operation_id", None)
        authority.pop("operation_kind", None)
        pending.authority_json = authority


async def _prepare_operation(request, root, session_factory):
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    kind = {"save_memory": "save", "update_memory": "update", "retire_memory": "retire"}[request.tool_name]
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        await memory_effect_actor(db, tenant_id, request)
        facts = await _writer_input(db, request, require_sources=kind != "retire")
        _tenant, _principal, actor, session, invocation, model, dependencies, args_hash = facts
        operation_id = uuid.uuid5(invocation.id, "explicit-memory-source")
        await _lock(db, tenant_id, request.context.agent_id, operation_id)
        row = await db.get(ContentInputReceipt, operation_id, populate_existing=True)
        if row is not None:
            _owned(row, tenant_id=tenant_id, agent_id=request.context.agent_id, actor_id=actor.id)
            if row.authority_json.get("args_hash") != args_hash:
                raise ContentInputReceiptError("explicit_memory_replay_mismatch", "The operation belongs to another command.", 403)
            return operation_id, False
        entry_id = "explicit_" + uuid.uuid5(invocation.id, "explicit-memory-entry").hex
        authority = {"schema": AUTHORITY_SCHEMA, "state": "prepared" if kind == "retire" else "reviewing",
            "operation_kind": kind, "tenant_id": str(tenant_id), "agent_id": str(request.context.agent_id),
            "owner_user_id": str(actor.id), **audience_for_session(session), "source_session_id": str(session.id),
            "invocation_id": str(invocation.id), "model_result_id": str(model.id),
            "model_request_hash": model.model_request_hash, "args_hash": args_hash, "entry_id": entry_id}
        row = ContentInputReceipt(id=operation_id, tenant_id=tenant_id, carrier_kind="explicit_memory",
            carrier_ref=memory_ref(request.context.agent_id, entry_id), revision=1,
            content_sha256="0" * 64, dependencies_json=dependencies, authority_json=authority)
        db.add(row)
        if kind in {"update", "retire"}:
            row.dependencies_json = await _reserve_target(db, request, row, root=root, actor=actor,
                dependencies=dependencies)
        await db.commit()
    return operation_id, True


async def _review_operation(request, operation_id, root, session_factory):
    from app.memory.explicit_overlay import prepare_memory_write_with_llm

    tenant_id = uuid.UUID(str(request.context.tenant_id))
    args = request.arguments
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        row = await db.get(ContentInputReceipt, operation_id)
        category = args.get("category") or row.authority_json.get("target", {}).get("metadata", {}).get("category", "general")
    refs = [str(ref) for ref in (args.get("source_refs") or [])]
    refs.append(f"session-tool-invocation:{row.authority_json['invocation_id']}")
    decision = await prepare_memory_write_with_llm(str(args["content"]), category=category,
        evidence_refs=refs, tenant_id=tenant_id, agent_id=request.context.agent_id)
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        await memory_effect_actor(db, tenant_id, request)
        await _writer_input(db, request)
        row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == operation_id).with_for_update())
        if row.authority_json["state"] != "reviewing":
            return
        a = deepcopy(row.authority_json)
        if decision.rejected or decision.held:
            a.update(state="rejected" if decision.rejected else "held", review_result={
                "category": decision.category, "sensitivity": decision.sensitivity, "reason": decision.reason})
            row.authority_json = a
            await _release_target(db, row)
        else:
            prepared = prepare_explicit_memory_entry(agent_id=request.context.agent_id, data_root=root,
                decision=decision, source_refs=refs, target_hint=_target_hint_for_category(decision.category),
                origin="explicit_user_correction" if a["operation_kind"] == "update" else "explicit_user_request",
                entry_id=a["entry_id"], extra_metadata={"source_receipt_id": str(row.id), "authority_schema": AUTHORITY_SCHEMA})
            a.update(state="prepared", metadata=prepared.entry.metadata, pending_write=prepared.rendered)
            row.authority_json = a
            row.content_sha256 = prepared.entry.source_sha256
        await db.commit()


def _stage_target(transaction, root, agent_id, target, *, restore=False):
    entries = load_explicit_overlay_entries(root, agent_id, transaction=transaction)
    entry = next((entry for entry in entries if entry.entry_id == target["entry_id"]), None)
    if entry is None:
        if not restore:
            return
        raise ContentInputReceiptError("explicit_memory_target_missing", "The original target file needs restoration.")
    if entry.metadata not in (target["metadata"], target["new_metadata"]):
        raise ContentInputReceiptError("explicit_memory_target_changed", "The target changed outside this operation.")
    metadata = target["metadata"] if restore else target["new_metadata"]
    _stage_explicit_overlay_status(transaction, entries=entries, entry=entry, record=metadata)


async def _resume_operation(request, operation_id, root, session_factory):
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        _principal, actor, _session = await _actor(db, tenant_id, request)
        row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == operation_id).with_for_update())
        _owned(row, tenant_id=tenant_id, agent_id=request.context.agent_id, actor_id=actor.id)
        a = deepcopy(row.authority_json)
        if a["state"] in {"held", "rejected"}:
            await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
                request=request, authority=a)
            result = a["review_result"]
            return ExplicitMemoryOverlayResult(status=a["state"], category=result["category"],
                sensitivity=result["sensitivity"], reason=result["reason"])
        if a["state"] in {"cancelled", "completed"}:
            return _outcome(row)
        if a["state"] == "active":
            entries = load_explicit_overlay_entries_by_ids(root, request.context.agent_id, [a["entry_id"]])
            if not entries:
                raise ContentInputReceiptError("explicit_memory_source_missing", "The recorded content needs restoration.")
            await _authorize_entry(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
                request=request, entry=entries[0], receipt=row)
            return _result(entries[0], duplicate=True)
        if a["state"] != "prepared":
            raise MemoryOperationError("explicit_memory_review_unknown",
                "The review outcome is unresolved. Cancel this write or inspect its status; do not repeat the review.", row.id)
        await memory_effect_actor(db, tenant_id, request)
        await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
            request=request, authority=a)
        if a["operation_kind"] != "retire":
            await validate_runtime_input_dependencies(db, request, row.dependencies_json)
        if a.get("target"):
            target = a["target"]
            await _lock(db, tenant_id, request.context.agent_id, target["entry_id"])
            latest = await latest_memory_receipt(db, tenant_id, request.context.agent_id, target["entry_id"])
            if latest is None or str(latest.id) != target["pending_receipt_id"] or latest.authority_json.get("operation_id") != str(row.id):
                raise ContentInputReceiptError("explicit_memory_target_changed", "The target reservation changed.")
        with AgentAssetTransaction(root / str(request.context.agent_id), operation="explicit_memory_source_write",
            idempotency_key=f"explicit-memory:{operation_id}", evidence_refs=(f"content-input:{operation_id}",)) as tx:
            if not tx.is_replay:
                if a["operation_kind"] != "retire":
                    stage_explicit_memory_entry(tx, _prepared_entry(root, request.context.agent_id, row))
                if a.get("target"):
                    _stage_target(tx, root, request.context.agent_id, a["target"])
            tx.commit()
        _principal, actor, _session = await memory_effect_actor(db, tenant_id, request)
        row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == operation_id).with_for_update())
        _owned(row, tenant_id=tenant_id, agent_id=request.context.agent_id, actor_id=actor.id)
        if row.authority_json["state"] != "prepared":
            raise MemoryOperationError("explicit_memory_operation_changed", "The operation changed before activation.", operation_id)
        a = deepcopy(row.authority_json)
        await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
            request=request, authority=a)
        entry = None
        if a["operation_kind"] != "retire":
            await validate_runtime_input_dependencies(db, request, row.dependencies_json)
            entries = load_explicit_overlay_entries_by_ids(root, request.context.agent_id, [a["entry_id"]])
            if not entries or entries[0].source_sha256 != row.content_sha256 or entries[0].metadata != a["metadata"]:
                raise MemoryOperationError("explicit_memory_write_unknown", "The planned bytes need reconciliation.", operation_id)
            entry = entries[0]
            a.update(state="active")
            a.pop("pending_write", None)
        else:
            a.update(state="completed")
        if a.get("target"):
            target = a["target"]
            latest = await latest_memory_receipt(db, tenant_id, request.context.agent_id, target["entry_id"])
            if latest is None or str(latest.id) != target["pending_receipt_id"]:
                raise MemoryOperationError("explicit_memory_target_changed", "The target version changed.", operation_id)
            updated = deepcopy(latest.authority_json)
            updated.update(state=target["final_state"])
            updated.pop("planned_state", None)
            latest.authority_json = updated
        row.authority_json = a
        await db.commit()
    return _result(entry) if entry is not None else _outcome(row)


async def execute_memory_operation(request, *, data_root, session_factory=None):
    root = Path(data_root)
    operation_id = None
    try:
        operation_id, created = await _prepare_operation(request, root, session_factory)
        if created and request.tool_name != "retire_memory":
            await _review_operation(request, operation_id, root, session_factory)
        return await _resume_operation(request, operation_id, root, session_factory)
    except Exception as exc:
        if operation_id is not None and not isinstance(exc, MemoryOperationError):
            status = exc.status_code if isinstance(exc, ContentInputReceiptError) else 503
            raise MemoryOperationError(getattr(exc, "code", "explicit_memory_operation_unknown"),
                "The retained memory operation needs inspection or reconciliation.", operation_id, status) from exc
        raise


async def reconcile_memory_operation(request, *, operation_id, action, data_root, session_factory=None):
    root = Path(data_root)
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    if operation_id is None:
        if action != "status":
            raise ContentInputReceiptError("explicit_memory_operation_required", "Choose a retained operation before retrying or cancelling.")
        async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
            _principal, actor, _session = await _actor(db, tenant_id, request)
            rows = (await db.scalars(select(ContentInputReceipt).where(
                ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "explicit_memory",
                ContentInputReceipt.authority_json["schema"].astext == AUTHORITY_SCHEMA,
                ContentInputReceipt.authority_json["agent_id"].astext == str(request.context.agent_id),
                ContentInputReceipt.authority_json["owner_user_id"].astext == str(actor.id),
                ContentInputReceipt.authority_json["operation_kind"].astext.in_(["save", "update", "retire"]),
                ContentInputReceipt.authority_json["state"].astext.in_(["reviewing", "prepared", "cancel_pending", "held"]),
            ).order_by(ContentInputReceipt.id))).all()
            return {"schema": "hive.explicit_memory_pending_operations.v1", "status": "ok",
                "operations": [_outcome(row) for row in rows]}
    operation_id = uuid.UUID(str(operation_id))
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        _principal, actor, _session = await _actor(db, tenant_id, request)
        row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == operation_id).with_for_update())
        _owned(row, tenant_id=tenant_id, agent_id=request.context.agent_id, actor_id=actor.id)
        if action == "status":
            return _outcome(row)
        await memory_effect_actor(db, tenant_id, request)
        if action == "cancel":
            if row.authority_json["state"] in {"active", "completed"}:
                raise ContentInputReceiptError("explicit_memory_already_committed", "Retire the committed memory instead of cancelling its completed write.")
            if row.authority_json["state"] == "cancelled":
                return _outcome(row)
            a = deepcopy(row.authority_json)
            a["state"] = "cancel_pending"
            row.authority_json = a
            await db.commit()
        elif action != "retry":
            raise ValueError("unsupported memory reconciliation action")
    if action == "retry":
        return await _resume_operation(request, operation_id, root, session_factory)
    with AgentAssetTransaction(root / str(request.context.agent_id), operation="explicit_memory_source_cancel",
        idempotency_key=f"explicit-memory-cancel:{operation_id}", evidence_refs=(f"content-input:{operation_id}",)) as tx:
        if not tx.is_replay:
            if a.get("target"):
                _stage_target(tx, root, request.context.agent_id, a["target"], restore=True)
            entries = load_explicit_overlay_entries(root, request.context.agent_id, transaction=tx)
            entry = next((entry for entry in entries if entry.entry_id == a["entry_id"]), None)
            if entry is not None:
                _stage_explicit_overlay_status(tx, entries=entries, entry=entry,
                    record={**entry.metadata, "status": "retired", "status_reason": "cancelled_source_write"})
        tx.commit()
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        _principal, actor, _session = await memory_effect_actor(db, tenant_id, request)
        row = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == operation_id).with_for_update())
        _owned(row, tenant_id=tenant_id, agent_id=request.context.agent_id, actor_id=actor.id)
        await _release_target(db, row)
        row.authority_json = {**row.authority_json, "state": "cancelled"}
        await db.commit()
    return _outcome(row)
