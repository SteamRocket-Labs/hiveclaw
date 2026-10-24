"""Registered session recall cannot borrow another requester's private history."""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.memory.t0.ledger import append_t0_session_event
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _sources(n, owner_sessionmaker, *, source, admin=False):
    own, foreign, unknown = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        if admin:
            (await db.get(User, n.user)).role = "org_admin"
        db.add_all([ChatSession(id=own, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            audience_kind="private", last_message_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
            ChatSession(id=foreign, tenant_id=n.tenant, agent_id=n.agent, user_id=n.foreign,
                audience_kind="private", last_message_at=datetime(2026, 10, 1, tzinfo=timezone.utc))])
        await db.flush()
        for session, marker in [(own, "OWN_PRIVATE_EVIDENCE"), (foreign, "OTHER_REQUESTER_PRIVATE_EVIDENCE"),
            (unknown, "UNKNOWN_PRIVATE_EVIDENCE")]:
            body = f"meeting {marker}\nPreserve the complete continuation tail: {marker}_TAIL"
            if source == "db":
                db.add(ChatMessage(tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, role="user",
                    conversation_id=str(session), content=body))
            elif source == "ledger":
                append_t0_session_event(agent_id=n.agent, session_id=str(session), event_type="user_message",
                    role="user", content=body, source="web", data_root=n.root.parent,
                    created_at=datetime(2026, 9 if session == own else 4, 1, tzinfo=timezone.utc))
            else:
                path = n.root / "logs" / f"chat-{session}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"---\ntype: chat\nsession_id: {session}\nsource: web\nuser: Same Display Name\n"
                    f"started: 2026-{'09' if session == own else '04'}-01T00:00:00Z\n---\n**User**: {body}\n")
        await db.commit()
    return own, foreign


async def _search(n, **extra):
    return str(await n.service.execute("search_memory", {"query": "meeting", "scope": "sessions", "limit": 1, **extra},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))


@pytest.mark.parametrize("source", ["ledger", "legacy", "db"])
@pytest.mark.parametrize("admin", [False, True])
async def test_each_native_source_filters_authority_before_the_requested_result_limit(
    native_workspace, owner_sessionmaker, source, admin,  # noqa: F811
):
    n = native_workspace
    await _sources(n, owner_sessionmaker, source=source, admin=admin)
    result = await _search(n)
    assert "OWN_PRIVATE_EVIDENCE" in result and "OWN_PRIVATE_EVIDENCE_TAIL" in result, result
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "UNKNOWN_PRIVATE_EVIDENCE" not in result


@pytest.mark.parametrize("case", ["shared_worker", "outage", "during_read", "during_source_change", "caller_identity"])
async def test_native_recall_never_falls_back_around_missing_current_authority(
    native_workspace, owner_sessionmaker, monkeypatch, case,  # noqa: F811
):
    from app.services import session_recall

    n = native_workspace
    own, _foreign = await _sources(n, owner_sessionmaker,
        source="db" if case in {"during_read", "during_source_change"} else "ledger")
    if case == "shared_worker":
        async with owner_sessionmaker() as db:
            (await db.get(ChatSession, n.session)).audience_kind = "group"
            await db.commit()
    elif case == "outage":
        @asynccontextmanager
        async def unavailable(*_args, **_kwargs):
            raise OSError("synthetic recall authority outage")
            yield
        monkeypatch.setattr(session_recall, "tenant_scoped_session", unavailable)
    elif case in {"during_read", "during_source_change"}:
        original = session_recall._search_session_history_db
        async def change_owner(*args, **kwargs):
            hits = await original(*args, **kwargs)
            async with owner_sessionmaker() as db:
                if case == "during_read":
                    (await db.get(Agent, n.agent)).owner_user_id = n.foreign
                else:
                    (await db.get(ChatSession, own)).user_id = n.foreign
                await db.commit()
            return hits
        monkeypatch.setattr(session_recall, "_search_session_history_db", change_owner)
    result = await _search(n, **({"user_id": str(n.foreign)} if case == "caller_identity" else {}))
    assert "PRIVATE_EVIDENCE" not in result, result
    expected = {"shared_worker": "session_recall_shared_audience_unavailable", "outage": "session_recall_authority_unavailable",
        "during_read": "session_recall_access_denied", "caller_identity": "session_recall_arguments_invalid"}
    if case in expected:
        assert expected[case] in result, result


async def test_raw_service_cannot_use_files_as_authority_when_the_reader_is_missing(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    from app.services.session_recall import search_session_history

    n = native_workspace
    await _sources(n, owner_sessionmaker, source="ledger")
    with pytest.raises(ValueError, match="authority"):
        await search_session_history(n.agent, "meeting", tenant_id=n.tenant)


@pytest.mark.parametrize("source", ["ledger", "legacy", "db"])
async def test_past_session_recall_never_reingests_the_current_work(
    native_workspace, owner_sessionmaker, monkeypatch, source,  # noqa: F811
):
    from app.services import session_recall

    n = native_workspace
    await _sources(n, owner_sessionmaker, source=source)
    marker = "CURRENT_WORK_RECURSIVE_RECALL"
    body = f"meeting {marker}: this tool result must not become a new past-session hit."
    if source == "ledger":
        append_t0_session_event(agent_id=n.agent, session_id=str(n.session), event_type="tool_result.completed",
            role="tool", content=body, source="web", data_root=n.root.parent)
    elif source == "legacy":
        path = n.root / "logs" / f"chat-{n.session}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntype: chat\nsession_id: {n.session}\nsource: web\n"
            f"started: 2026-10-24T00:00:00Z\n---\n**Tools**: {body}\n")
    else:
        async with owner_sessionmaker() as db:
            db.add(ChatMessage(tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, role="assistant",
                conversation_id=str(n.session), content=body))
            await db.commit()
    observed = []
    original = session_recall._search_t0_session_ledger
    def record_candidates(*args, **kwargs):
        observed.append(set(kwargs["authorized_session_ids"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(session_recall, "_search_t0_session_ledger", record_candidates)
    result = str(await n.service.execute("search_memory", {"query": marker, "scope": "sessions"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert body not in result, result
    assert "Complete transcript:" not in result and "session_recall_error" not in result, result
    assert observed and all(str(n.session) not in candidates for candidates in observed)
    # Authorized past evidence remains complete, including its original continuation tail.
    past = await _search(n)
    assert "OWN_PRIVATE_EVIDENCE" in past and "OWN_PRIVATE_EVIDENCE_TAIL" in past, past
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in past and "UNKNOWN_PRIVATE_EVIDENCE" not in past
