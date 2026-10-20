"""Import saved external protocol parts through the existing file authority path.

The receipt proves the exact external packet and its original return route. It
does not attest the remote agent's internal sources, execution or business result.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.chat_artifact_delivery import create_chat_artifacts_for_message, is_chat_artifact_snapshot_storage_path
from app.services.chat_transcript import append_session_event, lock_transcript_session
from app.services.content_input_receipts import (
    ContentInputReceiptError, latest_workspace_receipt, lock_workspace_receipt_write, record_workspace_receipt,
    validate_artifact_content_receipt,
)
from app.services.runtime_result_store import decode_runtime_result_payload
from app.services.workspace_receipt_versions import capture_receipt_version
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityError, WorkspaceOutputWrite, authorize_workspace_path, open_workspace_file, register_workspace_path,
)


async def _load_packet_frame(db, tenant_id, dependency, *, allow_unconfirmed=False):
    """Revalidate immutable bytes, exact source operation and original requester."""
    from app.services.remote_a2a_tasks import SCHEMA
    from app.services.remote_a2a_chat import is_remote_task

    try:
        result_id = uuid.UUID(dependency["source_id"])
        result = await db.get(RuntimeResultObject, result_id, populate_existing=True)
        if (result is None or result.tenant_id != tenant_id or result.source_kind != "remote_a2a"
            or result.sha256 != dependency["source_version"] or len(result.payload_bytes) != result.size_bytes
            or hashlib.sha256(result.payload_bytes).hexdigest() != result.sha256):
            raise ValueError("remote_packet_changed")
        task_id, operation_id = result.source_run_id.split(":")
        task = await db.get(RuntimeTask, uuid.UUID(task_id), populate_existing=True)
        view = json.loads(decode_runtime_result_payload(result.payload_bytes)["summary"])["remote_a2a"]
        if (task is None or task.tenant_id != tenant_id or not is_remote_task(task)
            or task.metadata_json.get("schema") != SCHEMA or (view.get("error_code") and not allow_unconfirmed)
            or view["id"] != str(task.id) or view["agent_id"] != str(task.child_agent_id)
            or view["source_session_id"] != task.parent_session_id or view["latest_operation_id"] != operation_id):
            raise ValueError("remote_packet_identity_mismatch")
        operation = next(op for op in task.metadata_json["remote_a2a"]["operations"] if op["id"] == operation_id)
        if (operation.get("observation") != view.get("operation_observation")
            or (not allow_unconfirmed and (operation["phase"] != "received"
                or not isinstance(view.get("operation_observation"), dict)))):
            raise ValueError("remote_packet_operation_mismatch")
        session = await db.get(ChatSession, uuid.UUID(task.parent_session_id), populate_existing=True)
        if (session is None or (session.tenant_id, session.agent_id) != (tenant_id, task.parent_agent_id)
            or (session.audience_kind != "group" and session.user_id != task.root_user_id)
            or str(session.root_session_id or session.id) != task.root_session_id):
            raise ValueError("remote_packet_work_mismatch")
        route = await db.scalar(select(RuntimeNotificationOutbox.id).where(
            RuntimeNotificationOutbox.tenant_id == tenant_id,
            RuntimeNotificationOutbox.source_kind == "remote_a2a",
            RuntimeNotificationOutbox.source_run_id == result.source_run_id,
            RuntimeNotificationOutbox.result_object_id == result.id,
            RuntimeNotificationOutbox.result_sha256 == result.sha256,
            RuntimeNotificationOutbox.result_size_bytes == result.size_bytes,
            RuntimeNotificationOutbox.parent_agent_id == task.parent_agent_id,
            RuntimeNotificationOutbox.parent_session_id == session.id,
            RuntimeNotificationOutbox.parent_user_id == task.root_user_id,
            RuntimeNotificationOutbox.root_runtime_task_id == task.root_runtime_task_id,
        ).limit(1))
        if route is None:
            raise ValueError("remote_packet_route_unavailable")
        if session.audience_kind == "group":
            from app.services.remote_a2a_chat import _session_principal

            await _session_principal(db, task=task, state=task.metadata_json["remote_a2a"], for_effect=False)
        return task, result, session, view, operation
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError) as exc:
        raise ContentInputReceiptError("remote_a2a_source_unavailable",
            "The exact remote packet, original work or requester cannot be verified.", 403) from exc


async def group_projection_actor(db, *, session, rows):
    """Bind saved remote returns to their requester without authorizing inference."""
    requester = None
    for row in rows:
        task, _result, original, _view, _op = await _load_packet_frame(db, session.tenant_id,
            {"source_id": str(row.result_object_id), "source_version": row.result_sha256}, allow_unconfirmed=True)
        if (row.source_kind != "remote_a2a" or row.delivery_mode != "session_projection"
            or original.id != session.id or row.parent_user_id != task.root_user_id
            or (requester is not None and requester.id != task.root_user_id)):
            raise ContentInputReceiptError("remote_a2a_source_unavailable", "The shared return route changed.", 403)
        requester = await db.get(User, task.root_user_id, populate_existing=True)
    if requester is None or not requester.is_active:
        raise ContentInputReceiptError("remote_a2a_source_unavailable", "The original requester is unavailable.", 403)
    return requester


async def group_event_ref(db, *, session, event, user):
    from app.services.remote_a2a_chat import reply_text, reply_message

    metadata = event.metadata_json or {}
    task, result, original, packet = await load_packet(db, session.tenant_id, metadata.get("remote_result"), user=user)
    if original.id != session.id or event.run_id != task.id:
        raise ContentInputReceiptError("remote_a2a_source_unavailable", "The returned event belongs to another work.", 403)
    if metadata.get("source") == "remote_a2a_chat" and (event.content != reply_text(packet)
        or metadata.get("remote_message") != reply_message(packet)):
        raise ContentInputReceiptError("remote_a2a_source_unavailable", "The returned message differs from its saved packet.", 403)
    if metadata.get("source") == "remote_a2a_return" and event.causation_id != result.id:
        raise ContentInputReceiptError("remote_a2a_source_unavailable", "The file return differs from its saved packet.", 403)
    return f"remote-chat-result:{result.id}"


async def _packet_reader(db, tenant_id, *, task, session, user=None, runtime_request=None):
    from app.services.content_input_receipts import _runtime_input_actor, _authorize_session_input_source

    if runtime_request is not None:
        principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
        if principal.agent_id != task.parent_agent_id or str(principal.session_id) != task.parent_session_id:
            raise ContentInputReceiptError("remote_a2a_source_unavailable", "The exact remote packet, original work or requester cannot be verified.", 403)
    reader = await db.get(User, user.id, populate_existing=True) if user is not None else None
    if (reader is None or not reader.is_active or reader.tenant_id != tenant_id
        or (session.audience_kind != "group" and reader.id != task.root_user_id)):
        raise ContentInputReceiptError("remote_a2a_source_unavailable", "The exact remote packet, original work or requester cannot be verified.", 403)
    if session.audience_kind == "group":
        await _authorize_session_input_source(db, tenant_id, session, user=reader, runtime_request=None)
    return reader


def _status_view(task, view, operation):
    if (not view.get('error_code') or operation.get('phase') not in {'not_started', 'unknown', 'received'}
        or view['status'] != task.status or view['error_code'] != task.metadata_json['remote_a2a'].get('error_code')):
        raise ContentInputReceiptError('remote_a2a_status_unavailable', 'No verified recovery status is available.', 403)


async def load_packet(db, tenant_id, dependency, *, user=None, runtime_request=None, allow_unconfirmed=False):
    task, result, session, view, operation = await _load_packet_frame(db, tenant_id, dependency,
        allow_unconfirmed=allow_unconfirmed)
    reader = await _packet_reader(db, tenant_id, task=task, session=session, user=user, runtime_request=runtime_request)
    from app.services.remote_a2a_delegation import validate_instruction_sources
    from fastapi import HTTPException

    try:
        state = task.metadata_json["remote_a2a"]
        # Later queued inputs do not change the causal inputs of this packet.
        end = state["operations"].index(operation) + 1
        # Reading the owner's saved packet does not authorize a new channel effect.
        await validate_instruction_sources(db, task=task,
            state={**state, "operations": state["operations"][:end]}, for_effect=False, reader=reader)
    except HTTPException as exc:
        raise ContentInputReceiptError("remote_a2a_instruction_source_unavailable",
            "The remote result's known local inputs are no longer available.", exc.status_code) from exc
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError) as exc:
        raise ContentInputReceiptError("remote_a2a_source_unavailable",
            "The exact remote packet, original work or requester cannot be verified.", 403) from exc
    return task, result, session, view["operation_observation"]


async def load_status_packet(db, tenant_id, dependency, *, user):
    """Verify host status and route without granting access to packet/input content."""
    task, result, session, view, operation = await _load_packet_frame(db, tenant_id, dependency,
        allow_unconfirmed=True)
    await _packet_reader(db, tenant_id, task=task, session=session, user=user)
    _status_view(task, view, operation)
    return task, result, session, view, operation


async def validate_remote_dependency(db, tenant_id, dependency, *, user=None, runtime_request=None):
    await load_packet(db, tenant_id, dependency, user=user, runtime_request=runtime_request)


def _inline(part):
    if (part.get("metadata") or {}).get("hiveRemoteContentStatus") == "credential_redacted":
        return None, "remote_a2a_credential_redacted"
    if "url" in part:
        return None, "remote_a2a_external_url_not_fetched"
    try:
        if "raw" in part:
            return base64.b64decode(part["raw"], validate=True), None
        if "text" in part:
            return part["text"].encode("utf-8"), None
        if "data" in part:
            return json.dumps(part["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False).encode("utf-8"), None
    except (ValueError, TypeError, AttributeError, binascii.Error):
        return None, "remote_a2a_inline_part_invalid"
    return None, "remote_a2a_part_content_unavailable"


def _filename(part):
    name = PurePosixPath(str(part.get("filename") or "").replace("\\", "/")).name
    if not name or name in {".", ".."} or any(ord(c) < 32 for c in name) or len(name.encode("utf-8")) > 160:
        name = "content.txt" if "text" in part else "content.json" if "data" in part else "content.bin"
    return name


def _write(root, path, body, observation):
    observation.expected_output_sha256 = hashlib.sha256(body).hexdigest()
    with open_workspace_file(root, path, mode="wb", write_observation=observation) as target:
        target.write(body)
        target.flush()
        os.fsync(target.fileno())
    if not observation.verified:
        raise WorkspaceAuthorityError("remote_a2a_file_write_unconfirmed", "The imported file write is unconfirmed.")


def _automatic_receipt(receipt, path):
    return receipt is not None and any(
        dep["source_kind"] == "remote_a2a_result" and receipt.id == uuid.uuid5(uuid.UUID(dep["source_id"]), path)
        for dep in receipt.dependencies_json
    )


async def _materialize(db, *, task, result, session, user, path, body, dependency):
    root = Path(get_settings().AGENT_DATA_DIR) / str(task.parent_agent_id)
    digest = hashlib.sha256(body).hexdigest()
    await lock_workspace_receipt_write(db, task.tenant_id, task.parent_agent_id, path)
    previous = await latest_workspace_receipt(db, task.tenant_id, task.parent_agent_id, path)
    observation = await asyncio.to_thread(WorkspaceOutputWrite.capture, root, path)
    conflict = (previous is not None and not _automatic_receipt(previous, path)) or (
        observation.expected_exists and observation.expected_predecessor_sha256 not in {
            digest, previous.content_sha256 if previous else None})
    if conflict:
        # Preserve both versions. External updates never overwrite a human edit.
        path = f"workspace/remote-a2a/{task.id}/conflicts/{result.id}/{PurePosixPath(path).parent.name}/{PurePosixPath(path).name}"
        await lock_workspace_receipt_write(db, task.tenant_id, task.parent_agent_id, path)
        previous = await latest_workspace_receipt(db, task.tenant_id, task.parent_agent_id, path)
        observation = await asyncio.to_thread(WorkspaceOutputWrite.capture, root, path)
        if observation.expected_exists and observation.expected_predecessor_sha256 != digest:
            raise WorkspaceAuthorityError("remote_a2a_file_conflict", "Preserve the edited returned file before retrying.")
    await register_workspace_path(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id, path=path,
        owner_user_id=user.id, root_session_id=session.root_session_id or session.id,
        source="remote_a2a_return", content_hash=digest, allow_owner_rebind=False)
    await authorize_workspace_path(db, user, agent_id=task.parent_agent_id, path=path, action="write",
        path_exists=observation.expected_exists, session_id=session.id, for_update=True)
    if observation.expected_predecessor_sha256 != digest:
        await asyncio.to_thread(_write, root, path, body, observation)
    receipt = record_workspace_receipt(db, task.tenant_id, task.parent_agent_id, path, digest, previous,
        additional_dependencies=[dependency])
    receipt.id = uuid.uuid5(result.id, path)
    await db.flush()
    await asyncio.to_thread(capture_receipt_version, task.parent_agent_id, path, receipt)
    return path, receipt, conflict


async def import_packet(db, *, tenant_id, result_id, sha256, user):
    dependency = {"source_kind": "remote_a2a_result", "source_id": str(result_id), "source_version": sha256}
    task, result, session, observation = await load_packet(db, tenant_id, dependency, user=user)
    await lock_transcript_session(db, session_id=session.id)
    existing = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == tenant_id, ChatTranscriptEvent.session_id == session.id,
        ChatTranscriptEvent.causation_id == result.id, ChatTranscriptEvent.event_type == "remote_a2a_artifacts"))
    if existing is not None:
        await _recover_snapshots(db, task=task, result=result, observation=observation, event=existing, user=user)
        return {"parts": existing.parts_json or [], "file_returns": existing.metadata_json["file_returns"]}
    artifacts = observation.get("task", observation).get("artifacts") or []
    if not artifacts:
        return {"parts": [], "file_returns": []}
    identities = [(artifact.get("artifactId") or index, part_index)
        for index, artifact in enumerate(artifacts) for part_index, _part in enumerate(artifact.get("parts") or [])]
    if len(identities) != len(set(identities)):
        raise ContentInputReceiptError("remote_a2a_artifact_identity_conflict",
            "The saved packet repeats an artifact identity. Request distinct artifact identities.", 409)
    message_id = uuid.uuid5(result.id, "remote-artifact-return")
    anchor = ChatMessage(id=message_id, tenant_id=tenant_id, agent_id=task.parent_agent_id,
        user_id=user.id, role="system", content="Remote artifact return", conversation_id=str(session.id))
    db.add(anchor)
    await db.flush()
    parts, returns = [], []
    for artifact_index, artifact in enumerate(artifacts):
        for part_index, part in enumerate(artifact.get("parts") or []):
            key = json.dumps([artifact.get("artifactId") or artifact_index, part_index], separators=(",", ":"))
            identity = hashlib.sha256(key.encode()).hexdigest()
            body, reason = _inline(part)
            record = {"artifact_id": artifact.get("artifactId"), "artifact_index": artifact_index, "part_index": part_index,
                "result_ref": f"runtime-result://{result.id}/{result.sha256}"}
            if reason:
                returns.append({**record, "status": "unavailable", "reason_code": reason,
                    "recovery_action": "request_inline_artifact"})
                continue
            path = f"workspace/remote-a2a/{task.id}/{identity}/{_filename(part)}"
            path, receipt, conflict = await _materialize(db, task=task, result=result, session=session, user=user,
                path=path, body=body, dependency=dependency)
            errors = []
            created = await create_chat_artifacts_for_message(db=db, agent_id=task.parent_agent_id,
                tenant_id=tenant_id, session_id=session.id, message_id=message_id, runtime_task_id=task.id,
                paths=[path], workspace_root=Path(get_settings().AGENT_DATA_DIR) / str(task.parent_agent_id),
                source="remote_a2a_return", action="created" if receipt.revision == 1 else "updated",
                source_validation_session=db if session.audience_kind == "group" else None,
                source_agent_id=task.child_agent_id, download_agent_id=task.parent_agent_id,
                source_receipt_ids={path: receipt.id}, expected_content_hashes={path: receipt.content_sha256},
                publication_errors=errors)
            if errors or not created:
                raise ContentInputReceiptError("remote_a2a_artifact_publication_unavailable",
                    "The saved remote file could not be published. Retry the saved result delivery.", 503)
            parts.extend(created)
            returns.append({**record, "status": "preserved_conflict" if conflict else "available", "path": path,
                "content_sha256": receipt.content_sha256, "content_input_receipt_id": str(receipt.id)})
    content = f"Returned {len(parts)} file(s). Unavailable parts: {sum(row['status'] == 'unavailable' for row in returns)}."
    anchor.content = content
    await append_session_event(db=db, agent_id=task.parent_agent_id, tenant_id=tenant_id, session_id=session.id,
        actor_type="system", event_type="remote_a2a_artifacts", role="system", content=content,
        user_id=user.id, runtime_task_id=task.id, message_id=message_id, materialize_chat_message=False,
        root_session_id=session.root_session_id or session.id, parts=parts,
        metadata={"causation_id": str(result.id), "file_returns": returns, "remote_untrusted_data": True,
            "remote_result": dependency},
        source="remote_a2a_return")
    return {"parts": parts, "file_returns": returns}


def _recover_snapshot(root, path, body):
    try:
        with open_workspace_file(root, path) as saved:
            if hashlib.file_digest(saved, "sha256").hexdigest() != hashlib.sha256(body).hexdigest():
                raise ContentInputReceiptError("remote_a2a_snapshot_changed", "Preserve the changed snapshot before recovery.", 409)
    except WorkspaceAuthorityError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        # Recovery uses the exact sealed external packet, never current workspace bytes.
        with open_workspace_file(root, path, mode="xb") as restored:
            restored.write(body)
            restored.flush()
            os.fsync(restored.fileno())


async def _recover_snapshots(db, *, task, result, observation, event, user):
    root = Path(get_settings().AGENT_DATA_DIR) / str(task.parent_agent_id)
    by_path = {record["path"]: record for record in event.metadata_json["file_returns"] if record.get("path")}
    for part in event.parts_json or []:
        artifact = await db.get(ChatArtifact, uuid.UUID(part["artifact_id"]), populate_existing=True)
        record = by_path[part["path"]]
        source_part = observation.get("task", observation)["artifacts"][record["artifact_index"]]["parts"][record["part_index"]]
        body, reason = _inline(source_part)
        snapshot = artifact.snapshot_json or {} if artifact else {}
        if (artifact is None or reason or (artifact.tenant_id, artifact.agent_id, artifact.session_id, artifact.runtime_task_id,
            artifact.message_id, artifact.path, artifact.source) != (task.tenant_id, task.parent_agent_id, event.session_id,
            task.id, event.message_id, part["path"], "remote_a2a_return")
            or snapshot.get("content_input_receipt_id") != record["content_input_receipt_id"]
            or snapshot.get("content_hash") != hashlib.sha256(body).hexdigest() or artifact.size != len(body)
            or record["result_ref"] != f"runtime-result://{result.id}/{result.sha256}"
            or not is_chat_artifact_snapshot_storage_path(snapshot.get("snapshot_storage_path", ""))):
            raise ContentInputReceiptError("remote_a2a_snapshot_unavailable", "The exact returned file cannot be recovered.", 409)
        await authorize_workspace_path(db, user, agent_id=task.parent_agent_id, path=artifact.path,
            action="read", path_exists=True, session_id=event.session_id)
        await validate_artifact_content_receipt(db, task.tenant_id, user, artifact)
        await asyncio.to_thread(_recover_snapshot, root, snapshot["snapshot_storage_path"], body)


async def import_page_artifacts(db, *, page, user):
    for item in page.items:
        if item.source_kind != "remote_a2a":
            continue
        result = await db.get(RuntimeResultObject, item.result_object_id)
        view = json.loads(decode_runtime_result_payload(result.payload_bytes)["summary"])["remote_a2a"]
        parts = []
        if view.get("error_code") is None:
            receipt = await import_packet(db, tenant_id=page.tenant_id, result_id=item.result_object_id,
                sha256=item.result_sha256, user=user)
            parts = receipt["parts"]
        from app.services.remote_a2a_channel import deliver_packet

        task = await db.get(RuntimeTask, uuid.UUID(view["id"]))
        await deliver_packet(db, task=task, result=result, view=view, parts=parts)


async def recover_task_artifacts(db, *, agent_id, task_id, actor, expected_user_id, expected_tenant_id):
    """Import historical saved returns, including an already delivered older page."""
    from app.services.remote_a2a_connections import _frame
    from app.services.remote_a2a_tasks import get_task

    task = await get_task(db, agent_id=agent_id, task_id=task_id, actor=actor, lock=True)
    _frame(task, actor, expected_user_id, expected_tenant_id)
    sources = [f"{task.id}:{op['id']}" for op in task.metadata_json["remote_a2a"]["operations"]]
    results = list((await db.scalars(select(RuntimeResultObject).join(RuntimeNotificationOutbox,
        RuntimeNotificationOutbox.result_object_id == RuntimeResultObject.id).where(
        RuntimeResultObject.tenant_id == task.tenant_id, RuntimeResultObject.source_kind == "remote_a2a",
        RuntimeResultObject.source_run_id.in_(sources), RuntimeNotificationOutbox.tenant_id == task.tenant_id,
        RuntimeNotificationOutbox.parent_session_id == uuid.UUID(task.parent_session_id),
        RuntimeNotificationOutbox.parent_user_id == actor.id, RuntimeNotificationOutbox.parent_agent_id == task.parent_agent_id,
    ).order_by(RuntimeNotificationOutbox.mailbox_sequence))).unique())
    parts, returns = [], []
    for result in results:
        view = json.loads(decode_runtime_result_payload(result.payload_bytes)["summary"])["remote_a2a"]
        if view.get("error_code"):
            returns.append({"status": "unavailable", "reason_code": view["error_code"],
                "recovery_action": "reconcile_remote_task"})
            continue
        receipt = await import_packet(db, tenant_id=task.tenant_id, result_id=result.id, sha256=result.sha256, user=actor)
        parts.extend(receipt["parts"])
        returns.extend(receipt["file_returns"])
    await db.commit()
    return {"agent_id": str(agent_id), "task_id": str(task.id), "source_session_id": task.parent_session_id,
        "parts": parts, "file_returns": returns}
