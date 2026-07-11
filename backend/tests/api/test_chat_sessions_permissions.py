from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


def test_session_permission_metadata_requires_active_scoped_break_glass_for_bypass() -> None:
    import app.api.chat_sessions as chat_sessions_api

    session = SimpleNamespace(transcript_metadata_json={})
    ordinary = chat_sessions_api._session_permission_metadata("bypassPermissions", session)
    assert ordinary["permission_mode"] == "default"

    active = chat_sessions_api._session_permission_metadata(
        "bypassPermissions",
        session,
        break_glass={
            "operator_id": str(uuid4()),
            "reason": "incident response",
            "scope": "session",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
    )
    assert active["permission_mode"] == "bypassPermissions"
    assert active["break_glass"]["scope"] == "session"


def test_session_permission_metadata_preserves_existing_mode_when_request_omits_override() -> None:
    import app.api.chat_sessions as chat_sessions_api

    session = SimpleNamespace(
        transcript_metadata_json={
            "permission_mode": "auto",
            "permission_profile": {"mode": "auto"},
        }
    )

    metadata = chat_sessions_api._session_permission_metadata(None, session)

    assert metadata["permission_mode"] == "auto"
    assert metadata["permission_profile"]["mode"] == "auto"


def test_tenant_permission_default_is_safe_and_never_break_glass() -> None:
    import app.api.chat_sessions as chat_sessions_api

    assert chat_sessions_api._tenant_permission_default_from_value({"mode": "auto"}) == "auto"
    assert chat_sessions_api._tenant_permission_default_from_value({"mode": "default"}) == "default"
    assert chat_sessions_api._tenant_permission_default_from_value({"mode": "bypassPermissions"}) == "default"
    assert chat_sessions_api._tenant_permission_default_from_value({"mode": "unknown"}) == "default"
    assert chat_sessions_api._tenant_permission_default_from_value(None) == "default"


@pytest.mark.asyncio
async def test_break_glass_permission_update_writes_durable_transcript_evidence(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    operator_id = uuid4()
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, transcript_metadata_json={})
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    current_user = SimpleNamespace(id=operator_id, role="org_admin")
    appended = []

    class DB:
        async def execute(self, _statement):
            return _ScalarResult(None)

        async def commit(self):
            return None

    async def get_session(**_kwargs):
        return session, agent, "manage"

    async def append_event(**kwargs):
        appended.append(kwargs)

    async def get_runtime_context(*_args):
        return SimpleNamespace(metadata={})

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", get_session)
    monkeypatch.setattr(chat_sessions_api, "append_session_event", append_event)
    monkeypatch.setattr(chat_sessions_api.web_chat_broker, "get_or_create_runtime_session", get_runtime_context)
    monkeypatch.setattr(chat_sessions_api, "broadcast_web_chat_event", broadcast)

    result = await chat_sessions_api.update_session_permission_profile(
        agent_id=agent_id,
        session_id=session_id,
        body=chat_sessions_api.UpdateSessionPermissionProfileIn(
            permission_mode="bypassPermissions",
            break_glass_reason="incident containment",
            break_glass_scope="session",
            break_glass_ttl_minutes=15,
        ),
        current_user=current_user,
        db=DB(),
    )

    assert result["permission_mode"] == "bypassPermissions"
    assert appended[0]["event_type"] == "permission_profile_updated"
    assert appended[0]["actor_type"] == "user"
    assert appended[0]["user_id"] == operator_id
    assert appended[0]["metadata"]["break_glass"]["reason"] == "incident containment"
    assert appended[0]["materialize_chat_message"] is False


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def scalar(self):
        return self._values

    def all(self):
        return self._values


@pytest.mark.asyncio
async def test_session_owner_cannot_self_elevate_to_operator_projection_with_query_params(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    owner_id = uuid4()
    session = SimpleNamespace(id=uuid4(), user_id=owner_id)
    agent = SimpleNamespace(id=uuid4())
    current_user = SimpleNamespace(id=owner_id)
    audited = []

    async def fake_audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_audit)

    authority_source = await chat_sessions_api._authorize_loaded_session(
        session=session,
        agent=agent,
        access_level="use",
        current_user=current_user,
        action="read_transcript",
        operator_view=True,
        operator_reason="Trying to reveal technical details",
    )

    assert authority_source == "session_owner"
    assert audited == []


@pytest.mark.asyncio
async def test_manager_operator_projection_requires_reason_and_writes_audit(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    owner_id = uuid4()
    manager_id = uuid4()
    session = SimpleNamespace(id=uuid4(), user_id=owner_id)
    agent = SimpleNamespace(id=uuid4())
    current_user = SimpleNamespace(id=manager_id)
    audited = []

    async def fake_audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_audit)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api._authorize_loaded_session(
            session=session,
            agent=agent,
            access_level="manage",
            current_user=current_user,
            action="read_transcript",
            operator_view=True,
        )
    assert exc.value.status_code == 403

    authority_source = await chat_sessions_api._authorize_loaded_session(
        session=session,
        agent=agent,
        access_level="manage",
        current_user=current_user,
        action="read_transcript",
        operator_view=True,
        operator_reason="Investigating a delivery incident",
    )

    assert authority_source == "manager_override"
    assert audited[0][1]["details"]["authority_source"] == "manager_override"
    assert audited[0][1]["details"]["reason"] == "Investigating a delivery incident"


class _QueryAwareDB:
    def __init__(
        self,
        *,
        agent=None,
        sessions=None,
        messages=None,
        artifacts=None,
        transcript_events=None,
        counts=None,
        message_counts=None,
        user_message_counts=None,
        users=None,
        agent_names=None,
    ):
        self.agent = agent
        self.sessions = sessions or []
        self.messages = messages or []
        self.artifacts = artifacts or []
        self.transcript_events = transcript_events or []
        self.counts = list(counts or [])
        self.message_counts = {str(key): value for key, value in (message_counts or {}).items()}
        self.user_message_counts = {str(key): value for key, value in (user_message_counts or {}).items()}
        self.users = users or {}
        self.agent_names = agent_names or {}
        self.statements = []
        self.added = []
        self.deleted = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = str(stmt)
        if "count(chat_messages.id)" in sql and "GROUP BY chat_messages.conversation_id" in sql:
            source = self.user_message_counts if "chat_messages.role" in sql else self.message_counts
            return _ListResult([(key, value) for key, value in source.items()])
        if "count(chat_messages.id)" in sql:
            if not self.counts:
                raise AssertionError("No count prepared")
            return _ScalarResult(self.counts.pop(0))
        if sql.startswith("DELETE FROM "):
            return _ScalarResult(None)
        if "FROM chat_sessions" in sql:
            if "WHERE chat_sessions.id =" in sql:
                return _ScalarResult(self.sessions[0] if self.sessions else None)
            return _ListResult(self.sessions)
        if "FROM chat_messages" in sql:
            return _ListResult(self.messages)
        if "FROM chat_artifacts" in sql:
            return _ListResult(self.artifacts)
        if "FROM chat_transcript_events" in sql:
            return _ListResult(self.transcript_events)
        if "coalesce(users.display_name, users.username)" in sql:
            if " IN " in sql:
                return _ListResult([(user_id, name) for user_id, name in self.users.items()])
            session = self.sessions[0]
            return _ScalarResult(self.users.get(session.user_id, "Unknown"))
        if "FROM agents" in sql and "agents.name" in sql and " IN " in sql:
            return _ListResult([(agent_id, name) for agent_id, name in self.agent_names.items()])
        if "FROM agents" in sql:
            return _ScalarResult(self.agent)
        raise AssertionError(f"Unhandled SQL in fake DB: {sql}")

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


@pytest.mark.asyncio
async def test_list_sessions_uses_check_agent_access_for_mine_scope(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _QueryAwareDB(agent=agent, sessions=[])
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    assert result == []
    assert called["args"] == (db, current_user, agent_id)


@pytest.mark.asyncio
async def test_create_session_uses_check_agent_access(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    db = _QueryAwareDB(agent=agent)
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.create_session(
        agent_id=agent_id,
        body=chat_sessions_api.CreateSessionIn(title="Manual Session"),
        current_user=current_user,
        db=db,
    )

    assert result.title == "Manual Session"
    assert called["args"] == (db, current_user, agent_id)
    assert result.is_current_user_session is True
    assert result.read_only is False


@pytest.mark.asyncio
async def test_list_sessions_mine_keeps_empty_owned_web_sessions_writable(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        title="Session 07-01 02:46",
        created_at=SimpleNamespace(isoformat=lambda: "2026-07-01T02:46:00+00:00"),
        last_message_at=None,
        peer_agent_id=None,
        parent_session_id=None,
        root_session_id=None,
        runtime_task_id=None,
        transcript_metadata_json={},
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=[session],
        message_counts={session_id: 0},
        user_message_counts={session_id: 0},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].id == str(session_id)
    assert result[0].message_count == 0
    assert result[0].is_current_user_session is True
    assert result[0].read_only is False


@pytest.mark.asyncio
async def test_list_sessions_all_scope_requires_explicit_audited_operator_view(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Ops Thread",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:10:00+00:00"),
        peer_agent_id=None,
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session], message_counts={session_id: 2}, users={owner_id: "Owner"})

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.list_sessions(
            agent_id=agent_id,
            scope="all",
            current_user=current_user,
            db=db,
        )
    assert exc.value.status_code == 403

    audited = []

    async def fake_audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_audit)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        operator_reason="Investigating an Agent delivery incident",
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].id == str(session_id)
    assert result[0].operator_view is True
    assert result[0].authority_source == "manager_override"
    assert audited[0][1]["details"]["reason"] == "Investigating an Agent delivery incident"


