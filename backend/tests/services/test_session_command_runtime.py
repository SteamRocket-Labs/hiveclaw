from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.memory.t0.ledger import T0SessionEvent


async def _run_session_command(owner, **kwargs):
    from app.services.session_command_runtime import SessionCommandContext

    command_name = kwargs.pop("command_name")
    return await owner(SessionCommandContext(**kwargs), command_name)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        value = self._value if isinstance(self._value, list) else ([] if self._value is None else [self._value])
        return SimpleNamespace(all=lambda: value, first=lambda: value[0] if value else None)


class _DB:
    def __init__(self, *values):
        self.values = list(values)
        self.added = []
        self.flushes = 0

    async def execute(self, _stmt):
        if not self.values:
            return _ScalarResult(None)
        return _ScalarResult(self.values.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1


def _db_rows(*events):
    """_load_db_events queries sequence DESC (newest window) and re-ascends;
    the fake returns rows verbatim, so feed them the way the DB would: descending."""
    return list(reversed(events))


def _session(agent_id, user_id, *, title="Session") -> ChatSession:
    return ChatSession(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        user_id=user_id,
        title=title,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )


def _event(
    session: ChatSession,
    event_type: str = "user_message",
    *,
    sequence: int = 1,
    content: str = "hello",
    role: str | None = None,
) -> ChatTranscriptEvent:
    resolved_role = role or (
        "assistant"
        if event_type == "assistant_message"
        else "tool"
        if event_type in {"tool_call", "tool_result"}
        else "user"
    )
    return ChatTranscriptEvent(
        id=uuid4(),
        sequence=sequence,
        tenant_id=session.tenant_id,
        agent_id=session.agent_id,
        session_id=session.id,
        actor_type="assistant" if resolved_role == "assistant" else "tool" if resolved_role == "tool" else "user",
        event_type=event_type,
        visibility_scope="direct_user",
        listed_surface="chat",
        content=content,
        metadata_json={"role": resolved_role},
    )


def _t0_event(
    session: ChatSession,
    event_type: str = "user_message",
    *,
    sequence: int = 1,
    content: str = "hello",
    role: str | None = None,
    transcript_event_id: str | None = None,
) -> T0SessionEvent:
    resolved_role = role or (
        "assistant"
        if event_type == "assistant_message"
        else "tool"
        if event_type in {"tool_call", "tool_result"}
        else "user"
    )
    ledger_event_id = f"evt_{sequence}"
    return T0SessionEvent(
        event_id=ledger_event_id,
        sequence=sequence,
        event_type=event_type,
        role=resolved_role,
        content=content,
        created_at="2026-06-22T00:00:00+00:00",
        message_id=f"msg-{sequence}",
        actor_id=str(session.agent_id),
        runtime_task_id=None,
        turn_id=f"turn-{sequence}",
        intent_id=f"intent-{sequence}",
        source="web",
        sensitivity="PL1_public",
        metadata={"role": resolved_role, "transcript_event_id": transcript_event_id or str(uuid4())},
        path=SimpleNamespace(as_posix=lambda: "memory/t0/sessions/source.md"),
        segment_id="seg-jsonl",
    )


@pytest.mark.asyncio
async def test_session_commands_resume_detects_interrupted_transcript():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session, [_event(session, "tool_call")])

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )

    assert result["interrupted"] is True
    assert result["next_query"] == "Continue from where you left off."
    assert result["repair_strategy"] == "transcript_replay_chain_repair"


@pytest.mark.asyncio
async def test_session_commands_resume_prefers_committed_db_event_over_t0_projection(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    t0_events = [_t0_event(session, "user_message", sequence=1, content="persisted before model loop", role="user")]
    db_events = [_event(session, "assistant_message", sequence=7, content="committed answer", role="assistant")]
    db = _DB(session, db_events)

    monkeypatch.setattr(runtime, "replay_t0_session_events_tail", lambda **_kwargs: t0_events, raising=False)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_source"] == "chat_transcript_events"
    assert result["event_count"] == 1
    assert result["interrupted"] is False
    assert result["last_replayable_event"]["content"] == "committed answer"


@pytest.mark.asyncio
async def test_session_commands_resume_ignores_non_turn_tail_and_detects_user_prewrite_interrupt():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    events = [
        _event(session, "assistant_message", sequence=1, content="done", role="assistant"),
        _event(session, "session_compact_command", sequence=2, content="pressure", role="system"),
    ]
    interrupted_events = [
        _event(session, "user_message", sequence=1, content="continue the audit", role="user"),
        _event(session, "session_compact_command", sequence=2, content="pressure", role="system"),
    ]
    db = _DB(session, events, session, interrupted_events)

    complete = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )
    interrupted = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )

    assert complete["interrupted"] is False
    assert complete["last_replayable_event"]["event_type"] == "assistant_message"
    assert interrupted["interrupted"] is True
    assert interrupted["last_replayable_event"]["event_type"] == "user_message"
    assert interrupted["resume_from_checkpoint_event_id"] == str(interrupted_events[0].id)


