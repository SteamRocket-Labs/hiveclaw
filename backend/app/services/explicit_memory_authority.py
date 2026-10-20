"""Bind explicit memory content to durable runtime authority and input receipts."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.memory.explicit_overlay import (
    ExplicitMemoryOverlayEntry,
    ExplicitMemoryOverlayResult,
    PreparedExplicitMemoryEntry,
    _extract_tag,
    load_explicit_overlay_entries_by_ids,
)
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.content_input_receipts import (
    ContentInputReceiptError,
    _runtime_input_actor,
    _validate_input_dependencies,
    _validate_source_bytes,
    validate_runtime_input_dependencies,
)
from app.services.session_model_inputs import _model_dependencies
from app.services.session_tool_runtime import _sha256

AUTHORITY_SCHEMA = "hive.explicit_memory_authority.v1"


def memory_ref(agent_id, entry_id):
    if Path(entry_id).name != entry_id or entry_id in {".", ".."}:
        raise ContentInputReceiptError("explicit_memory_binding_invalid", "The memory identity is invalid.")
    return f"{agent_id}\n{entry_id}"


def source_dependency(receipt):
    return {"source_kind": "explicit_memory", "source_id": str(receipt.id),
        "source_version": receipt.content_sha256}


async def _actor(db, tenant_id, request):
    from app.models.chat_session import ChatSession

    principal, actor = await _runtime_input_actor(db, tenant_id, request)
    session = await db.get(ChatSession, uuid.UUID(str(principal.session_id)), populate_existing=True)
    if getattr(actor, "id", None) is None or session.audience_kind not in {None, "private", "group"}:
        raise ContentInputReceiptError("explicit_memory_scoped_audience_unavailable",
            "This memory operation requires a supported current source audience.")
    return principal, actor, session


def audience_for_session(session):
    if session.audience_kind in {None, "private"}:
        return {"audience_kind": "private"}
    if (session.audience_kind == "group" and session.user_id is None and session.external_principal_id is None
        and session.group_installation_ref and session.group_subject_id and session.source_channel):
        return {"audience_kind": "group", "group_installation_ref": session.group_installation_ref,
            "group_subject_id": session.group_subject_id, "source_channel": session.source_channel}
    raise ContentInputReceiptError("explicit_memory_scoped_audience_unavailable", "The source audience needs reconciliation.")


def _same_group(authority, session):
    return session.audience_kind == "group" and all(authority.get(key) == value
        for key, value in audience_for_session(session).items())


async def authorize_memory_audience(db, *, tenant_id, agent_id, request, authority, mutation=False):
    """A source audience is independent of creator and operation-recovery ownership."""
    from fastapi import HTTPException
    from app.models.chat_session import ChatSession
    from app.services.shared_group_authority import require_current_group_member

    principal, actor, current = await _actor(db, tenant_id, request)
    if (authority.get("schema") != AUTHORITY_SCHEMA or authority.get("tenant_id") != str(tenant_id)
        or authority.get("agent_id") != str(agent_id) or principal.agent_id != agent_id):
        raise ContentInputReceiptError("explicit_memory_source_unverified", "The memory source binding is unavailable.", 403)
    if authority.get("audience_kind") == "private":
        if authority.get("owner_user_id") != str(actor.id) or current.audience_kind not in {None, "private"}:
            raise ContentInputReceiptError("explicit_memory_source_unverified", "The private memory is not authorized in this work.", 403)
    elif authority.get("audience_kind") == "group":
        try:
            origin = await db.get(ChatSession, uuid.UUID(authority["source_session_id"]), populate_existing=True)
            if (origin is None or origin.tenant_id != tenant_id or origin.agent_id != agent_id
                or not _same_group(authority, origin)
                or (current.audience_kind == "group" and not _same_group(authority, current))
                or (mutation and not _same_group(authority, current))):
                raise ContentInputReceiptError("explicit_memory_group_context_mismatch",
                    "Revise group memory in its original group; private work does not grant group disclosure.", 403)
            # Even an administrator's Agent run needs actual membership.
            # Business inspection belongs to the separate browser reader.
            await require_current_group_member(db, session=origin, user=actor)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise ContentInputReceiptError(detail.get("code", "explicit_memory_group_denied"),
                "The original group requires current membership and installation access.", exc.status_code) from exc
        except ContentInputReceiptError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise ContentInputReceiptError("explicit_memory_group_source_unavailable", "The original group binding needs recovery.") from exc
    else:
        raise ContentInputReceiptError("explicit_memory_scoped_audience_unavailable", "This memory audience is unsupported.")
    return principal, actor, current


async def memory_effect_actor(db, tenant_id, request):
    """Refresh the originating work and existing policy at the memory effect."""
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_ticket import build_live_approval_policy_snapshot, hash_policy_snapshot
    from app.services.session_approval import invocation_enterprise_approval
    from app.tools.guard_policy import evaluate_guard_policy

    principal, actor, session = await _actor(db, tenant_id, request)
    task = await db.get(RuntimeTask, uuid.UUID(str(request.context.runtime_task_id)), populate_existing=True)
    if (task is None or task.tenant_id != tenant_id or task.status not in {"pending", "running", "suspended", "resumable"}
        or task.root_user_id != actor.id or str(session.id) not in {task.parent_session_id, task.child_session_id}
        or request.context.agent_id not in {task.parent_agent_id, task.child_agent_id}):
        raise ContentInputReceiptError("explicit_memory_work_inactive", "Continue memory changes from current authorized work.", 403)
    live = await build_live_approval_policy_snapshot(db=db, tenant_id=tenant_id,
        agent_id=request.context.agent_id, tool_name=request.tool_name)
    capability = live["capability"]
    verdict = evaluate_guard_policy(tool_name=request.tool_name, arguments=request.arguments,
        external_visible=False, snapshot=live["guard_policy"])
    if capability["denied"] or verdict.decision == "deny":
        raise ContentInputReceiptError("explicit_memory_policy_denied", "Current policy denies this memory change.", 403)
    if (capability["policy_found"] and capability["escalate_to_l3"]) or verdict.decision == "require_approval":
        decision = request.context.approval_decision
        invocation = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.tenant_id == tenant_id, SessionToolInvocation.session_id == session.id,
            SessionToolInvocation.run_id == task.id, SessionToolInvocation.tool_name == request.tool_name,
            SessionToolInvocation.provider_tool_use_id == request.context.tool_call_id))
        args_hash = _sha256({"tool_name": request.tool_name, "arguments": request.arguments})
        approval = await invocation_enterprise_approval(db, invocation=invocation,
            agent_id=request.context.agent_id, decision={"approval_id": str(decision.approval_id),
                "decision_id": decision.decision_id}, input_hash=args_hash) if decision and invocation else None
        if (approval is None or approval.status != "approved" or approval.consumed_at is None
            or approval.requested_by != actor.id or approval.execution_envelope_hash != decision.envelope_hash
            or approval.policy_snapshot_hash != decision.policy_snapshot_hash
            or approval.policy_snapshot_hash != hash_policy_snapshot(live)):
            raise ContentInputReceiptError("explicit_memory_approval_required", "Current policy requires approval for this memory change.")
    return principal, actor, session


async def latest_memory_receipt(db, tenant_id, agent_id, entry_id):
    return await db.scalar(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "explicit_memory",
        ContentInputReceipt.carrier_ref == memory_ref(agent_id, entry_id),
    ).order_by(ContentInputReceipt.revision.desc()).limit(1).execution_options(populate_existing=True))


async def _authorize_entry(db, *, tenant_id, agent_id, request, entry, receipt, active_receipts=frozenset()):
    authority = receipt.authority_json if receipt is not None else {}
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=authority)
    if (
        receipt is None or receipt.tenant_id != tenant_id or receipt.carrier_kind != "explicit_memory"
        or receipt.carrier_ref != memory_ref(agent_id, entry.entry_id)
        or receipt.content_sha256 != entry.source_sha256
        or authority.get("schema") != AUTHORITY_SCHEMA or authority.get("state") != "active"
        or authority.get("tenant_id") != str(tenant_id) or authority.get("agent_id") != str(agent_id)
        or entry.status != "active"
        or authority.get("metadata") != entry.metadata
    ):
        raise ContentInputReceiptError("explicit_memory_source_unverified",
            "This memory has no current source receipt for this reader. Preserve it for source confirmation.", 403)
    if receipt.id in active_receipts:
        raise ContentInputReceiptError("explicit_memory_source_cycle", "The memory source chain is cyclic.")
    version = _sha256(authority)
    path = Path(str(agent_id)) / f"memory/explicit/entries/{entry.entry_id}.md"
    _validate_source_bytes(path, receipt.content_sha256)
    await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, runtime_request=request,
        active_workspace_receipts=active_receipts | {receipt.id})
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=authority)
    latest = await latest_memory_receipt(db, tenant_id, agent_id, entry.entry_id)
    if latest is None or latest.id != receipt.id or _sha256(latest.authority_json) != version:
        raise ContentInputReceiptError("explicit_memory_source_changed", "The memory authority changed while reading.", 403)
    _validate_source_bytes(path, receipt.content_sha256)
    return receipt


async def authorized_explicit_entries(*, tenant_id, agent_id, request, entries, session_factory=None, issue_observer=None):
    """Return only exact current versions; do not let previews bypass this boundary."""
    allowed = []
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        _principal, actor, _session = await _actor(db, tenant_id, request)
        actor_id = actor.id
        current_audience = audience_for_session(_session)
        issues = []
        for entry in entries:
            receipt = None
            try:
                receipt = await latest_memory_receipt(db, tenant_id, agent_id, entry.entry_id)
                await _authorize_entry(db, tenant_id=tenant_id, agent_id=agent_id, request=request,
                    entry=entry, receipt=receipt)
            except ContentInputReceiptError as exc:
                authority = receipt.authority_json if receipt is not None else {}
                if (authority.get("owner_user_id") == str(actor_id)
                    and authority.get("tenant_id") == str(tenant_id) and authority.get("agent_id") == str(agent_id)
                    and authority.get("schema") == AUTHORITY_SCHEMA and authority.get("state") not in {"retired", "revoked"}
                    and (_session.audience_kind in {None, "private"} or _same_group(authority, _session))):
                    issues.append({"memory_id": entry.entry_id, "status": "denied" if exc.status_code == 403 else "unavailable",
                        "code": exc.code})
                elif (authority.get("schema") == AUTHORITY_SCHEMA and authority.get("audience_kind") == "group"
                    and exc.status_code >= 500):
                    issue = {"status": "unavailable", "code": "explicit_memory_group_unavailable"}
                    if issue not in issues:
                        issues.append(issue)
                continue
            allowed.append((entry, receipt))
        _principal, current_actor, _session = await _actor(db, tenant_id, request)
        if current_actor.id != actor_id or audience_for_session(_session) != current_audience:
            raise ContentInputReceiptError("explicit_memory_reader_changed", "The current memory reader changed.", 403)
        if issue_observer is not None:
            issue_observer.extend(issues)
    return allowed


async def authorize_explicit_memory_file_read(request, path):
    from dataclasses import replace
    from app.config import get_settings
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope, WorkspaceInputRead

    context = request.context
    scope = context.workspace_authority_scope
    parts = Path(path).parts
    if (len(parts) != 4 or parts[:3] != ("memory", "explicit", "entries") or not path.endswith(".md")
        or not isinstance(scope, WorkspaceAuthorityScope) or scope.operator_view
        or scope.agent_id != context.agent_id or scope.user_id != context.user_id):
        raise ContentInputReceiptError("explicit_memory_raw_collection_denied",
            "Use search_memory or load_memory to read the current authorized memory view.", 403)
    root = Path(get_settings().AGENT_DATA_DIR)
    if Path(context.workspace).resolve() != (root / str(context.agent_id)).resolve():
        raise ContentInputReceiptError("explicit_memory_workspace_mismatch", "The memory storage root does not match.", 403)
    entries = load_explicit_overlay_entries_by_ids(root, context.agent_id, [Path(path).stem])
    authorized = await authorized_explicit_entries(tenant_id=uuid.UUID(str(context.tenant_id)),
        agent_id=context.agent_id, request=request, entries=entries)
    if len(authorized) != 1:
        raise ContentInputReceiptError("explicit_memory_file_denied", "The memory file is not authorized for this reader.", 403)
    _entry, receipt = authorized[0]
    observed = WorkspaceInputRead(path=path, receipt_id=receipt.id, revision=receipt.revision,
        content_sha256=receipt.content_sha256, dependencies_hash=_sha256(receipt.dependencies_json),
        carrier_kind="explicit_memory")
    context.workspace_authority_scope = replace(scope,
        input_reads=tuple(read for read in scope.input_reads if read.path != path) + (observed,))
    return observed


async def validate_explicit_memory_dependency(db, tenant_id, dependency, *, request, active_receipts=frozenset()):

    try:
        receipt = await db.get(ContentInputReceipt, uuid.UUID(dependency["source_id"]), populate_existing=True)
        if receipt is None or receipt.tenant_id != tenant_id or receipt.carrier_kind != "explicit_memory":
            raise ValueError("explicit_memory_receipt_missing")
        raw_agent, entry_id = receipt.carrier_ref.split("\n", 1)
        agent_id = uuid.UUID(raw_agent)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContentInputReceiptError("explicit_memory_source_unverified", "The memory source receipt is unavailable.") from exc
    if receipt.content_sha256 != dependency["source_version"]:
        raise ContentInputReceiptError("explicit_memory_source_changed", "The observed memory bytes changed.", 403)
    await authorize_memory_history(db, tenant_id=tenant_id, agent_id=agent_id, entry_id=entry_id,
        request=request, receipt=receipt, active_receipts=active_receipts)


async def authorize_memory_history(db, *, tenant_id, agent_id, entry_id, request, receipt, active_receipts=frozenset()):
    """Historical evidence retains source ACLs; semantic retirement stops new recall."""
    authority = receipt.authority_json
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=authority)
    if (receipt.tenant_id != tenant_id or receipt.carrier_kind != "explicit_memory"
        or receipt.carrier_ref != memory_ref(agent_id, entry_id)
        or authority.get("schema") != AUTHORITY_SCHEMA or authority.get("state") not in {"active", "retired"}
        or authority.get("tenant_id") != str(tenant_id) or authority.get("agent_id") != str(agent_id)):
        raise ContentInputReceiptError("explicit_memory_history_denied", "The historical memory source is not authorized.", 403)
    if receipt.id in active_receipts:
        raise ContentInputReceiptError("explicit_memory_source_cycle", "The memory source chain is cyclic.")
    path = Path(str(agent_id)) / f"memory/explicit/entries/{entry_id}.md"
    _validate_source_bytes(path, receipt.content_sha256)

    await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, runtime_request=request,
        active_workspace_receipts=active_receipts | {receipt.id})
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=authority)
    latest = await latest_memory_receipt(db, tenant_id, agent_id, entry_id)
    if (latest is None or latest.authority_json.get("state") == "revoked"
        or (latest.authority_json.get("state") == "prepared" and latest.authority_json.get("planned_state") == "revoked")):
        raise ContentInputReceiptError("explicit_memory_source_revoked", "The memory source was withdrawn for privacy.", 403)
    _validate_source_bytes(path, receipt.content_sha256)


async def validate_browser_memory_dependency(db, tenant_id, dependency, *, user, active_receipts=frozenset()):
    """Business inspection authority stays with the current browser principal."""
    from app.core.permissions import is_scoped_business_admin
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.content_input_receipts import _authorize_session_input_source

    from app.core.source_read_purpose import business_inspection_allowed

    def can_inspect(reader):
        return business_inspection_allowed() and is_scoped_business_admin(reader, resource_tenant_id=tenant_id)

    try:
        receipt = await db.get(ContentInputReceipt, uuid.UUID(dependency["source_id"]), populate_existing=True)
        a = receipt.authority_json
        raw_agent, entry_id = receipt.carrier_ref.split("\n", 1)
        agent_id = uuid.UUID(raw_agent)
        requester = await db.get(User, user.id, populate_existing=True)
        session = await db.get(ChatSession, uuid.UUID(a["source_session_id"]), populate_existing=True)
        if (receipt.tenant_id != tenant_id or receipt.carrier_kind != "explicit_memory"
            or receipt.content_sha256 != dependency["source_version"] or a.get("schema") != AUTHORITY_SCHEMA
            or a.get("state") not in {"active", "retired"} or a.get("audience_kind") not in {"private", "group"}
            or a.get("tenant_id") != str(tenant_id) or a.get("agent_id") != str(agent_id)
            or requester is None or not requester.is_active
            or (a.get("audience_kind") == "private" and a.get("owner_user_id") != str(requester.id)
                and not can_inspect(requester))
            or session is None or session.tenant_id != tenant_id or session.agent_id != agent_id
            or (a.get("audience_kind") == "group" and not _same_group(a, session))):
            raise ValueError("browser_memory_source_denied")
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise ContentInputReceiptError("explicit_memory_browser_denied", "This memory source is not authorized for this browser reader.", 403) from exc
    if receipt.id in active_receipts:
        raise ContentInputReceiptError("explicit_memory_source_cycle", "The memory source chain is cyclic.")
    path = Path(str(agent_id)) / f"memory/explicit/entries/{entry_id}.md"
    _validate_source_bytes(path, receipt.content_sha256)
    await _authorize_session_input_source(db, tenant_id, session, user=requester, runtime_request=None)
    await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, user=requester,
        active_workspace_receipts=active_receipts | {receipt.id})
    requester = await db.get(User, user.id, populate_existing=True)
    if (requester is None or not requester.is_active
        or (a.get("audience_kind") == "private" and a.get("owner_user_id") != str(requester.id)
            and not can_inspect(requester))):
        raise ContentInputReceiptError("explicit_memory_browser_denied", "The browser source reader changed.", 403)
    session = await db.get(ChatSession, session.id, populate_existing=True)
    if a.get("audience_kind") == "group" and (session is None or not _same_group(a, session)):
        raise ContentInputReceiptError("explicit_memory_group_source_changed", "The original group binding changed.", 403)
    await _authorize_session_input_source(db, tenant_id, session, user=requester, runtime_request=None)
    latest = await latest_memory_receipt(db, tenant_id, agent_id, entry_id)
    if (latest is None or latest.authority_json.get("state") == "revoked"
        or latest.authority_json.get("planned_state") == "revoked"):
        raise ContentInputReceiptError("explicit_memory_source_revoked", "The memory source was withdrawn for privacy.", 403)
    _validate_source_bytes(path, receipt.content_sha256)


async def authorize_browser_memory_path(db, user, *, agent_id, path):
    """Agent management does not transfer another requester's memory audience."""
    from app.core.permissions import check_agent_access, is_scoped_business_admin
    from app.core.resource_authority import QUARANTINED_AUTHORITY_STATE, authorize_resource_action, workspace_resource_id

    agent, access = await check_agent_access(db, user, agent_id)
    if is_scoped_business_admin(user, resource_tenant_id=agent.tenant_id):
        # Explicit business inspection is attributed to the browser administrator;
        # it never becomes a runtime input-read grant for the digital employee.
        decision = await authorize_resource_action(db, user, agent_id=agent_id, resource_kind="workspace_file",
            resource_id=workspace_resource_id(agent_id, path), action="read",
            authority_state=QUARANTINED_AUTHORITY_STATE, agent_access=(agent, access))
        return decision.authority_source
    if path == "runtime_artifacts/asset_transactions" or path.startswith("runtime_artifacts/asset_transactions/"):
        raise ContentInputReceiptError("explicit_memory_control_storage_denied",
            "Inspect owned work through its recovery tools; raw transaction storage contains mixed source audiences.", 403)
    parts = Path(path).parts
    if len(parts) != 4 or parts[:3] != ("memory", "explicit", "entries") or not path.endswith(".md"):
        scoped = await db.scalar(select(ContentInputReceipt.id).where(ContentInputReceipt.tenant_id == agent.tenant_id,
            ContentInputReceipt.carrier_kind == "explicit_memory",
            ContentInputReceipt.carrier_ref.startswith(f"{agent_id}\n")).limit(1))
        if scoped is None:
            return "legacy_agent_management"
        raise ContentInputReceiptError("explicit_memory_collection_denied",
            "Use search_memory or load_memory for the current source-scoped memory view.", 403)
    receipt = await latest_memory_receipt(db, agent.tenant_id, agent_id, Path(path).stem)
    if receipt is None:
        # Preserve the established management-only view of legacy unreceipted
        # files. They still cannot acquire live prompt or derived authority.
        return "legacy_agent_management"
    await validate_browser_memory_dependency(db, agent.tenant_id, source_dependency(receipt), user=user)
    return "explicit_memory_source"


