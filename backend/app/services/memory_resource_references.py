"""Native index observations for optional T2 bodies without producer receipts.

An observation attests only resource locators read by this private session. It
does not attest the contents, generation, or upstream sources of any file.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

from sqlalchemy import select

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


async def _private_sessions(db, *, tenant_id, agent_id, user_id, session_ids, sessions=None):
    sessions = sessions if sessions is not None else list(await db.scalars(select(ChatSession).where(ChatSession.id.in_(session_ids),
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id,
        ChatSession.user_id == user_id).execution_options(populate_existing=True)))
    if {session.id for session in sessions} != set(session_ids) or any(
        session.audience_kind not in {None, "private"} for session in sessions
    ):
        raise ContentInputReceiptError("content_source_access_denied", "The private memory reader changed.", 403)




async def validate_reference(db, tenant_id, dependency, *, user=None, runtime_request=None):
    """Recheck the current reader, without promoting a locator into a body receipt."""
    from app.core.permissions import check_agent_access
    from app.services.runtime_user_identity import load_scoped_runtime_user

    try:
        source_id = uuid.UUID(dependency["source_id"])
        from app.services.content_input_receipts import _input_row

        row = await _input_row(db, InvocationSpan, source_id)
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
    from app.services.content_input_receipts import _batch_browser_point

    if (runtime_request is None and user is not None and user.role != "platform_admin"
        and _batch_browser_point(db, user)):
        from types import SimpleNamespace
        from app.models.user import User
        from app.core.permissions import agent_access_from_facts
        from app.services.source_read_queries import source_reader_statement, _json_rows, reader_columns

        session_ids = {scope["session_id"], scope["source_session_id"]}
        facts = (await db.execute(source_reader_statement(user, scope["agent_id"]).add_columns(User,
            _json_rows(ChatSession, ("id", "audience_kind"), ChatSession.id.in_(session_ids),
                ChatSession.tenant_id == tenant_id, ChatSession.agent_id == scope["agent_id"],
                ChatSession.user_id == user.id)).outerjoin(User, User.id == user.id).options(*reader_columns(User)))).first()
        requester = facts[5] if facts is not None else None
        if requester is None or requester.role == "platform_admin":
            requester = await load_scoped_runtime_user(db, user.id, tenant_id=tenant_id)
            facts = None
        elif not requester.is_active or requester.tenant_id != tenant_id:
            requester = None
        if requester is None or requester.id != scope["user_id"]:
            raise ContentInputReceiptError("content_source_access_denied", "The original memory requester is unavailable.", 403)
        sessions = None
        if facts is None:
            await check_agent_access(db, requester, scope["agent_id"])
        else:
            await agent_access_from_facts(db, requester, facts)
            sessions = [SimpleNamespace(id=uuid.UUID(row["id"]), audience_kind=row["audience_kind"]) for row in facts[6] or []]
        await _private_sessions(db, tenant_id=tenant_id, agent_id=scope["agent_id"], user_id=requester.id,
            session_ids=session_ids, sessions=sessions)
        return
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