@pytest.mark.asyncio
async def test_session_commands_rename_and_tag_update_control_index_only():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session, session)

    renamed = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rename",
        session_id=session.id,
        arguments={"title": "New title"},
    )
    tagged = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="tag",
        session_id=session.id,
        arguments={"tags": ["parity", "cc", "parity"]},
    )

    assert renamed["title"] == "New title"
    assert tagged["tags"] == ["cc", "parity"]
    assert db.flushes == 2


@pytest.mark.asyncio
async def test_session_export_returns_transcript_messages_and_artifact_refs():
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    message = ChatMessage(id=uuid4(), agent_id=agent.id, user_id=user.id, role="user", content="hello")
    artifact = ChatArtifact(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        session_id=session.id,
        message_id=message.id,
        path="workspace/report.md",
        name="report.md",
        snapshot_hash="hash",
    )
    db = _DB(session, [_event(session)], [message], [artifact])

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="export",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_surface"] == "chat_transcript_events_cloud_truth_with_t0_memory_projection"
    assert result["truth_source"] == "chat_transcript_events"
    assert result["transcript_events"][0]["content"] == "hello"
    assert result["messages"][0]["role"] == "user"
    assert result["artifacts"][0]["path"] == "workspace/report.md"


@pytest.mark.asyncio
async def test_session_export_uses_t0_jsonl_as_memory_evidence_fallback(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    t0_events = [
        _t0_event(session, "user_message", sequence=1, content="jsonl first", role="user"),
        _t0_event(session, "assistant_message", sequence=2, content="projection second", role="assistant"),
    ]
    db = _DB(session, [], [], [])

    monkeypatch.setattr(runtime, "replay_t0_session_events_tail", lambda **_kwargs: t0_events, raising=False)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="export",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_surface"] == "t0_events_jsonl_memory_evidence_fallback"
    assert result["truth_source"] == "t0_events_jsonl_fallback"
    assert [event["content"] for event in result["t0_events"]] == ["jsonl first", "projection second"]
    assert result["transcript_events"] == []


@pytest.mark.asyncio
async def test_clear_command_returns_typed_switch_session_control_result(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    db = _DB(source)
    appended = []

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=7)

    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="clear",
        session_id=source.id,
        arguments={"title": "Fresh context"},
    )

    assert result["ok"] is True
    assert result["command"] == "clear"
    assert result["action"] == "session_created"
    assert result["session_id"] == result["session"]["id"]
    assert result["source_session_id"] == str(source.id)
    assert result["ui_action"] == {
        "type": "switch_session",
        "session_id": result["session"]["id"],
        "reason": "clear",
    }
    assert result["control_event"]["event_type"] == "session_clear"
    assert appended[0]["event_type"] == "session_clear"
    assert db.added and isinstance(db.added[0], ChatSession)


@pytest.mark.asyncio
async def test_branch_command_is_non_destructive_session_fork(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    branch = _session(agent.id, user.id, title="Branch")
    branch.parent_session_id = source.id
    branch.root_session_id = source.id
    db = _DB(source)
    captured = []
    appended = []

    async def fake_create_conversation_branch(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            session=branch,
            branch={"anchor_event_id": str(kwargs["anchor_event_id"]), "branch_mode": kwargs["mode"]},
        )

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=8)

    monkeypatch.setattr(runtime, "create_conversation_branch", fake_create_conversation_branch)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="branch",
        session_id=source.id,
        arguments={"anchor_event_id": str(uuid4())},
    )

    assert result["session"]["parent_session_id"] == str(source.id)
    assert result["action"] == "branch_created"
    assert result["ui_action"]["type"] == "switch_session"
    assert result["ui_action"]["reason"] == "branch"
    assert result["control_event"]["event_type"] == "session_branch"
    assert result["branch"]["command"] == "branch"
    assert [item["mode"] for item in captured] == ["branch"]
    assert appended[0]["event_type"] == "session_branch"


@pytest.mark.asyncio
async def test_rewind_without_checkpoint_opens_selector_and_does_not_create_session(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    second = _event(session, "user_message", sequence=3, content="second", role="user")
    db = _DB(session, _db_rows(first, second))

    async def fail_create_conversation_branch(**_kwargs):
        raise AssertionError("rewind must not create a branch session")

    monkeypatch.setattr(runtime, "create_conversation_branch", fail_create_conversation_branch)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={},
    )

    assert result["ok"] is True
    assert result["command"] == "rewind"
    assert result["action"] == "open_checkpoint_selector"
    assert result["session_id"] == str(session.id)
    assert "session" not in result
    assert result["ui_action"]["type"] == "open_checkpoint_selector"
    assert result["rewind_guard"] == {"last_sequence": 3}
    assert [item["checkpoint_event_id"] for item in result["ui_action"]["checkpoints"]] == [
        str(first.id),
        str(second.id),
    ]


