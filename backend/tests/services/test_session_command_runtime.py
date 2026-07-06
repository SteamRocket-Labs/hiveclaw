from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.memory.t0.ledger import T0SessionEvent


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        value = self._value if isinstance(self._value, list) else ([] if self._value is None else [self._value])
        return SimpleNamespace(all=lambda: value)


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

    result = await execute_session_command(
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
async def test_session_commands_resume_prefers_t0_jsonl_truth_over_db_projection(monkeypatch):
    import app.services.session_command_runtime as runtime

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    t0_events = [_t0_event(session, "user_message", sequence=1, content="persisted before model loop", role="user")]
    db = _DB(session, [])

    monkeypatch.setattr(runtime, "replay_t0_session_events_tail", lambda **_kwargs: t0_events, raising=False)

    result = await runtime.execute_session_command(
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_source"] == "t0_events_jsonl"
    assert result["event_count"] == 1
    assert result["interrupted"] is True
    assert result["last_replayable_event"]["ledger_event_id"] == "evt_1"
    assert result["next_query"] == "Continue from where you left off."


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

    complete = await execute_session_command(
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="resume",
        session_id=session.id,
        arguments={},
    )
    interrupted = await execute_session_command(
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

    renamed = await execute_session_command(
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="rename",
        session_id=session.id,
        arguments={"title": "New title"},
    )
    tagged = await execute_session_command(
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

    result = await execute_session_command(
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="export",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_surface"] == "chat_transcript_events_read_model_with_t0_fallback"
    assert result["truth_source"] == "chat_transcript_events_read_model"
    assert result["transcript_events"][0]["content"] == "hello"
    assert result["messages"][0]["role"] == "user"
    assert result["artifacts"][0]["path"] == "workspace/report.md"


@pytest.mark.asyncio
async def test_session_export_uses_t0_jsonl_truth_and_db_as_read_model(monkeypatch):
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

    result = await runtime.execute_session_command(
        db=db,
        agent=agent,
        user=user,
        access_level="use",
        command_name="export",
        session_id=session.id,
        arguments={},
    )

    assert result["truth_surface"] == "t0_events_jsonl_plus_markdown_projection"
    assert result["truth_source"] == "t0_events_jsonl"
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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
async def test_rewind_workspace_mode_is_explicitly_not_supported_without_snapshot():
    from app.services.session_command_runtime import execute_session_command

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    db = _DB(session, [first])

    result = await execute_session_command(
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
            str(first.id): {"checkpoint_event_id": str(first.id), "manifest_path": "runtime_artifacts/snap/manifest.json"}
        }
    }
    db = _DB(session, [first])

    def fail_restore(**_kwargs):
        raise AssertionError("workspace restore must wait for explicit confirmation")

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fail_restore)

    result = await runtime.execute_session_command(
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
    assert result["ui_action"]["type"] == "open_permissions_menu"
    assert result["debug_payload"]["requested_mode"] == "workspace"


@pytest.mark.asyncio
async def test_rewind_workspace_mode_restores_snapshot_when_confirmed(monkeypatch):
    import app.services.session_command_runtime as runtime
    from app.services.session_workspace_snapshot import WorkspaceRestoreResult

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = _session(agent.id, user.id)
    first = _event(session, "user_message", sequence=1, content="first", role="user")
    session.transcript_metadata_json = {
        "workspace_snapshots": {
            str(first.id): {"checkpoint_event_id": str(first.id), "manifest_path": "runtime_artifacts/snap/manifest.json"}
        }
    }
    db = _DB(session, [first])
    appended = []

    def fake_restore_session_workspace_snapshot(**kwargs):
        assert kwargs["checkpoint_event_id"] == str(first.id)
        return WorkspaceRestoreResult(
            ok=True,
            checkpoint_event_id=str(first.id),
            workspace_rel_path="workspace",
            restored_files=["report.md"],
            deleted_files=["draft.md"],
            unchanged_files=[],
            error=None,
        )

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=9)

    monkeypatch.setattr(runtime, "restore_session_workspace_snapshot", fake_restore_session_workspace_snapshot)
    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)

    result = await runtime.execute_session_command(
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
    assert appended[0]["event_type"] == "session_workspace_rewind"
    assert db.flushes == 1


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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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
        await runtime.execute_session_command(
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

    result = await execute_session_command(
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

    result = await execute_session_command(
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
        await execute_session_command(
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
        await execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await runtime.execute_session_command(
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

    result = await execute_session_command(
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

    result = await runtime.execute_session_command(
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

    assert truth_source == "t0_events_jsonl"
    assert [event.sequence for event in events] == list(range(21, 31))
    assert events[-1].content == "m30"