async def _writer_input(db, request, *, require_sources=True):
    context = request.context
    tenant_id = uuid.UUID(str(context.tenant_id))
    principal, actor, session = await _actor(db, tenant_id, request)
    rows = (await db.execute(select(SessionToolInvocation, SessionModelResult).join(SessionModelResult,
        SessionModelResult.provider_request_id == SessionToolInvocation.provider_request_id).where(
            SessionToolInvocation.tenant_id == tenant_id, SessionToolInvocation.session_id == session.id,
            SessionToolInvocation.run_id == uuid.UUID(str(context.runtime_task_id)),
            SessionToolInvocation.provider_tool_use_id == context.tool_call_id,
            SessionToolInvocation.tool_name == request.tool_name,
            SessionModelResult.tenant_id == tenant_id, SessionModelResult.session_id == session.id,
            SessionModelResult.run_id == SessionToolInvocation.run_id,
            SessionModelResult.turn_id == str(context.turn_id),
        ).limit(2).execution_options(populate_existing=True))).all()
    if len(rows) != 1:
        raise ContentInputReceiptError("explicit_memory_input_unavailable", "The exact sealed memory invocation is unavailable.")
    invocation, model = rows[0]
    arguments_hash = _sha256({"tool_name": request.tool_name, "arguments": request.arguments})
    effective = invocation.effective_args_hash or invocation.args_hash
    dependencies, complete = _model_dependencies(model)
    if (effective != arguments_hash or model.state not in {"sealed", "round_committed"}
        or _sha256(model.model_request_snapshot_json) != model.model_request_hash or (require_sources and not complete)):
        raise ContentInputReceiptError("explicit_memory_input_unverified",
            "The memory write requires complete source bindings for its exact model inputs.")
    if require_sources:
        await validate_runtime_input_dependencies(db, request, dependencies)
    return tenant_id, principal, actor, session, invocation, model, dependencies, arguments_hash