@pytest.mark.asyncio
async def test_rewind_with_checkpoint_updates_active_projection_without_new_session(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    second = _event(session, "user_message", sequence=3, content="second", role="user")
    db = _DB(session, _db_rows(first, second))
    appended = []

    async def fail_create_conversation_branch(**_kwargs):
        raise AssertionError("rewind must not create a branch session")

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=9)

    monkeypatch.setattr(runtime, "create_conversation_branch", fail_create_conversation_branch)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={"checkpoint_event_id": str(first.id), "mode": "conversation"},
    )

    assert result["ok"] is True
    assert result["action"] == "rewind_applied"
    assert result["session_id"] == str(session.id)
    assert result["checkpoint"]["checkpoint_event_id"] == str(first.id)
    assert result["ui_action"] == {
        "type": "install_active_projection",
        "session_id": str(session.id),
        "projection_reason": "rewind",
        "checkpoint_event_id": str(first.id),
        "draft_content": "first",
    }
    assert result["control_event"]["event_type"] == "session_rewind"
    assert appended[0]["event_type"] == "session_rewind"
    assert session.transcript_metadata_json["active_projection"]["projection_reason"] == "rewind"
    assert session.transcript_metadata_json["active_projection"]["checkpoint_event_id"] == str(first.id)
    assert session.transcript_metadata_json["active_projection"]["draft_content"] == "first"
    assert db.added == []
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_rewind_interrupts_active_run_then_applies_under_stable_revision(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    second = _event(session, "user_message", sequence=3, content="second", role="user")
    run_id = uuid4()
    db = _DB(session, _db_rows(first, second))
    active_states = iter([{"run_id": str(run_id), "status": "running"}, None])
    revisions = iter([3, 3])
    cancelled = []
    appended = []
    session_locks = []
    revision_locks = []
    lock_order = []

    async def fake_active(**_kwargs):
        return next(active_states)

    async def fake_cancel(**kwargs):
        cancelled.append(kwargs)
        return {"run_id": str(run_id), "status": "killed"}

    async def fake_revision(*_args, **_kwargs):
        revision_locks.append(_kwargs["lock"])
        lock_order.append(f"revision:{_kwargs['lock']}")
        return next(revisions)

    async def fake_session_lock(*_args, **_kwargs):
        session_locks.append(_kwargs["session_id"])
        lock_order.append("session_row")

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=4)

    monkeypatch.setattr(runtime, "get_active_web_chat_run", fake_active)
    monkeypatch.setattr(runtime, "cancel_web_chat_run", fake_cancel)
    monkeypatch.setattr(runtime, "_read_rewind_revision", fake_revision, raising=False)
    monkeypatch.setattr(runtime, "_lock_rewind_session_row", fake_session_lock, raising=False)
    monkeypatch.setattr(runtime, "append_session_event", fake_append)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={
            "checkpoint_event_id": str(first.id),
            "mode": "conversation",
            "expected_last_sequence": 3,
        },
    )

    assert result["action"] == "rewind_applied"
    assert result["rewind_guard"]["interrupted_active_run"] is True
    assert result["rewind_guard"]["last_sequence"] == 3
    assert cancelled[0]["run_id"] == run_id
    assert session_locks == [session.id]
    assert revision_locks == [False, True]
    assert lock_order == ["revision:False", "revision:True", "session_row"]
    assert appended[0]["event_type"] == "session_rewind"


@pytest.mark.asyncio
async def test_rewind_rejects_when_revision_changes_while_interrupting(monkeypatch):
    import app.services.session_command_runtime as runtime
    from fastapi import HTTPException

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    second = _event(session, "user_message", sequence=3, content="second", role="user")
    run_id = uuid4()
    db = _DB(session, _db_rows(first, second))
    active_states = iter([{"run_id": str(run_id), "status": "running"}, None])
    revisions = iter([3, 4])
    appended = []

    async def fake_active(**_kwargs):
        return next(active_states)

    async def fake_cancel(**_kwargs):
        return {"run_id": str(run_id), "status": "killed"}

    async def fake_revision(*_args, **_kwargs):
        return next(revisions)

    async def fake_append(**kwargs):
        appended.append(kwargs)

    monkeypatch.setattr(runtime, "get_active_web_chat_run", fake_active)
    monkeypatch.setattr(runtime, "cancel_web_chat_run", fake_cancel)
    monkeypatch.setattr(runtime, "_read_rewind_revision", fake_revision, raising=False)
    monkeypatch.setattr(runtime, "append_session_event", fake_append)

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            runtime.execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="rewind",
            session_id=session.id,
            arguments={"checkpoint_event_id": str(first.id), "expected_last_sequence": 3},
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "rewind_revision_conflict"
    assert (session.transcript_metadata_json or {}).get("active_projection") is None
    assert appended == []


