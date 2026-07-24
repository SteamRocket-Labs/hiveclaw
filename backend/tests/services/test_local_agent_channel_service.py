from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import local_agent_channel_service as service


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeDB:
    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.executed = []
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        value = self.execute_values.pop(0) if self.execute_values else None
        if hasattr(value, "all") or hasattr(value, "scalar_one_or_none"):
            return value
        return _ScalarResult(value)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self):
        self.committed = True


def test_reported_capability_names_adapts_canonical_hive_connect_vocabulary() -> None:
    assert service._reported_capability_names(
        {
            "im": True,
            "streaming": True,
            "attachments": True,
            "workspace": True,
            "runner": "codex",
        }
    ) == ("event_stream", "execute", "file_upload", "result_report")


def test_reported_capability_names_does_not_overgrant_unknown_or_disabled_aliases() -> None:
    assert (
        service._reported_capability_names(
            {
                "im": False,
                "streaming": False,
                "attachments": False,
                "workspace": True,
                "runner": "",
                "unknown": True,
            }
        )
        == ()
    )


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_reuses_existing_web_session() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    existing_session = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=None,
        chat_session_id=None,
        source="web",
        status="active",
        created_at=None,
    )
    db = _FakeDB([existing_session])

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )

    assert payload["id"] == existing_session.id
    assert payload["source"] == "web"
    assert payload["status"] == "active"
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_creates_when_missing() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    db = _FakeDB([None])

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )

    assert payload["source"] == "web"
    assert payload["status"] == "active"
    assert len(db.added) == 1
    assert db.flushed is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_creates_agent_scoped_chat_session(monkeypatch) -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB([None])
    captured = {}

    async def fake_create_or_bind_chat_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=chat_session_id)

    monkeypatch.setattr(service, "create_or_bind_chat_session", fake_create_or_bind_chat_session)

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        title="Codex on Mac",
    )

    assert payload["chat_session_id"] == chat_session_id
    assert payload["source"] == "web"
    assert len(db.added) == 1
    assert db.added[0].source_agent_id == source_agent_id
    assert db.added[0].chat_session_id == chat_session_id
    assert captured["db"] is db
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == source_agent_id
    assert captured["user_id"] == owner_user_id
    assert captured["external_conversation_id"] == f"local_agent:{owner_user_id}:{source_agent_id}:web"
    assert captured["source_channel"] == "local_agent"


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_separates_shared_actor_from_host_owner(monkeypatch) -> None:
    tenant_id = uuid4()
    host_owner_id = uuid4()
    actor_user_id = uuid4()
    source_agent_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB([_RowsResult([])])
    captured = {}

    async def fake_create_or_bind_chat_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=chat_session_id)

    monkeypatch.setattr(service, "create_or_bind_chat_session", fake_create_or_bind_chat_session)

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        actor_user_id=actor_user_id,
        source_agent_id=source_agent_id,
        title="Shared Codex",
    )

    assert payload["chat_session_id"] == chat_session_id
    assert len(db.added) == 1
    channel_session = db.added[0]
    assert channel_session.owner_user_id == host_owner_id
    assert channel_session.source_agent_id == source_agent_id
    assert captured["user_id"] == actor_user_id
    assert captured["external_conversation_id"] == (
        f"local_agent:{host_owner_id}:{source_agent_id}:{actor_user_id}:web"
    )


