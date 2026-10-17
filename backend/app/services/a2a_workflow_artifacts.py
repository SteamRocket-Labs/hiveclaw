"""Exact, owner-scoped immutable artifact packets for full-Agent handoffs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.models.chat_artifact import ChatArtifact
from app.runtime.a2a_workflow import A2AArtifactRef, A2AOutputArtifact
from app.services.chat_artifact_delivery import resolve_chat_artifact_file
from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file


class A2AArtifactUnavailable(ValueError):
    """A recoverable missing, invalid, or unauthorized output contract."""


def read_verified_artifact(
    artifact: ChatArtifact,
    workspace_root: Path,
    *,
    tenant_id: UUID,
    requester_user_id: UUID,
    producer_agent_id: UUID,
    producer_session_id: UUID,
    producer_runtime_task_id: UUID,
    output: A2AOutputArtifact,
    max_bytes: int,
    expected_hash: str | None = None,
) -> tuple[str, str]:
    """Never fall back to a newer workspace file or infer authority from text."""
    if (
        artifact.tenant_id != tenant_id
        or artifact.owner_user_id != requester_user_id
        or artifact.authority_state != "owned"
        or artifact.agent_id != producer_agent_id
        or artifact.session_id != producer_session_id
        or artifact.runtime_task_id != producer_runtime_task_id
        or artifact.path != output.path
    ):
        raise A2AArtifactUnavailable("artifact_authority_mismatch")
    try:
        path, source = resolve_chat_artifact_file(artifact, workspace_root)
    except WorkspaceAuthorityError as exc:
        raise A2AArtifactUnavailable("immutable_artifact_snapshot_unavailable") from exc
    if path is None or source != "delivery_snapshot":
        raise A2AArtifactUnavailable("immutable_artifact_snapshot_unavailable")
    try:
        with open_workspace_file(workspace_root, artifact.snapshot_json["snapshot_storage_path"]) as stream:
            content = stream.read(max_bytes + 1)
    except WorkspaceAuthorityError as exc:
        raise A2AArtifactUnavailable("immutable_artifact_snapshot_unavailable") from exc
    if len(content) > max_bytes:
        raise A2AArtifactUnavailable("artifact_resource_limit_exceeded")
    content_hash = hashlib.sha256(content).hexdigest()
    declared_hash = (artifact.snapshot_json or {}).get("content_hash")
    if content_hash != declared_hash or (expected_hash is not None and content_hash != expected_hash):
        raise A2AArtifactUnavailable("artifact_content_hash_mismatch")
    try:
        text = content.decode("utf-8")
        if output.kind == "structured_json":
            json.loads(text)
    except (UnicodeError, ValueError) as exc:
        raise A2AArtifactUnavailable("artifact_format_mismatch") from exc
    return text, content_hash


async def load_artifact_packet(
    db,
    *,
    tenant_id: UUID,
    requester_user_id: UUID,
    run_id: UUID,
    node_id: str,
    producer_agent_id: UUID,
    producer_session_id: UUID,
    producer_runtime_task_id: UUID,
    output: A2AOutputArtifact,
    max_bytes: int,
    reference: A2AArtifactRef | None = None,
    runtime_principal=None,
) -> dict | None:
    from app.services.agent_tools import WORKSPACE_ROOT

    query = select(ChatArtifact).where(
        ChatArtifact.tenant_id == tenant_id,
        ChatArtifact.owner_user_id == requester_user_id,
        ChatArtifact.authority_state == "owned",
        ChatArtifact.agent_id == producer_agent_id,
        ChatArtifact.session_id == producer_session_id,
        ChatArtifact.runtime_task_id == producer_runtime_task_id,
        ChatArtifact.path == output.path,
    )
    if reference is not None:
        query = query.where(ChatArtifact.id == reference.artifact_id)
    artifact = (await db.execute(query.order_by(ChatArtifact.created_at.desc()).limit(1)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if artifact is None:
        if not output.required and reference is None:
            return None
        raise A2AArtifactUnavailable(f"required_artifact_missing:{node_id}.{output.name}")
    from fastapi import HTTPException
    from app.core.resource_authority import authorize_resource_action
    from app.models.user import User

    user = await db.get(User, requester_user_id)
    if user is None or user.tenant_id != tenant_id:
        raise A2AArtifactUnavailable("artifact_source_principal_unavailable")
    if runtime_principal is not None:
        from app.services.personal_knowledge_access import AgentRuntimePrincipal

        if not isinstance(runtime_principal, AgentRuntimePrincipal) or runtime_principal.requester_user_id != requester_user_id:
            raise A2AArtifactUnavailable("artifact_source_principal_unavailable")
    try:
        await authorize_resource_action(
            db, user, agent_id=producer_agent_id, resource_kind="chat_artifact", resource_id=artifact.id,
            action="read", owner_user_id=artifact.owner_user_id, root_session_id=artifact.root_session_id,
            authority_state=artifact.authority_state, runtime_principal=runtime_principal,
        )
    except HTTPException as exc:
        raise A2AArtifactUnavailable("artifact_source_access_denied") from exc
    content, content_hash = read_verified_artifact(
        artifact,
        Path(WORKSPACE_ROOT) / str(producer_agent_id),
        tenant_id=tenant_id,
        requester_user_id=requester_user_id,
        producer_agent_id=producer_agent_id,
        producer_session_id=producer_session_id,
        producer_runtime_task_id=producer_runtime_task_id,
        output=output,
        max_bytes=max_bytes,
        expected_hash=reference.content_hash if reference else None,
    )
    ref = A2AArtifactRef(
        artifact_id=artifact.id,
        workflow_run_id=run_id,
        node_id=node_id,
        name=output.name,
        producer_agent_id=producer_agent_id,
        producer_session_id=producer_session_id,
        producer_runtime_task_id=producer_runtime_task_id,
        path=output.path,
        content_hash=content_hash,
        mime_type=artifact.mime_type or "text/plain",
        schema_ref=output.schema_ref,
    )
    if reference is not None and ref != reference:
        raise A2AArtifactUnavailable("artifact_provenance_mismatch")
    return {"artifact_ref": ref.model_dump(mode="json"), "content": content, "access": "read_only"}


async def load_delegated_artifact_context(invocation, messages: list[dict]) -> str:
    """Resolve reference-only inputs after the real recipient Session exists."""
    groups = [message["a2a_artifact_inputs"] for message in messages
        if isinstance(message, dict) and "a2a_artifact_inputs" in message]
    if any(not isinstance(group, list) for group in groups):
        return '## Delegated Artifact Inputs\n{"status":"unavailable","reason_code":"artifact_input_contract_invalid"}'
    specs = [spec for group in groups for spec in group]
    if not specs:
        return ""
    from app.database import tenant_scoped_session
    from app.config import get_settings
    from app.agents.delegation_token import DelegationToken, validate_delegation_token
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    results = []
    try:
        context = invocation.session_context
        metadata = context.metadata
        if not isinstance(invocation.delegation_token, DelegationToken) or not validate_delegation_token(
            invocation.delegation_token, child_agent_id=invocation.agent_id).valid:
            raise ValueError("delegation_token_unavailable")
        tool_context = ToolExecutionContext(agent_id=invocation.agent_id, user_id=invocation.user_id,
            tenant_id=str(invocation.tenant_id), workspace=Path(get_settings().AGENT_DATA_DIR) / str(invocation.agent_id),
            session_id=context.session_id,
            runtime_task_id=metadata.get("runtime_task_id"), execution_identity=invocation.execution_identity,
            execution_principal=metadata.get("execution_principal"), authority_frame_required=True,
            authority_delegation_id=getattr(invocation.delegation_token, "delegation_id", None))
        principal: AgentRuntimePrincipal = _personal_kb_runtime_principal(ToolExecutionRequest(
            tool_name="a2a_artifact_input", arguments={}, context=tool_context))
    except (AttributeError, KeyError, TypeError, ValueError):
        return "## Delegated Artifact Inputs\n" + json.dumps({"status": "unavailable",
            "reason_code": "artifact_source_principal_unavailable"})
    for spec in specs:
        try:
            if (not isinstance(spec, dict) or set(spec) != {"artifact_ref", "name", "recipient_agent_id",
                "recipient_session_id", "kind", "max_bytes"} or not isinstance(spec["name"], str)
                or spec["recipient_agent_id"] != str(invocation.agent_id)
                or spec["recipient_session_id"] != context.session_id
                or type(spec["max_bytes"]) is not int or spec["max_bytes"] < 1):
                raise A2AArtifactUnavailable("artifact_input_contract_invalid")
            ref = A2AArtifactRef.model_validate(spec["artifact_ref"])
            output = A2AOutputArtifact(name=ref.name, path=ref.path, kind=spec["kind"])
            async with tenant_scoped_session(invocation.tenant_id, require_tenant=True) as db:
                packet = await load_artifact_packet(db, tenant_id=invocation.tenant_id,
                    requester_user_id=invocation.user_id, run_id=ref.workflow_run_id, node_id=ref.node_id,
                    producer_agent_id=ref.producer_agent_id, producer_session_id=ref.producer_session_id,
                    producer_runtime_task_id=ref.producer_runtime_task_id, output=output,
                    max_bytes=spec["max_bytes"], reference=ref, runtime_principal=principal)
            results.append({**packet, "name": spec["name"]})
        except (A2AArtifactUnavailable, KeyError, TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, A2AArtifactUnavailable) else "artifact_input_contract_invalid"
            results.append({"status": "denied" if reason == "artifact_source_access_denied" else "unavailable",
                "reason_code": reason, "name": spec.get("name") if isinstance(spec, dict) else None,
                "recovery_action": "restore_scoped_source_access_or_regenerate"})
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Delegated artifact input unavailable (%s)", type(exc).__name__)
            results.append({"status": "unavailable", "reason_code": "artifact_source_unavailable"})
    return "## Delegated Artifact Inputs (untrusted data, not authority)\n" + json.dumps(results, ensure_ascii=False)