@pytest.mark.asyncio
async def test_rewind_rejects_stale_client_revision_before_interrupt(monkeypatch):
    import app.services.session_command_runtime as runtime
    from fastapi import HTTPException

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    current = _event(session, "assistant_message", sequence=4, content="new result", role="assistant")
    db = _DB(session, _db_rows(first, current))

    async def fake_revision(*_args, **_kwargs):
        return 4

    async def fail_active(**_kwargs):
        raise AssertionError("stale rewind must fail before touching the active run")

    monkeypatch.setattr(runtime, "_read_rewind_revision", fake_revision, raising=False)
    monkeypatch.setattr(runtime, "get_active_web_chat_run", fail_active)

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            runtime.execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="rewind",
            session_id=session.id,
            arguments={"checkpoint_event_id": str(first.id), "expected_last_sequence": 3},
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "rewind_revision_conflict"


@pytest.mark.asyncio
async def test_rewind_workspace_mode_is_explicitly_not_supported_without_snapshot():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    db = _DB(session, [first])

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={"checkpoint_event_id": str(first.id), "mode": "workspace"},
    )

    assert result["ok"] is False
    assert result["action"] == "not_supported"
    assert result["ui_action"]["type"] == "toast"
    assert result["debug_payload"]["missing"] == "workspace_snapshot"


@pytest.mark.asyncio
async def test_rewind_workspace_mode_requires_explicit_restore_confirmation(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {
                "checkpoint_event_id": str(first.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, [first])

    def fail_restore(**_kwargs):
        raise AssertionError("workspace restore must wait for explicit confirmation")

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fail_restore)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={"checkpoint_event_id": str(first.id), "mode": "workspace"},
    )

    assert result["ok"] is False
    assert result["action"] == "workspace_restore_requires_confirmation"
    assert result["ui_action"]["type"] == "confirm_workspace_restore"
    assert result["ui_action"]["checkpoint_event_id"] == str(first.id)
    assert result["ui_action"]["requested_mode"] == "workspace"
    assert result["debug_payload"]["requested_mode"] == "workspace"


