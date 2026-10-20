"""Transfer canonical conversation files without borrowing path authority."""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import mimetypes
from pathlib import Path
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.core.resource_authority import normalize_workspace_resource_path
from app.models.invocation_span import InvocationSpan
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionTurnInput
from app.services.content_input_receipts import (
    ContentInputReceiptError, _validate_input_dependencies, latest_workspace_receipt, workspace_artifact_ref,
)
from app.services.remote_a2a_tasks import _digest
from app.services.session_workspace_snapshot import async_agent_workspace_lock
from app.services.workspace_receipt_versions import capture_receipt_version, project_receipt_version
from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file


def _unavailable():
    return ContentInputReceiptError('remote_a2a_attachment_source_unavailable',
        'The attachment has no exact authorized file version. Restore its source or select a verified file.', 409)


async def _file(db, *, task, principal, row, part_index, attachment):
    if not isinstance(attachment, dict) or attachment.get('state') == 'unavailable':
        raise _unavailable()
    path = attachment.get('path')
    try:
        if not isinstance(path, str) or normalize_workspace_resource_path(path) != path or not path.startswith('workspace/'):
            raise ValueError('noncanonical attachment path')
    except (ValueError, TypeError) as exc:
        raise _unavailable() from exc
    span_id = uuid.uuid5(row.id, f'remote-attachment:{row.revision}:{part_index}')
    span = await db.get(InvocationSpan, span_id, populate_existing=True)
    key = {'input_id': str(row.id), 'input_revision': row.revision, 'part_index': part_index,
        'input_sha256': _digest(row.content_parts_json), 'path': path}
    if span is not None:
        proof = (span.metadata_json or {}).get('attachment')
        captured_principal = {**principal.evidence(), 'runtime_task_id': str(span.runtime_task_id)}
        if (span.tenant_id != task.tenant_id or span.agent_id != task.parent_agent_id
            or span.user_id != task.root_user_id or span.session_id != task.parent_session_id
            or span.span_type != 'remote_chat_attachment' or span.status != 'captured'
            or not isinstance(proof, dict) or proof.get('schema') != 'hive.remote_chat_attachment.v1'
            or any(proof.get(name) != value for name, value in key.items())
            or proof.get('principal') != captured_principal
            or proof.get('sha256') != _digest({name: value for name, value in proof.items() if name != 'sha256'})):
            raise _unavailable()
        dependency = proof.get('source')
        try:
            receipt_id = uuid.UUID(dependency['source_id'])
        except (ValueError, TypeError, KeyError) as exc:
            raise _unavailable() from exc
        receipt = await db.get(ContentInputReceipt, receipt_id, populate_existing=True)
    else:
        receipt = await latest_workspace_receipt(db, task.tenant_id, task.parent_agent_id, path)
        if receipt is None:
            raise _unavailable()
        dependency = {'source_kind': 'workspace_artifact', 'source_id': str(receipt.id),
            'source_version': receipt.content_sha256}
    if (receipt is None or receipt.tenant_id != task.tenant_id or receipt.carrier_kind != 'workspace_artifact'
        or receipt.carrier_ref != workspace_artifact_ref(task.parent_agent_id, path)
        or dependency != {'source_kind': 'workspace_artifact', 'source_id': str(receipt.id),
            'source_version': receipt.content_sha256}):
        raise _unavailable()
    await _validate_input_dependencies(db, task.tenant_id, [dependency], runtime_request=principal)

    if span is None:
        await asyncio.to_thread(capture_receipt_version, task.parent_agent_id, path, receipt)
    version = await asyncio.to_thread(project_receipt_version, task.parent_agent_id, path, receipt, require_available=True)
    root = Path(get_settings().AGENT_DATA_DIR) / str(task.parent_agent_id)
    def read_body():
        with open_workspace_file(root, version.content_path.relative_to(root).as_posix()) as source:
            return source.read()
    body = await asyncio.to_thread(read_body)
    if hashlib.sha256(body).hexdigest() != receipt.content_sha256:
        raise _unavailable()
    if span is None:
        proof = {'schema': 'hive.remote_chat_attachment.v1', **key,
            'principal': principal.evidence(), 'source': dependency}
        proof['sha256'] = _digest(proof)
        db.add(InvocationSpan(id=span_id, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            user_id=task.root_user_id, session_id=task.parent_session_id, runtime_task_id=task.id,
            trace_id=f'remote-attachment:{row.id}', span_id=str(span_id), span_type='remote_chat_attachment',
            name='attachment_input', status='captured', started_at=datetime.now(timezone.utc),
            metadata_json={'attachment': proof}))
    filename = Path(path).name
    media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    binding = {**key, 'source': dependency, 'filename': filename, 'media_type': media_type, 'size': len(body)}
    return binding, {'raw': base64.b64encode(body).decode(), 'filename': filename, 'mediaType': media_type}


async def conversation_files(db, *, task, principal, messages):
    """Expose all canonical current/prior attachment inputs, retaining captured versions."""
    ids = list(dict.fromkeys(uuid.UUID(message['source_ref'].removeprefix('session-input:'))
        for message in messages if str(message.get('source_ref') or '').startswith('session-input:')))
    if not ids:
        return [], []
    rows = {row.id: row for row in await db.scalars(select(SessionTurnInput).where(
        SessionTurnInput.tenant_id == task.tenant_id, SessionTurnInput.session_id == uuid.UUID(task.parent_session_id),
        SessionTurnInput.id.in_(ids)))}
    if set(rows) != set(ids):
        raise _unavailable()
    bindings, parts = [], []
    try:
        async with async_agent_workspace_lock(task.parent_agent_id):
            for input_id in ids:
                row = rows[input_id]
                for index, part in enumerate(row.content_parts_json):
                    if part.get('type') != 'attachment_ref':
                        continue
                    binding, wire = await _file(db, task=task, principal=principal, row=row,
                        part_index=index, attachment=part.get('attachment'))
                    bindings.append({**binding, 'wire_part_index': len(parts) + 2})
                    parts.append(wire)
        await db.flush()
    except (WorkspaceAuthorityError, OSError) as exc:
        raise _unavailable() from exc
    return bindings, parts