@pytest.mark.asyncio
async def test_create_a2a_channel_session_reuses_exact_active_conversation(monkeypatch) -> None:
    tenant_id = uuid4()
    target_owner_id = uuid4()
    requester_user_id = uuid4()
    target_agent_id = uuid4()
    chat_session_id = uuid4()
    channel_session_id = uuid4()
    chat_session = SimpleNamespace(
        id=chat_session_id,
        title="A2A from Source Agent",
        source_channel="local_agent",
        session_kind="local_agent_channel",
        last_message_at=None,
        created_at=None,
    )
    existing_channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=target_owner_id,
        source_agent_id=target_agent_id,
        chat_session_id=chat_session_id,
        source="a2a",
        status="active",
        created_at=None,
        updated_at=None,
    )
    db = _FakeDB([existing_channel_session])

    async def fake_create_or_bind_chat_session(**_kwargs):
        return chat_session

    monkeypatch.setattr(service, "create_or_bind_chat_session", fake_create_or_bind_chat_session)

    payload = await service.create_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=target_owner_id,
        actor_user_id=requester_user_id,
        source_agent_id=target_agent_id,
        source="a2a",
        title="A2A from Source Agent",
        commit=False,
        reuse_existing=True,
    )

    assert payload["id"] == channel_session_id
    assert payload["chat_session_id"] == chat_session_id
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_enqueue_a2a_local_message_binds_exact_source_parent_session() -> None:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    target_owner_id = uuid4()
    requester_user_id = uuid4()
    source_agent_id = uuid4()
    target_agent_id = uuid4()
    channel_session_id = uuid4()
    parent_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=target_owner_id,
        source_agent_id=target_agent_id,
        chat_session_id=None,
        source="a2a",
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        tenant_id=tenant_id,
        agent_id=source_agent_id,
        user_id=requester_user_id,
    )
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
    )
    db = _FakeDB([channel_session, parent_session, None])

    payload = await service.enqueue_channel_message(
        db,
        session_id=channel_session_id,
        owner_user_id=target_owner_id,
        sender_user_id=requester_user_id,
        sender_agent_id=source_agent_id,
        content="Run this on the target machine.",
        metadata={
            "source": "a2a",
            "execution_target": "local_agent",
            "sender_agent_id": str(source_agent_id),
            "target_agent_id": str(target_agent_id),
            "target_agent_name": "Target Mac",
            "target_owner_user_id": str(target_owner_id),
            "parent_session_id": str(parent_session_id),
            "execution_principal": principal.to_evidence(),
        },
        idempotency_key="a2a:exact-source-route",
    )

    message = next(obj for obj in db.added if obj.__class__.__name__ == "LocalAgentChannelMessage")
    assert payload["id"] == str(message.id)
    assert message.sender_agent_id == source_agent_id
    assert message.source_agent_id == target_agent_id
    assert message.metadata_json["parent_session_id"] == str(parent_session_id)
    assert message.metadata_json["execution_principal"] == principal.to_evidence()


@pytest.mark.asyncio
async def test_enqueue_a2a_local_message_rejects_cross_session_authority() -> None:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    target_owner_id = uuid4()
    requester_user_id = uuid4()
    source_agent_id = uuid4()
    target_agent_id = uuid4()
    channel_session_id = uuid4()
    parent_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=target_owner_id,
        source_agent_id=target_agent_id,
        chat_session_id=None,
        source="a2a",
    )
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
    )
    db = _FakeDB([channel_session])

    with pytest.raises(Exception) as exc_info:
        await service.enqueue_channel_message(
            db,
            session_id=channel_session_id,
            owner_user_id=target_owner_id,
            sender_user_id=requester_user_id,
            sender_agent_id=source_agent_id,
            content="Do not cross source sessions.",
            metadata={
                "source": "a2a",
                "execution_target": "local_agent",
                "sender_agent_id": str(source_agent_id),
                "target_agent_id": str(target_agent_id),
                "target_owner_user_id": str(target_owner_id),
                "parent_session_id": str(uuid4()),
                "execution_principal": principal.to_evidence(),
            },
            idempotency_key="a2a:cross-session-denied",
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_list_agent_channel_sessions_returns_sidebar_ready_sessions() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
        source="web",
        status="active",
        created_at=None,
        updated_at=None,
    )
    chat_session = SimpleNamespace(
        id=chat_session_id,
        agent_id=source_agent_id,
        title="Codex on Mac",
        source_channel="local_agent",
        session_kind="local_agent_channel",
        last_message_at=None,
        created_at=None,
    )
    db = _FakeDB([_RowsResult([(channel_session, chat_session)])])

    rows = await service.list_agent_channel_sessions(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=owner_user_id,
        source_agent_id=source_agent_id,
    )

    assert rows == [
        {
            "id": channel_session_id,
            "chat_session_id": chat_session_id,
            "agent_id": source_agent_id,
            "title": "Codex on Mac",
            "source": "web",
            "source_channel": "local_agent",
            "session_kind": "local_agent_channel",
            "status": "active",
            "created_at": None,
            "updated_at": None,
            "last_message_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_agent_channel_session_accepts_chat_session_id() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
        source="web",
        status="active",
        created_at=None,
        updated_at=None,
    )
    chat_session = SimpleNamespace(
        id=chat_session_id,
        agent_id=source_agent_id,
        title="Local debug session",
        source_channel="local_agent",
        session_kind="local_agent_channel",
        last_message_at=None,
        created_at=None,
    )
    db = _FakeDB([_RowsResult([(channel_session, chat_session)])])

    payload = await service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        session_id=chat_session_id,
    )

    assert payload["id"] == channel_session_id
    assert payload["chat_session_id"] == chat_session_id
    assert payload["title"] == "Local debug session"


