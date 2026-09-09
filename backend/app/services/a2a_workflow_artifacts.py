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
    path, source = resolve_chat_artifact_file(artifact, workspace_root)
    if path is None or source != "delivery_snapshot":
        raise A2AArtifactUnavailable("immutable_artifact_snapshot_unavailable")
    with path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
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
    artifact = (await db.execute(query.order_by(ChatArtifact.created_at.desc()).limit(1))).scalar_one_or_none()
    if artifact is None:
        if not output.required and reference is None:
            return None
        raise A2AArtifactUnavailable(f"required_artifact_missing:{node_id}.{output.name}")
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