@pytest.mark.asyncio
async def test_workspace_rewind_does_not_refresh_a_stale_revision_during_confirmation():
    from app.services.session_command_runtime import execute_session_command
    from fastapi import HTTPException

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    latest = _event(session, "assistant_message", sequence=3, content="latest", role="assistant")
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {
                "checkpoint_event_id": str(first.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, _db_rows(first, latest))

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="rewind",
            session_id=session.id,
            arguments={
                "checkpoint_event_id": str(first.id),
                "mode": "workspace",
                "expected_last_sequence": 2,
            },
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "stale_workspace_confirmation"


@pytest.mark.asyncio
async def test_rewind_workspace_mode_restores_snapshot_when_confirmed(monkeypatch):
    import app.services.session_command_runtime as runtime
    from app.services.session_workspace_snapshot import WorkspaceRestoreResult

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    file_changes = _event(session, "file_changes", sequence=2, content="file_changes", role="system")
    file_changes.metadata_json = {
        "role": "system",
        "file_change_paths": ["workspace/report.md", "workspace/draft.md"],
        "file_change_states": {
            "workspace/report.md": {
                "path": "workspace/report.md",
                "exists": True,
                "sha256": "a" * 64,
                "size": 10,
            },
            "workspace/draft.md": {
                "path": "workspace/draft.md",
                "exists": False,
                "sha256": None,
                "size": 0,
            },
        },
        "file_change_lineage": [
            {
                "path": "workspace/report.md",
                "before_state": {
                    "path": "workspace/report.md",
                    "exists": False,
                    "sha256": None,
                    "size": 0,
                },
                "after_state": {
                    "path": "workspace/report.md",
                    "exists": True,
                    "sha256": "a" * 64,
                    "size": 10,
                },
            },
            {
                "path": "workspace/draft.md",
                "before_state": {
                    "path": "workspace/draft.md",
                    "exists": True,
                    "sha256": "d" * 64,
                    "size": 5,
                },
                "after_state": {
                    "path": "workspace/draft.md",
                    "exists": False,
                    "sha256": None,
                    "size": 0,
                },
            },
        ],
    }
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {
                "checkpoint_event_id": str(first.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, _db_rows(first, file_changes))
    appended = []

    def fake_restore_session_workspace_snapshot(**kwargs):
        assert kwargs["checkpoint_event_id"] == str(first.id)
        assert kwargs["restore_paths"] == ["workspace/draft.md", "workspace/report.md"]
        assert kwargs["expected_current_states"]["workspace/report.md"]["sha256"] == "a" * 64
        assert len(kwargs["expected_lineage"]["workspace/report.md"]) == 1
        assert kwargs["defer_finalize"] is True
        return WorkspaceRestoreResult(
            ok=True,
            checkpoint_event_id=str(first.id),
            workspace_rel_path="workspace",
            restored_files=["report.md"],
            deleted_files=["draft.md"],
            unchanged_files=[],
            error=None,
            transaction_id="restore-tx-1",
            requires_finalize=True,
        )

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=9)

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fake_restore_session_workspace_snapshot)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={"checkpoint_event_id": str(first.id), "mode": "workspace", "confirm_workspace_restore": True},
    )

    assert result["ok"] is True
    assert result["action"] == "workspace_rewind_applied"
    assert result["ui_action"]["type"] == "install_workspace_snapshot"
    assert result["workspace_restore"]["restored_files"] == ["report.md"]
    assert result["workspace_restore"]["deleted_files"] == ["draft.md"]
    assert result["workspace_restore"]["transaction_id"] == "restore-tx-1"
    assert appended[0]["event_type"] == "session_workspace_rewind"
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_workspace_rewind_lock_wait_does_not_block_the_event_loop(monkeypatch):
    import app.services.session_command_runtime as runtime
    from app.services.session_workspace_snapshot import WorkspaceRestoreResult

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    checkpoint = _event(session, "user_message", sequence=1, content="first", role="user")
    change = _event(session, "file_changes", sequence=2, content="file_changes", role="system")
    change.metadata_json = {
        "file_change_paths": ["workspace/report.md"],
        "file_change_states": {
            "workspace/report.md": {
                "path": "workspace/report.md",
                "exists": True,
                "sha256": "b" * 64,
                "size": 1,
            }
        },
        "file_change_lineage": [
            {
                "path": "workspace/report.md",
                "before_state": {
                    "path": "workspace/report.md",
                    "exists": False,
                    "sha256": None,
                    "size": 0,
                },
                "after_state": {
                    "path": "workspace/report.md",
                    "exists": True,
                    "sha256": "b" * 64,
                    "size": 1,
                },
            }
        ],
    }
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(checkpoint.id): {
                "checkpoint_event_id": str(checkpoint.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, _db_rows(checkpoint, change))
    restore_entered = threading.Event()
    release_restore = threading.Event()

    def slow_restore(**_kwargs):
        restore_entered.set()
        release_restore.wait(timeout=1)
        return WorkspaceRestoreResult(
            ok=True,
            checkpoint_event_id=str(checkpoint.id),
            workspace_rel_path="workspace",
            restored_files=["report.md"],
            deleted_files=[],
            unchanged_files=[],
            transaction_id="restore-nonblocking",
            requires_finalize=True,
        )

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4(), sequence=9)

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", slow_restore)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    timer = threading.Timer(0.25, release_restore.set)
    timer.start()
    started = time.perf_counter()
    command_task = asyncio.create_task(
        _run_session_command(
            runtime.execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="rewind",
            session_id=session.id,
            arguments={
                "checkpoint_event_id": str(checkpoint.id),
                "mode": "workspace",
                "confirm_workspace_restore": True,
            },
        )
    )
    try:
        await asyncio.sleep(0.03)
        assert restore_entered.is_set() is True
        assert time.perf_counter() - started < 0.15
    finally:
        release_restore.set()
        timer.cancel()
    await command_task


@pytest.mark.asyncio
async def test_workspace_rewind_fails_closed_when_file_change_hash_evidence_is_missing(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    legacy_change = _event(session, "file_changes", sequence=2, content="file_changes", role="system")
    legacy_change.metadata_json = {
        "role": "system",
        "file_change_paths": ["workspace/report.md"],
    }
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {
                "checkpoint_event_id": str(first.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, _db_rows(first, legacy_change))

    def fail_restore(**_kwargs):
        raise AssertionError("unverifiable workspace writes must fail before restore")

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fail_restore)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=session.id,
        arguments={
            "checkpoint_event_id": str(first.id),
            "mode": "workspace",
            "confirm_workspace_restore": True,
        },
    )

    assert result["ok"] is False
    assert result["action"] == "workspace_restore_conflict"
    assert result["debug_payload"]["unverifiable_paths"] == ["workspace/report.md"]