@pytest.mark.asyncio
async def test_record_channel_result_writes_assistant_message_for_actor_chat_session() -> None:
    tenant_id = uuid4()
    host_owner_id = uuid4()
    actor_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    message_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
    )
    channel_message = SimpleNamespace(
        id=message_id,
        session_id=channel_session_id,
        owner_user_id=host_owner_id,
        source_agent_id=source_agent_id,
        tenant_id=tenant_id,
        direction="hive_to_local",
        content="shared caller request",
        status="delivered",
        result=None,
        attachments_json=[],
        metadata_json={},
        created_at=None,
        delivered_at=None,
        completed_at=None,
    )
    mirrored_chat_session = SimpleNamespace(id=chat_session_id, user_id=actor_user_id, last_message_at=None)

    class _DbWithGet(_FakeDB):
        async def get(self, _model, obj_id):
            assert obj_id == chat_session_id
            return mirrored_chat_session

    db = _DbWithGet([channel_session, channel_message])
    context = SimpleNamespace(
        tenant_id=tenant_id,
        user_id=host_owner_id,
        scopes=("local_agent:report",),
    )

    result = await service.record_channel_result(
        db,
        context=context,
        session_id=channel_session_id,
        message_id=message_id,
        result_status="completed",
        output="shared caller result",
        artifacts=[],
        metadata={},
    )

    chat_messages = [obj for obj in db.added if obj.__class__.__name__ == "ChatMessage"]
    assert result["status"] == "completed"
    assert len(chat_messages) == 1
    assert chat_messages[0].user_id == actor_user_id
    assert chat_messages[0].conversation_id == str(chat_session_id)
    assert db.executed[1]._for_update_arg is not None


