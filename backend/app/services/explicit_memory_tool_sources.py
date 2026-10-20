"""Exact memory-tool observations used by the next sealed model request."""

from datetime import datetime, timezone
import uuid

from app.database import tenant_scoped_session
from app.models.invocation_span import InvocationSpan
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.explicit_memory_authority import _actor, _authorize_entry, _writer_input, source_dependency
from app.services.session_model_inputs import _hash, _stored_dependencies, _unique_dependencies

MEMORY_RESULT_TOOLS = frozenset({"save_memory", "update_memory", "retire_memory", "reconcile_memory_write",
    "load_memory", "search_memory"})


async def record_memory_result(request, *, content, entries=(), team_entries=(), unbound=(), session_factory=None):
    """Pin actual emitted entries, never infer source completeness from prose."""
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        await _actor(db, tenant_id, request)
        for entry, receipt in entries:
            await _authorize_entry(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
                request=request, entry=entry, receipt=receipt)
        from app.services.team_memory_runtime import authorize_entry, source_dependency as team_source

        for entry, receipt in team_entries:
            await authorize_entry(db, tenant_id, request, entry, receipt)
        try:
            facts = await _writer_input(db, request, require_sources=False)
        except ContentInputReceiptError as exc:
            # Compatibility callers can read their authorized content without a
            # canonical model invocation; that result grants no derived authority.
            if exc.code == "explicit_memory_input_unavailable":
                return
            raise
        _tenant, _principal, actor, _session, invocation, model, _dependencies, args_hash = facts
        proof = {"schema": "hive.explicit_memory_tool_result.v1", "invocation_id": str(invocation.id),
            "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
            "args_hash": args_hash, "authority_hash": invocation.authority_snapshot_hash,
            "content_hash": _hash(content), "sources": _unique_dependencies([
                *[source_dependency(r) for _e, r in entries], *[team_source(r) for _e, r in team_entries]]),
            "unbound": sorted(set(unbound))}
        identity = uuid.uuid5(invocation.id, "explicit-memory-tool-result")
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        if row is None:
            db.add(InvocationSpan(id=identity, tenant_id=tenant_id, agent_id=request.context.agent_id,
                user_id=actor.id, session_id=str(invocation.session_id), runtime_task_id=invocation.run_id,
                trace_id=f"session-run:{invocation.run_id}", span_id=f"memory-result:{identity}",
                span_type="memory_result", name=request.tool_name, status="captured", duration_ms=0,
                started_at=datetime.now(timezone.utc), metadata_json={"memory_result": proof}))
        elif row.metadata_json != {"memory_result": proof}:
            raise ContentInputReceiptError("explicit_memory_result_changed",
                "The original memory result needs reconciliation; its source binding cannot be replaced.")
        await db.commit()


async def memory_result_sources(db, *, tenant_id, agent_id, invocation, model, content_hash):
    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "explicit-memory-tool-result"), populate_existing=True)
    proof = (row.metadata_json or {}).get("memory_result") if row else None
    valid = (row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
        and row.runtime_task_id == invocation.run_id and row.session_id == str(invocation.session_id)
        and row.span_type == "memory_result" and row.name == invocation.tool_name and row.status == "captured"
        and invocation.tool_name in MEMORY_RESULT_TOOLS and invocation.effect_state == "effect_committed"
        and isinstance(proof, dict) and proof.get("schema") == "hive.explicit_memory_tool_result.v1"
        and proof.get("invocation_id") == str(invocation.id) and proof.get("content_hash") == content_hash
        and model is not None and proof.get("model_result_id") == str(model.id)
        and proof.get("model_request_hash") == model.model_request_hash
        and proof.get("args_hash") == (invocation.effective_args_hash or invocation.args_hash)
        and proof.get("authority_hash") == invocation.authority_snapshot_hash
        and isinstance(proof.get("unbound"), list))
    sources = _stored_dependencies(proof.get("sources")) if valid else None
    return (sources or [], valid and sources is not None and not proof["unbound"])
