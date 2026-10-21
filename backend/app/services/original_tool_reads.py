"""Read a named original through current native source authority and verified bytes."""

from dataclasses import replace
import json
from pathlib import Path
import uuid

from fastapi import HTTPException

from app.database import tenant_scoped_session
from app.services.blob_store import BlobStoreError
from app.tools.result_envelope import ToolContentEnvelope, render_tool_error


async def read_original_for_tool(workspace, arguments, tenant_id, *, execution_context, tool_name, document=False):
    from app.services.original_tool_sources import record_original_tool_result
    from app.services.content_input_receipts import ContentInputReceiptError

    result = await _read_original_for_tool(workspace, arguments, tenant_id, execution_context=execution_context,
        tool_name=tool_name, document=document)
    try:
        await record_original_tool_result(context=execution_context, arguments=arguments, tool_name=tool_name, result=result)
    except ContentInputReceiptError:
        result = ToolContentEnvelope(text=render_tool_error(tool_name=tool_name, error_class="auth_or_permission",
            message="The original source authority changed before its read could be recorded.", retryable=True),
            metadata={"original_read_status": "denied"})
        await record_original_tool_result(context=execution_context, arguments=arguments, tool_name=tool_name, result=result)
    return result


async def _read_original_for_tool(workspace, arguments, tenant_id, *, execution_context, tool_name, document=False):
    from app.services.agent_tool_domains.workspace import _IMAGE_MEDIA_TYPES, _MAX_IMAGE_BYTES, _read_file, _resolved_workspace_path
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.upload_originals import capture_original_body, resolve_original_download

    def error(kind, message, *, retryable=False):
        return ToolContentEnvelope(text=render_tool_error(tool_name=tool_name, error_class=kind, message=message, provider="original_attachment",
            retryable=retryable, actionable_hint="Select an authorized original, retry an unavailable source, or explicitly read the working copy."),
            metadata={"original_read_status": "denied" if kind == "auth_or_permission" else "unavailable"})

    try:
        ref_id = uuid.UUID(arguments["original_attachment_id"])
    except (ValueError, TypeError, AttributeError, KeyError):
        return error("bad_arguments", "original_attachment_id must be a recorded source UUID.")
    context = execution_context
    scope = getattr(context, "workspace_authority_scope", None)
    if (context is None or not tenant_id or str(context.tenant_id) != str(tenant_id) or context.user_id is None
        or scope is None or scope.agent_id != context.agent_id or scope.user_id != context.user_id or scope.operator_view
        or Path(context.workspace).resolve() != workspace.resolve()):
        return error("auth_or_permission", "Current native source authority is required.")
    path = arguments.get("path")
    if not isinstance(path, str) or _resolved_workspace_path(workspace, path, context.permission_profile) is None:
        return error("auth_or_permission", "The source path is outside the authorized workspace scope.")
    if document and arguments.get("return_format", "markdown") != "markdown":
        return error("bad_arguments", "Original document reads return complete Markdown. Use return_format='markdown'.")
    try:
        async with tenant_scoped_session(tenant_id, require_tenant=True, source="native_original_read") as db:
            from app.services.runtime_user_identity import load_scoped_runtime_user

            user = await load_scoped_runtime_user(db, context.user_id, tenant_id=uuid.UUID(str(tenant_id)))
            if user is None:
                return error("auth_or_permission", "The current source reader is unavailable.")
            principal = AgentRuntimePrincipal(agent_id=context.agent_id, requester_user_id=user.id,
                session_id=context.session_id, runtime_task_id=context.runtime_task_id)
            blob, metadata = await resolve_original_download(db, user, ref_id, runtime_principal=principal)
            if (metadata.get("agent_id") != str(context.agent_id)
                or (metadata.get("workspace_path") or metadata.get("path")) != path):
                return error("auth_or_permission", "The original does not belong to this employee and source path.")
        if (blob.state != "available" or blob.size_bytes != metadata.get("size_bytes")
            or blob.content_sha256 != metadata.get("content_sha256")):
            raise BlobStoreError("Original source requires reconciliation")
        if not document and Path(metadata["filename"]).suffix.lower() in _IMAGE_MEDIA_TYPES and blob.size_bytes > _MAX_IMAGE_BYTES:
            return error("operation_failed", f"Image too large for inline original reading (max {_MAX_IMAGE_BYTES} bytes).")
        # Commit source authorization before provider I/O; no audit/row locks
        # remain held while fetching or converting the immutable source.
        lease, target, filename = await capture_original_body(blob, metadata, uuid.UUID(str(tenant_id)))
        try:
            source = target.with_name("original" + Path(filename).suffix)
            target.rename(source)
            if document:
                from app.services.document_conversion import DocumentConversionRequest, DocumentConversionService

                result = DocumentConversionService().convert(DocumentConversionRequest(source_path=source,
                    workspace_root=source.parent, source_uri=f"original-attachment:{ref_id}", tenant_id=tenant_id,
                    agent_id=context.agent_id, user_id=context.user_id, mode="auto" if tool_name == "fs_read" else arguments.get("mode") or "auto",
                    max_pages=arguments.get("max_pages"), expected_source_sha256=metadata["content_sha256"])).markdown
            else:
                result = _read_file(source.parent, source.name, tenant_id, tool_name=tool_name, typed_errors=True)
                if isinstance(result, ToolContentEnvelope) and result.metadata.get("filesystem_read_error") is True:
                    return error("operation_failed", "The original source could not be rendered.", retryable=True)
            receipt = {"source_ref": f"original-attachment:{ref_id}", "reference_id": str(ref_id), "path": path,
                "content_sha256": metadata["content_sha256"], "size_bytes": metadata["size_bytes"]}
            # The source body is data, even if it contains executor-protocol tags.
            # This trusted success receipt describes the completed read only.
            content = result.text if isinstance(result, ToolContentEnvelope) else result
            wire = json.dumps({"ok": True, "original_attachment": receipt, "content": content},
                ensure_ascii=False, separators=(",", ":"))
            if isinstance(result, ToolContentEnvelope):
                return replace(result, text=wire, metadata={**result.metadata, "original_attachment": receipt})
            return ToolContentEnvelope(text=wire, metadata={"original_attachment": receipt})
        finally:
            lease.cleanup()
    except HTTPException as exc:
        return error("not_found" if exc.status_code == 404 else "auth_or_permission", "The original source is not currently accessible.")
    except (BlobStoreError, OSError):
        return error("provider_unavailable", "The original source is unavailable or failed integrity verification.", retryable=True)
    except Exception:
        return error("operation_failed", "The original source could not be read or converted.", retryable=True)