def _prepared_entry(root, agent_id, receipt):
    authority = receipt.authority_json
    metadata = authority["metadata"]
    rendered = authority["pending_write"]
    if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != receipt.content_sha256:
        raise ContentInputReceiptError("explicit_memory_plan_invalid", "The retained memory write plan changed.")
    entry_id = metadata["id"]
    return PreparedExplicitMemoryEntry(ExplicitMemoryOverlayEntry(
        entry_id=entry_id, status=metadata["status"], category=metadata["category"],
        target_hint=metadata["target_hint"], content=_extract_tag(rendered, "normalized_memory"),
        source_refs=tuple(ref for ref in metadata["source_refs"].split(",") if ref),
        sensitivity=metadata["sensitivity"], path=Path(root) / str(agent_id) / f"memory/explicit/entries/{entry_id}.md",
        created_at=metadata["created_at"], metadata=metadata, source_sha256=receipt.content_sha256,
    ), rendered)


def _result(entry, *, duplicate=False):
    return ExplicitMemoryOverlayResult(status="duplicate" if duplicate else "active", category=entry.category,
        entry_id=entry.entry_id, path=f"memory/explicit/entries/{entry.entry_id}.md",
        sensitivity=entry.sensitivity, target_hint=entry.target_hint, content=entry.content)