def test_workspace_restore_scope_requires_contiguous_write_lineage():
    import app.services.session_command_runtime as runtime

    session = _session(uuid4(), uuid4())
    checkpoint = _event(session, "user_message", sequence=1, content="first", role="user")
    change = _event(session, "file_changes", sequence=2, content="file_changes", role="system")
    change.metadata_json = {
        "file_change_paths": ["workspace/report.md"],
        "file_change_states": {
            "workspace/report.md": {
                "path": "workspace/report.md",
                "exists": True,
                "sha256": "a" * 64,
                "size": 1,
            }
        },
    }

    paths, states, lineage, unverifiable = runtime._workspace_restore_scope_after_checkpoint(
        [checkpoint, change],
        checkpoint=checkpoint,
    )

    assert paths == ["workspace/report.md"]
    assert states["workspace/report.md"]["sha256"] == "a" * 64
    assert lineage == {}
    assert unverifiable == ["workspace/report.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (RuntimeError("control event insert failed"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_workspace_rewind_rolls_back_deferred_swap_when_control_event_fails(
    monkeypatch,
    failure,
    expected_type,
):
    import app.services.session_command_runtime as runtime
    from app.services.session_workspace_snapshot import WorkspaceRestoreResult

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {
                "checkpoint_event_id": str(first.id),
                "manifest_path": "runtime_artifacts/snap/manifest.json",
            }
        }
    }
    db = _DB(session, [first])
    finalized = []

    def fake_restore(**_kwargs):
        return WorkspaceRestoreResult(
            ok=True,
            checkpoint_event_id=str(first.id),
            workspace_rel_path="workspace",
            restored_files=[],
            deleted_files=[],
            unchanged_files=[],
            transaction_id="restore-tx-failed-event",
            requires_finalize=True,
        )

    async def fail_append(**_kwargs):
        raise failure

    def fake_finalize(**kwargs):
        finalized.append(kwargs)
        return True

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fake_restore)
    monkeypatch.setattr(runtime, "append_session_event", fail_append)
    monkeypatch.setattr(runtime, "finalize_workspace_restore", fake_finalize)

    with pytest.raises(expected_type):
        await _run_session_command(
            runtime.execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="rewind",
            session_id=session.id,
            arguments={
                "checkpoint_event_id": str(first.id),
                "mode": "workspace",
                "confirm_workspace_restore": True,
            },
        )

    assert finalized == [
        {
            "agent_id": agent.id,
            "transaction_id": "restore-tx-failed-event",
            "commit": False,
        }
    ]


@pytest.mark.asyncio
async def test_compact_command_installs_compacted_projection_and_session_compact_event(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    events = [
        _event(session, "user_message", sequence=1, content="Please build the report", role="user"),
        _event(session, "assistant_message", sequence=2, content="Report drafted", role="assistant"),
    ]
    db = _DB(session, _db_rows(*events))
    appended = []
    hooks = []

    async def fake_generate_session_summary(messages, tenant_id, **kwargs):
        assert tenant_id == agent.tenant_id
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert kwargs["agent_id"] == agent.id
        assert kwargs["user_id"] == user.id
        return "Compact summary from LLM."

    def fake_wrap_compressed_summary(summary):
        return {"role": "system", "content": f"[Previous conversation summary]\n{summary}"}

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=10)

    async def fake_emit_hook(*args, **kwargs):
        hooks.append((args, kwargs))

    monkeypatch.setattr(runtime, "_generate_session_summary", fake_generate_session_summary)
    monkeypatch.setattr(runtime, "_wrap_compressed_summary", fake_wrap_compressed_summary)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime, "emit_hook", fake_emit_hook)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="compact",
        session_id=session.id,
        arguments={"keep_recent": 1, "reason": "manual cleanup"},
    )

    assert result["ok"] is True
    assert result["command"] == "compact"
    assert result["action"] == "compacted_context_installed"
    assert result["session_id"] == str(session.id)
    assert result["ui_action"]["type"] == "install_compacted_context"
    assert result["ui_action"]["session_id"] == str(session.id)
    assert result["control_event"]["event_type"] == "session_compact"
    assert appended[0]["event_type"] == "session_compact"
    assert appended[0]["content"] == "Compact summary from LLM."
    assert session.transcript_metadata_json["active_projection"]["projection_reason"] == "compact"
    assert session.transcript_metadata_json["active_projection"]["summary"] == "Compact summary from LLM."
    assert session.transcript_metadata_json["active_projection"]["replacement_messages"][0]["role"] == "system"
    assert db.flushes == 1
    assert len(hooks) == 2
    assert hooks[0][0][0] == runtime.HookEvent.PRE_COMPACTION
    assert hooks[0][1]["messages"] == [
        {"role": "user", "content": "Please build the report"},
        {"role": "assistant", "content": "Report drafted"},
    ]
    assert hooks[1][0][0] == runtime.HookEvent.POST_COMPACTION


@pytest.mark.asyncio
async def test_btw_command_creates_side_question_session(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    side_session = _session(agent.id, user.id, title="Side question")
    side_session.parent_session_id = source.id
    side_session.root_session_id = source.id
    side_session.session_kind = "side_question"
    side_session.runtime_source = "side_question"
    side_session.listed_surface = "sidechain"
    anchor = _event(source, "assistant_message", sequence=2, content="answer", role="assistant")
    db = _DB(source, [anchor])
    captured = {}

    async def fake_create_conversation_branch(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            session=side_session,
            branch={"anchor_event_id": str(kwargs["anchor_event_id"]), "branch_mode": kwargs["mode"]},
            run_request=SimpleNamespace(
                content=kwargs["content"],
                display_content=kwargs["display_content"],
                file_name="",
                append_user_message=True,
                attachments=[],
                parts=[],
                extra_metadata={"side_session": True, "max_turns": 1, "tool_policy": "disabled_by_default"},
            ),
        )

    monkeypatch.setattr(runtime, "create_conversation_branch", fake_create_conversation_branch)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="btw",
        session_id=source.id,
        arguments={"question": "What does this acronym mean?", "title": "Acronym side note"},
    )

    assert captured["mode"] == "side_question"
    assert captured["anchor_event_id"] == anchor.id
    assert captured["content"] == "What does this acronym mean?"
    assert result["session"]["id"] == str(side_session.id)
    assert result["session"]["session_kind"] == "side_question"
    assert result["session"]["listed_surface"] == "sidechain"
    assert result["run_request"]["max_turns"] == 1
    assert result["run_request"]["tool_policy"] == "disabled_by_default"