@pytest.mark.asyncio
async def test_record_a2a_channel_result_enqueues_source_session_completion_before_commit(
    monkeypatch,
) -> None:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    host_owner_id = uuid4()
    requester_user_id = uuid4()
    source_agent_id = uuid4()
    target_agent_id = uuid4()
    channel_session_id = uuid4()
    target_chat_session_id = uuid4()
    parent_session_id = uuid4()
    message_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        source_agent_id=target_agent_id,
        chat_session_id=target_chat_session_id,
    )
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
        root_runtime_task_id=str(uuid4()),
    )
    channel_message = SimpleNamespace(
        id=message_id,
        session_id=channel_session_id,
        owner_user_id=host_owner_id,
        source_agent_id=target_agent_id,
        sender_agent_id=source_agent_id,
        sender_user_id=requester_user_id,
        tenant_id=tenant_id,
        direction="hive_to_local",
        content="run on the target machine",
        status="delivered",
        result=None,
        attachments_json=[],
        metadata_json={
            "source": "a2a",
            "execution_target": "local_agent",
            "sender_agent_id": str(source_agent_id),
            "sender_agent_name": "Source Agent",
            "target_agent_id": str(target_agent_id),
            "target_agent_name": "Target Mac",
            "target_owner_user_id": str(host_owner_id),
            "parent_session_id": str(parent_session_id),
            "execution_principal": principal.to_evidence(),
        },
        request_hash=None,
        capability_snapshot_hash=None,
        replay_key="local:test",
        receipt_trace_id=None,
        receipt_span_id=None,
        delivery_lease_expires_at=None,
        created_at=None,
        delivered_at=None,
        completed_at=None,
    )
    target_chat_session = SimpleNamespace(
        id=target_chat_session_id,
        user_id=requester_user_id,
        last_message_at=None,
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        tenant_id=tenant_id,
        agent_id=source_agent_id,
        user_id=requester_user_id,
    )

    class _DbWithGet(_FakeDB):
        async def get(self, _model, obj_id):
            assert obj_id == target_chat_session_id
            return target_chat_session

    db = _DbWithGet([channel_session, channel_message, parent_session])
    context = SimpleNamespace(
        tenant_id=tenant_id,
        user_id=host_owner_id,
        scopes=("local_agent:report",),
    )
    notification_id = uuid4()
    captured = {}

    async def fake_enqueue_completion_notification(db_arg, notification):
        assert db_arg is db
        assert db.committed is False
        captured["notification"] = notification
        return notification_id

    monkeypatch.setattr(
        service,
        "enqueue_completion_notification",
        fake_enqueue_completion_notification,
        raising=False,
    )

    result = await service.record_channel_result(
        db,
        context=context,
        session_id=channel_session_id,
        message_id=message_id,
        result_status="completed",
        output="Repository evidence is ready.",
        artifacts=[{"path": "workspace/results/evidence.md"}],
        metadata={"runtime": "codex"},
    )

    notification = captured["notification"]
    assert notification.source_kind == "a2a_delegation"
    assert notification.source_run_id == str(message_id)
    assert notification.parent_session_id == parent_session_id
    assert notification.parent_agent_id == source_agent_id
    assert notification.parent_user_id == requester_user_id
    assert notification.terminal_status == "completed"
    assert notification.task_type == "a2a_local_delegation"
    assert notification.summary == "Repository evidence is ready."
    assert notification.child_agent_name == "Target Mac"
    assert notification.delivery_mode == "parent_continuation"
    assert notification.artifacts == [{"path": "workspace/results/evidence.md"}]
    assert result["source_delivery"] == {
        "status": "queued",
        "notification_id": str(notification_id),
    }
    assert channel_message.metadata_json["source_delivery"] == result["source_delivery"]
    assert db.committed is True


