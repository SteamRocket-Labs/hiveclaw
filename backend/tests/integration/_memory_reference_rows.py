"""Seed retained memory-index rows without the retired prompt projection."""

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert

from app.models.invocation_span import InvocationSpan
from app.services.memory_resource_references import SOURCE_KIND, _FILES, _SCHEMA, _hash, _private_sessions, _source_session
from app.services.workspace_resource_authority import open_workspace_file


async def capture_memory_reference(db, *, item, data_root, tenant_id, reader, user):
    """Write the same observation and source binding as the former capture helper."""
    source_session = _source_session(item.source)
    if str(source_session) != item.metadata.get("session_id"):
        raise ValueError("memory_resource_session_mismatch")
    await _private_sessions(db, tenant_id=tenant_id, agent_id=reader.agent_id, user_id=user.id,
        session_ids={uuid.UUID(reader.session_id), source_session})
    files = []
    for name in _FILES:
        try:
            (data_root / str(reader.agent_id) / item.source / name).lstat()
        except FileNotFoundError:
            continue
        with open_workspace_file(data_root / str(reader.agent_id), f"{item.source}/{name}"):
            files.append(name)
    if "manifest.json" not in files or not any(name != "manifest.json" for name in files):
        raise ValueError("memory_resource_unavailable")
    payload = {"schema": _SCHEMA, "tenant_id": str(tenant_id), "agent_id": str(reader.agent_id),
        "session_id": reader.session_id, "user_id": str(user.id), "source_session_id": str(source_session),
        "resource_path": item.source, "files": files}
    version = _hash(payload)
    source_id = uuid.uuid5(uuid.UUID(reader.session_id), f"memory-resource-reference:{version}")
    await db.execute(insert(InvocationSpan).values(id=source_id, tenant_id=tenant_id, agent_id=reader.agent_id,
        user_id=user.id, session_id=reader.session_id, trace_id=f"session-context:{reader.session_id}",
        span_id=f"memory-reference:{source_id}", span_type=SOURCE_KIND, name=SOURCE_KIND, status="captured",
        duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={SOURCE_KIND: payload})
        .on_conflict_do_nothing(index_elements=["id"]))
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    if row is None or row.metadata_json != {SOURCE_KIND: payload}:
        raise ValueError("memory_resource_reference_conflict")
    return ({"source_kind": SOURCE_KIND, "source_id": str(source_id), "source_version": version},)
