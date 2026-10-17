"""Trusted content receipts for the two file-backed carriers in the M1 contract."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select, text

from app.models.organization_identity import ContentInputReceipt
from app.services.team_memory import _safe_component, _slugify


class ContentInputReceiptError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def team_memory_ref(workspace_key: str, key: str) -> str:
    return f"{workspace_key}\n{_slugify(key)}"


async def lock_team_memory_write(db, tenant_id: uuid.UUID, workspace_key: str, key: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"team-memory:{tenant_id}:{_safe_component(workspace_key)}:{_slugify(key)}"})


async def latest_team_memory_receipts(db, tenant_id: uuid.UUID, workspace_key: str, entries) -> dict:
    refs = [team_memory_ref(workspace_key, Path(entry.path).stem) for entry in entries]
    if not refs:
        return {}
    rows = (await db.scalars(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id,
        ContentInputReceipt.carrier_kind == "team_memory",
        ContentInputReceipt.carrier_ref.in_(refs),
    ).distinct(ContentInputReceipt.carrier_ref).order_by(
        ContentInputReceipt.carrier_ref, ContentInputReceipt.revision.desc(),
    ))).all()
    return {row.carrier_ref: row for row in rows}


async def validate_browser_input_dependencies(db, tenant_id: uuid.UUID, user, dependencies: list) -> None:
    # Browser authority, including PDEC-013, must not authorize Agent prompt consumption.
    await _validate_input_dependencies(db, tenant_id, dependencies, user=user)


async def validate_runtime_input_dependencies(db, request, dependencies: list) -> None:
    await _validate_input_dependencies(db, uuid.UUID(str(request.context.tenant_id)), dependencies,
        runtime_request=request)


async def _validate_input_dependencies(db, tenant_id: uuid.UUID, dependencies: list, *, user=None, runtime_request=None) -> None:
    from app.config import get_settings
    from app.models.knowledge import KnowledgeDocument
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    if not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        raise ContentInputReceiptError("content_source_unavailable", "The source receipt is invalid.", 503)
    for dependency in dependencies:
        if dependency["source_kind"] != "knowledge_document":
            raise ContentInputReceiptError("content_source_unavailable", "A source receipt cannot yet be revalidated. Regenerate from verifiable inputs.", 503)
        try:
            source_id = uuid.UUID(dependency["source_id"])
        except ValueError as exc:
            raise ContentInputReceiptError("content_source_unavailable", "The source receipt is invalid.", 503) from exc
        document = (await db.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.id == source_id,
        ).execution_options(populate_existing=True))).one_or_none()
        if document is None or document.canonical_md_sha256 != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "A source is missing or has changed. Regenerate from current authorized inputs.", 403)
        if document.scope_type == "person":
            if runtime_request is not None:
                from app.tools.handlers.knowledge import _personal_kb_runtime_principal

                try:
                    principal = _personal_kb_runtime_principal(runtime_request)
                except ValueError as exc:
                    raise ContentInputReceiptError("content_source_access_denied",
                        "The runtime source authority is invalid.", 403) from exc
            else:
                principal = HumanBrowserPrincipal(user_id=user.id, role=user.role, home_tenant_id=tenant_id)
            result = await PersonalKnowledgeService().get_personal_document_with_authority(db,
                tenant_id=tenant_id, owner_user_id=document.scope_id, document_id=document.id,
                principal=principal)
        elif document.scope_type == "company":
            if runtime_request is not None:
                raise ContentInputReceiptError("content_source_unavailable",
                    "This source requires its immutable Company Knowledge publication reader.", 503)
            from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
            from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

            result = await CompanyKnowledgeGateway().read(db,
                principal=CompanyKnowledgePrincipal(tenant_id=tenant_id, accountable_user_id=user.id,
                    accountable_role=user.role, actor_type="user", actor_id=user.id,
                    department_id=getattr(user, "department_id", None), purpose="interactive_session"),
                request=CompanyKnowledgeReadRequest(document_id=document.id, publication_id=None, segment_ids=(),
                    max_chars=None, trace_id=f"content-input:{uuid.uuid4().hex}"))
        else:
            raise ContentInputReceiptError("content_source_unavailable", "This source requires its canonical audience reader.", 503)
        if result.status != "ok" or getattr(result, "credential_reference", None):
            raise ContentInputReceiptError("content_source_access_denied", "A required source is no longer authorized for this reader.", 403)
        try:
            root = Path(get_settings().AGENT_DATA_DIR).resolve()
            path = Path(document.canonical_md_path)
            if path.is_absolute():
                path = path.relative_to(root)
            with open_workspace_file(root, path.as_posix()) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, ValueError, WorkspaceAuthorityError) as exc:
            raise ContentInputReceiptError("content_source_unavailable", "The canonical source bytes are unavailable.", 503) from exc
        if digest != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The canonical source bytes no longer match the receipt.", 403)


async def authorize_workspace_receipt_read(request):
    """Recheck known receipt dependencies under the actual native Agent principal."""
    from dataclasses import replace
    from sqlalchemy.exc import SQLAlchemyError
    from app.core.resource_authority import normalize_workspace_resource_path
    from app.database import tenant_scoped_session
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.result_envelope import render_tool_error

    if request.tool_name not in {"read_file", "read_document"} and not (
        request.tool_name == "fs_read" and str(request.arguments.get("mode") or "text").strip().lower() in {"text", "document"}
    ):
        return None
    context = request.context
    try:
        path = normalize_workspace_resource_path(str(request.arguments.get("path") or ""))
        if not path.startswith("workspace/"):
            return None
        scope = context.workspace_authority_scope
        if scope is not None and not scope.can_read(path):
            return None  # The existing path boundary denies before body access.
        tenant_id = uuid.UUID(str(context.tenant_id))
        async with tenant_scoped_session(tenant_id) as db:
            receipt = await latest_workspace_receipt(db, tenant_id, context.agent_id, path)
            if receipt is None:
                return None  # Legacy carrier migration remains separate; no receipt is invented.
            if not isinstance(scope, WorkspaceAuthorityScope) or scope.agent_id != context.agent_id or scope.user_id != context.user_id:
                raise ContentInputReceiptError("content_source_access_denied", "The runtime file scope does not match.", 403)
            await validate_runtime_input_dependencies(db, request, receipt.dependencies_json)
            # This hash is checked against the very bytes returned by the file reader.
            scope = context.workspace_authority_scope
            hashes = dict(scope.input_content_hashes)
            hashes[path] = receipt.content_sha256
            context.workspace_authority_scope = replace(scope, input_content_hashes=frozenset(hashes.items()))
        return None
    except (ContentInputReceiptError, OSError, ValueError, SQLAlchemyError) as exc:
        status = exc.status_code if isinstance(exc, ContentInputReceiptError) else 503
        code = exc.code if isinstance(exc, ContentInputReceiptError) else "content_source_unavailable"
        return render_tool_error(tool_name=request.tool_name,
            error_class="auth_or_permission" if status == 403 else "provider_unavailable",
            message="This file's source receipt cannot authorize model consumption.", provider="content_input_receipt",
            http_status=status, retryable=status == 503,
            actionable_hint="Restore the scoped source grant or regenerate from current authorized inputs.",
            extra={"reason_code": code, "outcome": "denied" if status == 403 else "unavailable"})


async def validate_team_memory_receipt(db, tenant_id: uuid.UUID, user, workspace_key: str, entry, receipts: dict):
    receipt = receipts.get(team_memory_ref(workspace_key, Path(entry.path).stem))
    if receipt is None or receipt.revision != entry.revision or receipt.content_sha256 != entry.checksum:
        raise ContentInputReceiptError("team_memory_unverified", "This entry has no matching current content receipt. Preserve it and regenerate a new entry from authorized inputs.")
    await validate_browser_input_dependencies(db, tenant_id, user, receipt.dependencies_json)
    return receipt


def record_team_memory_receipt(db, tenant_id: uuid.UUID, workspace_key: str, entry, dependencies: list) -> None:
    unique = {(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies}
    db.add(ContentInputReceipt(tenant_id=tenant_id, carrier_kind="team_memory",
        carrier_ref=team_memory_ref(workspace_key, Path(entry.path).stem), revision=entry.revision,
        content_sha256=entry.checksum, dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in sorted(unique)]))


def workspace_artifact_ref(agent_id: uuid.UUID, path: str) -> str:
    from app.core.resource_authority import normalize_workspace_resource_path

    normalized = normalize_workspace_resource_path(path)
    if not normalized.startswith("workspace/"):
        raise ContentInputReceiptError("workspace_content_unverified", "Only workspace files carry content receipts.")
    return f"{agent_id}\n{normalized}"


async def lock_workspace_receipt_write(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace-content:{tenant_id}:{workspace_artifact_ref(agent_id, path)}"})


async def latest_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str):
    return await db.scalar(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "workspace_artifact",
        ContentInputReceipt.carrier_ref == workspace_artifact_ref(agent_id, path),
    ).order_by(ContentInputReceipt.revision.desc()).limit(1))


async def validate_browser_workspace_receipt(db, tenant_id: uuid.UUID, user, receipt, content_sha256: str) -> None:
    if receipt is None or receipt.content_sha256 != content_sha256:
        raise ContentInputReceiptError("workspace_content_unverified",
            "The file has no matching verified content receipt. Preserve it and regenerate a new file from authorized inputs.")
    dependencies = receipt.dependencies_json
    if not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        raise ContentInputReceiptError("workspace_content_unverified", "The file's input receipt is invalid.")
    await validate_browser_input_dependencies(db, tenant_id, user, dependencies)


def record_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str, content_sha256: str, previous):
    # Authenticated human input adds no authority to the previous source audience.
    dependencies = previous.dependencies_json if previous is not None else []
    unique = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies})
    receipt = ContentInputReceipt(tenant_id=tenant_id, carrier_kind="workspace_artifact",
        carrier_ref=workspace_artifact_ref(agent_id, path), revision=previous.revision + 1 if previous else 1,
        content_sha256=content_sha256,
        dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in unique])
    db.add(receipt)
    return receipt


async def persist_workspace_execution_evidence(*, context, tool_name, tool_call_id, arguments, result, states, lineage, errors):
    """Persist mechanical executor facts; this is not an input-authority receipt."""
    from datetime import datetime, timezone
    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_tool_runtime import _sha256

    if not all((context.tenant_id, context.session_id, context.runtime_task_id)):
        return {"status": "unavailable", "reason_code": "workspace_execution_context_missing"}
    try:
        tenant_id, session_id, run_id = (uuid.UUID(str(value)) for value in (
            context.tenant_id, context.session_id, context.runtime_task_id))
        args_hash = _sha256({"tool_name": tool_name, "arguments": arguments})
        async with tenant_scoped_session(tenant_id) as db:
            session = await db.get(ChatSession, session_id)
            if session is None or session.agent_id != context.agent_id or session.tenant_id != tenant_id:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The executor session does not match.")
            invocation = (await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == tenant_id, SessionToolInvocation.session_id == session_id,
                SessionToolInvocation.run_id == run_id, SessionToolInvocation.provider_tool_use_id == tool_call_id,
                SessionToolInvocation.tool_name == tool_name, SessionToolInvocation.result_event_id.is_(None),
            ))).one_or_none()
            if invocation is None or (invocation.effective_args_hash or invocation.args_hash) != args_hash:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The canonical invocation does not match.")
            model = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.tenant_id == tenant_id, SessionModelResult.session_id == session_id,
                SessionModelResult.run_id == run_id, SessionModelResult.provider_request_id == invocation.provider_request_id,
            ))
            if model is None:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The canonical model request is missing.")
            proof = {"schema": "hive.workspace_execution_evidence.v1", "invocation_id": str(invocation.id),
                "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
                "effective_args_hash": args_hash, "authority_snapshot_hash": invocation.authority_snapshot_hash,
                "tool_result_hash": _sha256(str(result)), "states": states, "lineage": lineage, "errors": errors}
            evidence_id = uuid.uuid5(invocation.id, "workspace-output-evidence")
            existing = await db.get(InvocationSpan, evidence_id)
            if existing is not None:
                if existing.metadata_json.get("workspace_execution_evidence") != proof:
                    raise ContentInputReceiptError("workspace_execution_evidence_changed", "Executor evidence is immutable.")
            else:
                db.add(InvocationSpan(id=evidence_id, tenant_id=tenant_id, agent_id=context.agent_id,
                    user_id=context.user_id, runtime_task_id=run_id, session_id=str(session_id),
                    trace_id=f"session-tool:{invocation.id}", span_id=f"workspace-output:{invocation.id}",
                    span_type="workspace_output", name=tool_name, status="captured", duration_ms=0,
                    started_at=datetime.now(timezone.utc), input_hash=args_hash,
                    metadata_json={"workspace_execution_evidence": proof}))
                await db.commit()
            return {"status": "captured", "receipt_ref": f"workspace-output:{evidence_id}"}
    except ContentInputReceiptError as exc:
        return {"status": "unavailable", "reason_code": exc.code}
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("Workspace execution evidence unavailable (%s)", type(exc).__name__)
        # A failed evidence write cannot undo a file effect or authorize its replay.
        return {"status": "unavailable", "reason_code": "workspace_execution_evidence_unavailable"}


async def load_workspace_execution_evidence(db, *, agent_id, invocation, model_result, arguments, content_hash):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_tool_runtime import _sha256

    evidence_id = uuid.uuid5(invocation.id, "workspace-output-evidence")
    row = await db.get(InvocationSpan, evidence_id)
    proof = (row.metadata_json or {}).get("workspace_execution_evidence") if row is not None else None
    if not isinstance(proof, dict) or row.tenant_id != invocation.tenant_id or row.agent_id != agent_id or (
        row.session_id != str(invocation.session_id) or row.runtime_task_id != invocation.run_id
        or row.span_type != "workspace_output"
        or proof.get("schema") != "hive.workspace_execution_evidence.v1"
        or proof.get("invocation_id") != str(invocation.id)
        or proof.get("model_result_id") != str(model_result.id)
        or proof.get("model_request_hash") != model_result.model_request_hash
        or _sha256(model_result.model_request_snapshot_json) != model_result.model_request_hash
        or proof.get("authority_snapshot_hash") != invocation.authority_snapshot_hash
        or proof.get("effective_args_hash") != _sha256({"tool_name": invocation.tool_name, "arguments": arguments})
        or proof.get("tool_result_hash") != content_hash
    ):
        raise ContentInputReceiptError("workspace_execution_unverified", "The captured output cannot be bound to this invocation.")
    states = proof.get("states")
    lineage = proof.get("lineage")
    errors = proof.get("errors")
    if not isinstance(states, dict) or not isinstance(lineage, list) or not isinstance(errors, dict):
        raise ContentInputReceiptError("workspace_execution_unverified", "The captured output evidence is invalid.")
    hashes = {}
    for item in lineage:
        if not isinstance(item, dict):
            continue
        path, after = item.get("path"), item.get("after_state")
        if not isinstance(path, str) or path in errors or not isinstance(after, dict) or after != states.get(path) or after.get("exists") is not True:
            continue
        digest = after.get("sha256")
        if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            if invocation.tool_name in {"write_file", "fs_write"} and isinstance(arguments.get("content"), str):
                if digest != hashlib.sha256(arguments["content"].encode()).hexdigest():
                    continue
            hashes[path] = digest
    return f"workspace-output:{evidence_id}", hashes