@pytest.mark.asyncio
async def test_record_a2a_channel_result_replay_repairs_missing_source_delivery(
    monkeypatch,
) -> None:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    host_owner_id = uuid4()
    requester_user_id = uuid4()
    source_agent_id = uuid4()
    target_agent_id = uuid4()
    channel_session_id = uuid4()
    parent_session_id = uuid4()
    message_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
    )
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        source_agent_id=target_agent_id,
        chat_session_id=None,
    )
    channel_message = SimpleNamespace(
        id=message_id,
        session_id=channel_session_id,
        owner_user_id=host_owner_id,
        source_agent_id=target_agent_id,
        sender_agent_id=source_agent_id,
        sender_user_id=requester_user_id,
        tenant_id=tenant_id,
        direction="hive_to_local",
        content="run on the target machine",
        status="completed",
        result="Persisted original result.",
        attachments_json=[],
        metadata_json={
            "source": "a2a",
            "execution_target": "local_agent",
            "sender_agent_id": str(source_agent_id),
            "target_agent_id": str(target_agent_id),
            "target_agent_name": "Target Mac",
            "target_owner_user_id": str(host_owner_id),
            "parent_session_id": str(parent_session_id),
            "execution_principal": principal.to_evidence(),
            "report": {
                "runtime": "codex",
                "artifacts": [{"path": "workspace/original.md"}],
            },
        },
        request_hash=None,
        capability_snapshot_hash=None,
        replay_key="local:test",
        receipt_trace_id=None,
        receipt_span_id=None,
        delivery_lease_expires_at=None,
        created_at=None,
        delivered_at=None,
        completed_at=None,
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        tenant_id=tenant_id,
        agent_id=source_agent_id,
        user_id=requester_user_id,
    )
    db = _FakeDB([channel_session, channel_message, parent_session])
    context = SimpleNamespace(
        tenant_id=tenant_id,
        user_id=host_owner_id,
        scopes=("local_agent:report",),
    )
    notification_id = uuid4()
    captured = {}

    async def fake_enqueue_completion_notification(_db, notification):
        captured["notification"] = notification
        return notification_id

    monkeypatch.setattr(
        service,
        "enqueue_completion_notification",
        fake_enqueue_completion_notification,
        raising=False,
    )

    result = await service.record_channel_result(
        db,
        context=context,
        session_id=channel_session_id,
        message_id=message_id,
        result_status="completed",
        output="Duplicate payload must not replace the persisted result.",
        artifacts=[],
        metadata={"runtime": "duplicate"},
    )

    assert result["idempotent_replay"] is True
    assert result["source_delivery"]["notification_id"] == str(notification_id)
    assert captured["notification"].summary == "Persisted original result."
    assert captured["notification"].artifacts == [{"path": "workspace/original.md"}]
    assert [obj for obj in db.added if obj.__class__.__name__ == "ChatMessage"] == []
    assert db.committed is True


@pytest.mark.asyncio
async def test_reconcile_stale_delivery_terminalizes_remote_action_span(monkeypatch) -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    trace_id = str(uuid4())
    span_id = uuid4().hex
    started_at = service.utcnow() - timedelta(seconds=2)
    message = SimpleNamespace(
        id=message_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        session_id=session_id,
        direction="hive_to_local",
        status="delivered",
        result=None,
        delivery_attempt_count=service.MAX_DELIVERY_ATTEMPTS,
        delivery_lease_expires_at=service.utcnow() - timedelta(seconds=1),
        replay_key=f"local:{message_id}",
        request_hash="request-hash",
        capability_snapshot_hash="snapshot-hash",
        receipt_trace_id=trace_id,
        receipt_span_id=span_id,
        metadata_json={},
    )
    span = SimpleNamespace(
        status="running",
        started_at=started_at,
        ended_at=None,
        duration_ms=0.0,
        side_effect_refs=[],
        metadata_json={"execution_receipt": {"status": "delivered"}},
        error=None,
    )
    db = _FakeDB([_RowsResult([message]), span])
    context = SimpleNamespace(tenant_id=tenant_id, user_id=owner_user_id)

    async def fixed_event_sequence(_db, *, session_id):
        assert session_id == message.session_id
        return 7

    monkeypatch.setattr(service, "AsyncSession", _FakeDB)
    monkeypatch.setattr(service, "_allocate_event_sequence", fixed_event_sequence)

    assert await service._reconcile_stale_deliveries(db, context=context) == 1

    assert message.status == "needs_reconciliation"
    assert span.status == "error"
    assert span.ended_at is not None
    assert span.duration_ms >= 2_000
    assert span.error == service.DELIVERY_RECONCILIATION_ERROR
    assert span.metadata_json["execution_receipt"]["status"] == "needs_reconciliation"
    assert span.metadata_json["reconciliation"] == {
        "status": "needs_reconciliation",
        "reason": "delivery_attempt_limit",
        "replay_key": message.replay_key,
        "delivery_attempt_count": service.MAX_DELIVERY_ATTEMPTS,
        "retryable": False,
        "manual_review_required": True,
    }
    reconciliation_event = next(obj for obj in db.added if obj.__class__.__name__ == "LocalAgentChannelEvent")
    assert reconciliation_event.event_type == "delivery_reconciliation_required"
    assert reconciliation_event.payload_json["reason"] == "delivery_attempt_limit"
    assert reconciliation_event.payload_json["manual_review_required"] is True