@pytest.mark.asyncio
async def test_turn_steer_command_queues_message_to_active_turn(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session)
    captured = {}

    async def fake_steer_active_web_chat_turn(**kwargs):
        captured.update(kwargs)
        return {
            "run_id": "run-1",
            "turn_id": "turn-1",
            "queued": {"content": kwargs["content"]},
            "steer_strategy": "pending_mid_run_user_message",
        }

    monkeypatch.setattr(runtime, "steer_active_web_chat_turn", fake_steer_active_web_chat_turn)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="turn_steer",
        session_id=session.id,
        arguments={"content": "Use the stricter interpretation.", "expected_turn_id": "turn-1"},
    )

    assert captured["session"] is session
    assert captured["content"] == "Use the stricter interpretation."
    assert captured["expected_turn_id"] == "turn-1"
    assert result["steer_strategy"] == "pending_mid_run_user_message"
    assert result["queued"]["content"] == "Use the stricter interpretation."


@pytest.mark.asyncio
async def test_interrupt_command_cancels_current_active_turn(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session)
    run_id = uuid4()
    captured = {}

    async def fake_get_active_web_chat_run(**kwargs):
        return {"run_id": str(run_id), "status": "running"}

    async def fake_cancel_web_chat_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": str(kwargs["run_id"]), "status": "killed"}

    monkeypatch.setattr(runtime, "get_active_web_chat_run", fake_get_active_web_chat_run)
    monkeypatch.setattr(runtime, "cancel_web_chat_run", fake_cancel_web_chat_run)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="interrupt",
        session_id=session.id,
        arguments={},
    )

    assert captured["agent_id"] == agent.id
    assert captured["session_id"] == session.id
    assert captured["run_id"] == run_id
    assert captured["user_id"] == user.id
    assert result["status"] == "killed"
    assert result["interrupt_strategy"] == "cancel_active_web_chat_run"


@pytest.mark.asyncio
async def test_interrupt_command_rejects_invalid_run_id(monkeypatch):
    from fastapi import HTTPException

    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session)

    async def fake_get_active_web_chat_run(**kwargs):
        return {"run_id": str(uuid4()), "status": "running"}

    monkeypatch.setattr(runtime, "get_active_web_chat_run", fake_get_active_web_chat_run)

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            runtime.execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="interrupt",
            session_id=session.id,
            arguments={"run_id": "not-a-uuid"},
        )

    assert exc.value.status_code == 400
    assert "run_id must be a UUID" in exc.value.detail


@pytest.mark.asyncio
async def test_checkpoints_lists_user_turn_boundaries():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first_user = _event(session, "user_message", sequence=1, content="first", role="user")
    assistant = _event(session, "assistant_message", sequence=2, content="answer", role="assistant")
    second_user = _event(session, "user_message", sequence=3, content="second", role="user")
    db = _DB(session, _db_rows(first_user, assistant, second_user))

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="checkpoints",
        session_id=session.id,
        arguments={},
    )

    assert result["checkpoint_count"] == 2
    assert [item["turn_index"] for item in result["checkpoints"]] == [1, 2]
    assert [item["content"] for item in result["checkpoints"]] == ["first", "second"]
    assert [item["checkpoint_event_id"] for item in result["checkpoints"]] == [str(first_user.id), str(second_user.id)]


@pytest.mark.asyncio
async def test_copy_returns_nth_latest_assistant_response_and_code_blocks():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(
        session,
        "assistant_message",
        sequence=1,
        content="First response\n\n```python\nprint('first')\n```",
        role="assistant",
    )
    second = _event(
        session,
        "assistant_message",
        sequence=2,
        content="Second response\n\n```ts\nconsole.log('second')\n```",
        role="assistant",
    )
    db = _DB(session, _db_rows(first, second))

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="copy",
        session_id=session.id,
        arguments={"n": 2},
    )

    assert result["content"].startswith("First response")
    assert result["message_age"] == 1
    assert result["source_event_id"] == str(first.id)
    assert result["available_assistant_messages"] == 2
    assert result["code_blocks"] == [{"index": 0, "lang": "python", "code": "print('first')\n"}]
    assert result["copy_strategy"] == "client_clipboard_or_file"