@pytest.mark.asyncio
async def test_list_sessions_downgrades_unscoped_legacy_bypass_permission_mode(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Permission Mode Thread",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:10:00+00:00"),
        peer_agent_id=None,
        transcript_metadata_json={
            "permission_mode": "bypassPermissions",
            "session_permission_allowed_tools": ["web_search"],
        },
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=[session],
        message_counts={session_id: 2},
        user_message_counts={session_id: 1},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].permission_mode == "default"
    assert result[0].permission_profile == {
        "mode": "default",
        "allowed_tools": ["web_search"],
        "writable_roots": ["workspace/"],
    }


@pytest.mark.asyncio
async def test_list_sessions_mine_scope_uses_canonical_microsoft_teams_channel(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="microsoft_teams",
        title="Teams Thread",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:10:00+00:00"),
        peer_agent_id=None,
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=[session],
        message_counts={session_id: 2},
        user_message_counts={session_id: 1},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    session_query = db.statements[0]
    session_query_params = session_query.compile().params
    assert "chat_sessions.user_id" in str(session_query)
    assert owner_id in session_query_params.values()
    assert session_query_params["listed_surface_1"] == "chat"
    assert len(result) == 1
    assert result[0].source_channel == "microsoft_teams"


@pytest.mark.asyncio
async def test_list_sessions_mine_includes_owned_a2a_peer_sessions(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    requested_agent_id = uuid4()
    owning_user_id = uuid4()
    target_agent_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=requested_agent_id, creator_id=owning_user_id)
    a2a_session = SimpleNamespace(
        id=session_id,
        agent_id=target_agent_id,
        peer_agent_id=requested_agent_id,
        user_id=owning_user_id,
        source_channel="agent",
        session_kind="delegation_run",
        actor_type="agent",
        runtime_source="delegation",
        visibility_scope="agent_owner",
        listed_surface="chat",
        title="Lead ↔ Researcher",
        created_at=SimpleNamespace(isoformat=lambda: "2026-06-29T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-06-29T00:05:00+00:00"),
        parent_session_id=None,
        root_session_id=None,
        runtime_task_id=None,
        transcript_metadata_json={},
    )
    current_user = SimpleNamespace(id=owning_user_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=[a2a_session],
        message_counts={session_id: 2},
        user_message_counts={session_id: 1},
        users={owning_user_id: "rocky"},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=requested_agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    assert "chat_sessions.peer_agent_id" in str(db.statements[0])
    count_statements = [str(stmt) for stmt in db.statements if "count(chat_messages.id)" in str(stmt)]
    assert len(count_statements) == 2
    a2a_user_message_count_sql = count_statements[0]
    a2a_total_count_sql = count_statements[1]
    assert "chat_messages.conversation_id" in a2a_user_message_count_sql
    assert "chat_messages.agent_id" not in a2a_user_message_count_sql
    assert "chat_messages.conversation_id" in a2a_total_count_sql
    assert "chat_messages.agent_id" not in a2a_total_count_sql
    assert len(result) == 1
    assert result[0].participant_type == "agent"
    assert result[0].peer_agent_id == str(requested_agent_id)
    assert result[0].source_channel == "agent"
    assert result[0].session_kind == "delegation_run"


@pytest.mark.asyncio
async def test_list_sessions_mine_batches_message_counts_for_multiple_sessions(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    sessions = []
    message_counts = {}
    user_message_counts = {}
    for index in range(3):
        session_id = uuid4()
        sessions.append(
            SimpleNamespace(
                id=session_id,
                agent_id=agent_id,
                user_id=owner_id,
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
                title=f"Thread {index}",
                created_at=SimpleNamespace(isoformat=lambda: "2026-07-02T00:00:00+00:00"),
                last_message_at=None,
                peer_agent_id=None,
                parent_session_id=None,
                root_session_id=None,
                runtime_task_id=None,
                transcript_metadata_json={},
            )
        )
        message_counts[session_id] = index + 2
        user_message_counts[session_id] = index + 1

    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=sessions,
        message_counts=message_counts,
        user_message_counts=user_message_counts,
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    count_statements = [str(stmt) for stmt in db.statements if "count(chat_messages.id)" in str(stmt)]
    assert len(count_statements) == 2
    assert all("GROUP BY chat_messages.conversation_id" in sql for sql in count_statements)
    assert [item.message_count for item in result] == [2, 3, 4]


@pytest.mark.asyncio
async def test_get_session_messages_requires_explicit_operator_view_for_non_owner(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="done",
        participant_id=None,
        thinking=None,
        created_at=None,
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session], messages=[message])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session_messages(
            agent_id=agent_id,
            session_id=session_id,
            current_user=current_user,
            db=db,
        )
    assert exc.value.status_code == 403

    audited = []

    async def fake_audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_audit)

    result = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        operator_view=True,
        operator_reason="Reviewing another user's failed delivery",
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "done"
    assert audited[0][1]["details"]["action"] == "read_messages"


@pytest.mark.asyncio
async def test_get_session_index_rejects_non_owner_without_manage_access(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_read_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1"}

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(chat_sessions_api, "read_session_index", fake_read_session_index, raising=False)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session_index(
            agent_id=agent_id,
            session_id=session_id,
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_session_messages_enriches_artifact_agent_names(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    producer_agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        id=message_id,
        role="assistant",
        content="done",
        participant_id=None,
        thinking=None,
        created_at=None,
    )
    artifact = SimpleNamespace(
        id=uuid4(),
        message_id=message_id,
        agent_id=producer_agent_id,
        path="workspace/report.md",
        name="report.md",
        mime_type="text/markdown",
        size=128,
        modified_at=None,
        preview_kind="markdown",
        source="a2a_workspace_write",
        runtime_task_id=None,
        snapshot_hash="sha256:report",
        snapshot_json={
            "owner_agent_id": str(producer_agent_id),
            "source_agent_id": str(producer_agent_id),
            "download_agent_id": str(producer_agent_id),
        },
        created_at=None,
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(
        agent=agent,
        sessions=[session],
        messages=[message],
        artifacts=[artifact],
        agent_names={producer_agent_id: "Reviewer Bot"},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    artifact_part = result[0]["artifacts"][0]
    assert artifact_part["source_agent_id"] == str(producer_agent_id)
    assert artifact_part["source_agent_name"] == "Reviewer Bot"
    assert artifact_part["owner_agent_name"] == "Reviewer Bot"
    assert artifact_part["download_agent_name"] == "Reviewer Bot"


@pytest.mark.asyncio
async def test_get_session_context_usage_returns_context_diagnostics(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    prompt_manifest = {
        "schema": "hive.ccplus.prompt_assembly_manifest.v1",
        "context_usage_ledger": {
            "schema": "hive.ccplus.context_usage_ledger.v1",
            "model_window_tokens": 1000,
            "used_tokens": 120,
            "free_space_tokens": 880,
            "categories": [
                {"name": "system_prompt", "tokens": 20, "chars": 80, "item_count": 1},
                {"name": "skills", "tokens": 10, "chars": 40, "item_count": 2},
            ],
        },
        "context_candidates": [{"id": "ctx:memory:memory_files", "selected": True}],
        "selected_contexts": [{"id": "ctx:memory:memory_files", "selected": True}],
        "suppressed_contexts": [{"id": "ctx:permissions:permissions_context", "selected": False}],
        "loaded_skills": ["research"],
        "active_tool_names": ["read_file"],
        "available_deferred_tools": ["web_search"],
    }
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
        transcript_metadata_json={
            "runtime_assembly_state": {
                "schema": "hive.ccplus.runtime_assembly_state.v1",
                "prompt_assembly_manifest": prompt_manifest,
                "dynamic_context_section_ledger": {
                    "schema": "hive.ccplus.dynamic_context_section_ledger.v1",
                    "sections": [{"candidate_id": "dynamic:skill:skill_catalog", "selected": True}],
                },
                "tool_result_ledger": [
                    {"tool_name": "read_file", "result_kind": "evidence"},
                    {"tool_name": "search_personal_kb", "result_kind": "knowledge_reference"},
                ],
                "cache_decision_ledger": [{"cache_surface": "prompt_prefix", "decision": "hit"}],
                "agent_cycle_decision_ledger": [{"subsystem": "workflow", "decision": "run_or_preview"}],
                "activation_candidates": [
                    {"candidate_kind": "agent_memory", "metadata": {"scope": "agent"}},
                ],
            },
            "context_artifacts": [{"kind": "knowledge_relevant", "source": "prompt_manifest"}],
        },
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.get_session_context_usage(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert result["schema"] == "hive.ccplus.session_context_usage.v1"
    assert result["session_id"] == str(session_id)
    assert result["model_window_tokens"] == 1000
    assert result["used_tokens"] == 120
    assert result["free_space_tokens"] == 880
    assert result["categories"][1]["name"] == "skills"
    assert result["selected_contexts"][0]["id"] == "ctx:memory:memory_files"
    assert result["suppressed_contexts"][0]["id"] == "ctx:permissions:permissions_context"
    assert result["dynamic_context_sections"][0]["candidate_id"] == "dynamic:skill:skill_catalog"
    assert result["tool_result_ledger"][0]["tool_name"] == "read_file"
    assert result["knowledge_tool_results"] == [
        {"tool_name": "search_personal_kb", "result_kind": "knowledge_reference"}
    ]
    assert result["cache_decision_ledger"][0]["cache_surface"] == "prompt_prefix"
    assert result["agent_cycle_decision_ledger"][0]["subsystem"] == "workflow"
    assert result["activation_candidates"][0]["candidate_kind"] == "agent_memory"
    assert result["context_artifacts"][0]["kind"] == "knowledge_relevant"
    assert result["counts"] == {
        "categories": 2,
        "context_candidates": 1,
        "selected_contexts": 1,
        "suppressed_contexts": 1,
        "dynamic_context_sections": 1,
        "cache_decisions": 1,
        "agent_cycle_decisions": 1,
        "activation_candidates": 1,
        "knowledge_tool_results": 1,
        "context_artifacts": 1,
        "tools": 1,
        "deferred_tools": 1,
        "skills": 1,
    }


@pytest.mark.asyncio
async def test_delete_session_removes_transcript_before_messages(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=owner_id)
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.delete_session(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    delete_sql = [str(stmt) for stmt in db.statements if str(stmt).startswith("DELETE FROM ")]
    assert "DELETE FROM chat_transcript_events" in delete_sql[1]
    assert "DELETE FROM chat_messages" in delete_sql[2]
    assert db.deleted == [session]
    assert db.commits == 1
    assert result is None


@pytest.mark.asyncio
async def test_delete_branch_session_runs_blockable_worktree_remove_hook(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    tenant_id = uuid4()
    agent_id = uuid4()
    owner_id = uuid4()
    source_session_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=owner_id,
        parent_session_id=source_session_id,
    )
    current_user = SimpleNamespace(id=owner_id, role="member", tenant_id=tenant_id)
    db = _QueryAwareDB(agent=agent, sessions=[session])
    captured = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_emit(event, **kwargs):
        captured.append((event.value, kwargs))
        return None

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)

    await chat_sessions_api.delete_session(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert captured[0][0] == "worktree_remove"
    assert captured[0][1]["metadata"]["source_session_id"] == str(source_session_id)
    assert captured[0][1]["metadata"]["target_session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_get_session_transcript_returns_replayable_events(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    message_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    event = SimpleNamespace(
        id=uuid4(),
        sequence=42,
        session_id=session_id,
        run_id=run_id,
        message_id=message_id,
        actor_type="assistant",
        event_type="assistant_message",
        visibility_scope="direct_user",
        listed_surface="chat",
        content="final answer",
        parts_json=[{"type": "text", "text": "final answer"}],
        metadata_json={"source": "web", "role": "assistant"},
        created_at=datetime(2026, 6, 20, 12, tzinfo=timezone.utc),
    )
    current_user = SimpleNamespace(id=owner_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session], transcript_events=[event])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.get_session_transcript(
        agent_id=agent_id,
        session_id=session_id,
        after_sequence=10,
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0]["schema"] == "hive.thread_item.v1"
    assert result[0]["id"] == str(event.id)
    assert result[0]["sequence"] == 42
    assert result[0]["item_type"] == "agent_message"
    assert result[0]["audience"] == "user"
    assert result[0]["user_summary"] == "final answer"
    assert result[0]["content"] == "final answer"
    assert result[0]["metadata"] == {"status": "succeeded"}
    assert result[0]["evidence_refs"] == []
    assert "operator_details" not in result[0]
    assert db.commits == 1


def test_serialize_transcript_event_bounds_large_ui_payload():
    import app.api.chat_sessions as chat_sessions_api

    session_id = uuid4()
    event = SimpleNamespace(
        id=uuid4(),
        sequence=7,
        session_id=session_id,
        run_id=None,
        message_id=None,
        actor_type="assistant",
        event_type="artifact_delivery",
        visibility_scope="direct_user",
        listed_surface="chat",
        content="c" * 80_000,
        parts_json=[
            {
                "type": "artifact",
                "artifact_id": "artifact-1",
                "filename": "report.md",
                "inline_content": "p" * 600_000,
            }
        ],
        metadata_json={
            "role": "assistant",
            "artifact_id": "artifact-1",
            "artifact_ids": ["artifact-1"],
            "content_replacement": {"inline_content": "m" * 600_000},
        },
        created_at=None,
    )

    payload = chat_sessions_api._serialize_transcript_event(event)

    encoded = json.dumps(payload, default=str)
    assert len(encoded) < 40_000
    assert len(payload["content"]) < 20_000
    assert payload["metadata"]["_payload_truncated"] is True
    assert payload["metadata"]["artifact_ids"] == ["artifact-1"]
    assert "inline_content" not in payload["parts"][0]
    assert "content_replacement" not in payload["metadata"]


@pytest.mark.asyncio
async def test_rename_session_rejects_use_access_for_non_owner(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        title="Before",
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.rename_session(
            agent_id=agent_id,
            session_id=session_id,
            body=chat_sessions_api.PatchSessionIn(title="After"),
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403
