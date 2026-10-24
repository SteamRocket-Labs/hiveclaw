"""Native index observations for optional T2 bodies without producer receipts.

An observation attests only resource locators read by this private session. It
does not attest the contents, generation, or upstream sources of any file.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.memory.types import MemoryKind
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor
from app.services.session_model_inputs import _hash

SOURCE_KIND = "memory_resource_reference"
_FILES = ("manifest.json", "summary.md", "labels.md", "synthesis.md", "review.md")
_SCHEMA = "hive.memory_resource_reference.v1"


def _source_session(path: str) -> uuid.UUID:
    from app.core.resource_authority import normalize_workspace_resource_path

    if not isinstance(path, str) or normalize_workspace_resource_path(path) != path:
        raise ValueError("invalid_t2_resource_path")
    parts = PurePosixPath(path).parts
    prefix = ("memory", "t2", "sessions") if parts[:3] == ("memory", "t2", "sessions") else ("memory", "sessions")
    if (PurePosixPath(path).as_posix() != path or ".." in parts
        or parts[:len(prefix)] != prefix or len(parts) != len(prefix) + 3
        or parts[-2] not in {"segments", "episodes"}):
        raise ValueError("invalid_t2_resource_path")
    return uuid.UUID(parts[len(prefix)])


async def _private_sessions(db, *, tenant_id, agent_id, user_id, session_ids):
    sessions = list(await db.scalars(select(ChatSession).where(ChatSession.id.in_(session_ids),
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id,
        ChatSession.user_id == user_id).execution_options(populate_existing=True)))
    if {session.id for session in sessions} != set(session_ids) or any(
        session.audience_kind not in {None, "private"} for session in sessions
    ):
        raise ContentInputReceiptError("content_source_access_denied", "The private memory reader changed.", 403)


def _render(payload):
    paths = [f"{payload['resource_path']}/{name}" for name in payload["files"]]
    reads = "; ".join(f"read_file(path={json.dumps(path, ensure_ascii=False)})" for path in paths)
    return ("source_unverified: Optional T2 memory body was not prefetched because its original producer "
        "sources are unverified. This reference attests only the resource index, not its body. "
        "Inspect complete authorized files when relevant: " + reads)


async def _capture_reference(db, *, item, data_root, tenant_id, reader, user):
    from app.services.workspace_resource_authority import open_workspace_file

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
        duration_ms=0, started_at=datetime.now(UTC), metadata_json={SOURCE_KIND: payload})
        .on_conflict_do_nothing(index_elements=["id"]))
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    if row is None or row.metadata_json != {SOURCE_KIND: payload}:
        raise ValueError("memory_resource_reference_conflict")
    source = {"source_kind": SOURCE_KIND, "source_id": str(source_id), "source_version": version}
    return replace(item, content=_render(payload), source_refs=(source,),
        metadata={"source_ref": item.source, "native_reference": True})


async def project_unverified_t2(items, *, data_root, tenant_id, principal):
    """Keep other context available when an optional reference cannot be authorized."""
    from app.database import tenant_scoped_session
    from app.services.workspace_resource_authority import WorkspaceAuthorityError
    from sqlalchemy.exc import SQLAlchemyError

    candidates = [item for item in items if getattr(item, "kind", None) == MemoryKind.EPISODIC
        and item.metadata.get("source_type") == "t2_package" and not item.source_refs]
    if not candidates:
        return items, 0
    candidate_ids = {id(item) for item in candidates}
    other = [item for item in items if id(item) not in candidate_ids]
    projected = {}
    try:
        async with tenant_scoped_session(tenant_id, require_tenant=True, source="memory_resource_reference") as db:
            reader, user = await _runtime_input_actor(db, tenant_id, principal)
            for item in candidates:
                try:
                    projected[id(item)] = await _capture_reference(db, item=item, data_root=data_root,
                        tenant_id=tenant_id, reader=reader, user=user)
                except (ContentInputReceiptError, OSError, ValueError, WorkspaceAuthorityError):
                    continue
            await db.commit()
    except (ContentInputReceiptError, SQLAlchemyError, OSError):
        return other, len(candidates)
    return [projected[id(item)] if id(item) in projected else item for item in items
        if id(item) not in candidate_ids or id(item) in projected], len(candidates) - len(projected)


async def validate_reference(db, tenant_id, dependency, *, user=None, runtime_request=None):
    """Recheck the current reader, without promoting a locator into a body receipt."""
    from app.core.permissions import check_agent_access
    from app.services.runtime_user_identity import load_scoped_runtime_user

    try:
        source_id = uuid.UUID(dependency["source_id"])
        row = await db.get(InvocationSpan, source_id, populate_existing=True)
        p = (row.metadata_json or {}).get(SOURCE_KIND) if row is not None else None
        if not isinstance(p, dict) or set(p) != {"schema", "tenant_id", "agent_id", "session_id", "user_id",
            "source_session_id", "resource_path", "files"} or p["schema"] != _SCHEMA:
            raise ValueError("invalid_memory_reference")
        scope = {key: uuid.UUID(p[key]) for key in ("tenant_id", "agent_id", "session_id", "user_id", "source_session_id")}
        if (scope["tenant_id"] != tenant_id or row.tenant_id != tenant_id or row.agent_id != scope["agent_id"]
            or row.user_id != scope["user_id"] or row.session_id != p["session_id"]
            or row.span_type != SOURCE_KIND or row.name != SOURCE_KIND or row.status != "captured"
            or row.trace_id != f"session-context:{p['session_id']}" or row.span_id != f"memory-reference:{source_id}"
            or _hash(p) != dependency["source_version"]
            or source_id != uuid.uuid5(scope["session_id"], f"memory-resource-reference:{_hash(p)}")
            or _source_session(p["resource_path"]) != scope["source_session_id"]
            or not isinstance(p["files"], list) or "manifest.json" not in p["files"] or len(p["files"]) < 2
            or p["files"] != [name for name in _FILES if name in p["files"]]):
            raise ValueError("memory_reference_binding_invalid")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The memory index observation is unavailable.", 503) from exc
    if runtime_request is not None:
        principal, requester = await _runtime_input_actor(db, tenant_id, runtime_request)
        if principal.agent_id != scope["agent_id"]:
            raise ContentInputReceiptError("content_source_access_denied", "The memory reference belongs to another employee.", 403)
        await _private_sessions(db, tenant_id=tenant_id, agent_id=scope["agent_id"], user_id=requester.id,
            session_ids={uuid.UUID(principal.session_id)})
    else:
        requester = await load_scoped_runtime_user(db, getattr(user, "id", None), tenant_id=tenant_id)
    if requester is None or requester.id != scope["user_id"]:
        raise ContentInputReceiptError("content_source_access_denied", "The original memory requester is unavailable.", 403)
    await check_agent_access(db, requester, scope["agent_id"])
    await _private_sessions(db, tenant_id=tenant_id, agent_id=scope["agent_id"], user_id=requester.id,
        session_ids={scope["session_id"], scope["source_session_id"]})