async def save_scoped_explicit_memory(request, *, data_root, session_factory=None):
    from app.services.explicit_memory_operations import execute_memory_operation

    return await execute_memory_operation(request, data_root=data_root, session_factory=session_factory)


async def validate_memory_model_sources(db, *, task, agent_id, session_id, dependencies):
    """Recheck source-bound recalled memory at the actual provider boundary."""
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    sources = [ref for ref in dependencies if ref["source_kind"] in {"explicit_memory", "team_memory"}]
    if not sources:
        return
    try:
        if task is None:
            raise ContentInputReceiptError("explicit_memory_work_unavailable", "Restore the original memory reader.", 503)
        principal = AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=task.root_user_id,
            session_id=str(session_id), runtime_task_id=str(task.id), purpose="interactive_session")
        for source in sources:
            if source["source_kind"] == "team_memory":
                from app.services.team_memory_runtime import validate_dependency

                await validate_dependency(db, task.tenant_id, source, runtime_request=principal)
            else:
                await validate_explicit_memory_dependency(db, task.tenant_id, source, request=principal)
    except ContentInputReceiptError as exc:
        raise ExecutionAuthorityUnavailable(code=exc.code, task=task, effect="model_request",
            status="denied" if exc.status_code in {401, 403, 404} else "unavailable") from exc