@pytest.mark.asyncio
async def test_copy_rejects_out_of_range_assistant_index():
    from fastapi import HTTPException

    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    only = _event(session, "assistant_message", sequence=1, content="Only response", role="assistant")
    db = _DB(session, [only])

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="copy",
            session_id=session.id,
            arguments={"n": 2},
        )

    assert exc.value.status_code == 400
    assert "Only 1 assistant message" in exc.value.detail


@pytest.mark.asyncio
async def test_copy_rejects_zero_index():
    from fastapi import HTTPException

    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    only = _event(session, "assistant_message", sequence=1, content="Only response", role="assistant")
    db = _DB(session, [only])

    with pytest.raises(HTTPException) as exc:
        await _run_session_command(
            execute_session_command,
            db=db,
            agent=agent,
            user=user,
            access_level="use",
            command_name="copy",
            session_id=session.id,
            arguments={"n": 0},
        )

    assert exc.value.status_code == 400
    assert "n must be a positive integer" in exc.value.detail


@pytest.mark.asyncio
async def test_rewind_defaults_to_last_user_checkpoint_and_drops_that_turn(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    first_user = _event(source, "user_message", sequence=1, content="first", role="user")
    assistant = _event(source, "assistant_message", sequence=2, content="answer", role="assistant")
    second_user = _event(source, "user_message", sequence=3, content="second", role="user")
    db = _DB(source, _db_rows(first_user, assistant, second_user))

    async def fail_create_conversation_branch(**_kwargs):
        raise AssertionError("rewind must not create a branch session")

    monkeypatch.setattr(runtime, "create_conversation_branch", fail_create_conversation_branch)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rewind",
        session_id=source.id,
        arguments={},
    )

    assert result["action"] == "open_checkpoint_selector"
    assert result["session_id"] == str(source.id)
    assert [item["checkpoint_event_id"] for item in result["checkpoints"]] == [str(first_user.id), str(second_user.id)]
    assert result["ui_action"]["checkpoints"][-1]["checkpoint_event_id"] == str(second_user.id)


@pytest.mark.asyncio
async def test_rollback_num_turns_selects_nth_latest_user_checkpoint(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    first_user = _event(source, "user_message", sequence=1, content="first", role="user")
    assistant = _event(source, "assistant_message", sequence=2, content="answer", role="assistant")
    second_user = _event(source, "user_message", sequence=3, content="second", role="user")
    db = _DB(source, _db_rows(first_user, assistant, second_user))
    appended = []

    async def fail_create_conversation_branch(**_kwargs):
        raise AssertionError("rollback must use active projection, not branch creation")

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=12)

    monkeypatch.setattr(runtime, "create_conversation_branch", fail_create_conversation_branch)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rollback",
        session_id=source.id,
        arguments={"num_turns": 2},
    )

    assert result["action"] == "rewind_applied"
    assert result["session_id"] == str(source.id)
    assert result["checkpoint"]["checkpoint_event_id"] == str(first_user.id)
    assert result["control_event"]["event_type"] == "session_rewind"
    assert appended[0]["metadata"]["command"] == "rollback"
    assert result["rollback"]["num_turns"] == 2


@pytest.mark.asyncio
async def test_clear_creates_new_context_boundary_without_deleting_source():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    source = _session(agent.id, user.id)
    db = _DB(source)

    result = await _run_session_command(
        execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="clear",
        session_id=source.id,
        arguments={"title": "Clean"},
    )

    new_session = db.added[0]
    assert result["source_session_id"] == str(source.id)
    assert new_session.parent_session_id == source.id
    assert new_session.transcript_metadata_json["keeps_evidence"] is True


@pytest.mark.asyncio
async def test_compact_command_refuses_to_fake_success_without_messages(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    db = _DB(session)
    emitted = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event.value, kwargs))

    async def fake_append_session_event(**kwargs):
        return SimpleNamespace(event_id=uuid4(), kwargs=kwargs)

    monkeypatch.setattr(runtime, "emit_hook", fake_emit_hook)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await _run_session_command(
        runtime.execute_session_command,
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="compact",
        session_id=session.id,
        arguments={"reason": "pressure"},
    )

    assert result["ok"] is False
    assert result["action"] == "not_supported"
    assert result["debug_payload"]["missing"] == "session_messages"
    assert emitted == []


@pytest.mark.asyncio
async def test_load_events_returns_latest_t0_window_not_earliest(monkeypatch, tmp_path):
    """A1 regression pin: a session longer than `limit` must surface its NEWEST
    events (latest_event/active-turn derivation reads events[-1])."""
    import app.services.session_command_runtime as runtime
    from app.config import get_settings
    from app.memory.t0.ledger import append_t0_session_event

    agent = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    for i in range(1, 31):
        append_t0_session_event(
            agent_id=agent.id,
            session_id=session.id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
        )

    events, truth_source = await runtime._load_events(None, agent=agent, session=session, limit=10)

    assert truth_source == "t0_events_jsonl_fallback"
    assert [event.sequence for event in events] == list(range(21, 31))
    assert events[-1].content == "m30"
