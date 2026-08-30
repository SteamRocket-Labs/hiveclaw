from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value=None, values=None):
        self._value = value
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._values))


class _FakeDB:
    def __init__(self, *, anchor, prefix):
        self._results = [_ScalarResult(anchor), _ScalarResult(values=prefix)]
        self.added = []
        self.flushed = 0

    async def execute(self, _stmt):
        if "session_tool_invocations" in str(_stmt):
            return _ScalarResult(values=[])
        assert self._results, "unexpected execute call"
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1


def _event(*, session_id, sequence, event_type, role, content):
    event_id = uuid4()
    return SimpleNamespace(
        id=event_id,
        sequence=sequence,
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=session_id,
        run_id=None,
        parent_event_id=None,
        root_session_id=None,
        parent_session_id=None,
        message_id=uuid4(),
        actor_type="user" if role == "user" else "assistant",
        event_type=event_type,
        visibility_scope="direct_user",
        listed_surface="chat",
        content=content,
        parts_json=[],
        metadata_json={"role": role, "source": "test"},
        created_at=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_edit_branch_creates_new_session_without_mutating_source(monkeypatch):
    from app.models.chat_session import ChatSession
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    source_session = SimpleNamespace(
        id=source_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title="Original",
        parent_session_id=None,
        root_session_id=None,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    anchor = _event(session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="old")
    db = _FakeDB(anchor=anchor, prefix=[])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="edit",
        anchor_event_id=anchor.id,
        content="new wording",
        display_content="new wording",
    )

    branch_session = next(item for item in db.added if isinstance(item, ChatSession))
    assert branch_session.id != source_session_id
    assert branch_session.parent_session_id == source_session_id
    assert branch_session.root_session_id == source_session_id
    assert branch_session.transcript_metadata_json["branch_mode"] == "edit"
    assert branch_session.transcript_metadata_json["anchor_event_id"] == str(anchor.id)
    assert branch_session.title == "Original"
    assert copied == []
    assert result.run_request.content == "new wording"
    assert result.run_request.display_content == "new wording"
    assert result.run_request.append_user_message is True


@pytest.mark.asyncio
async def test_branch_is_blocked_before_mutation_when_source_has_unresolved_tool_effect(monkeypatch):
    from app.services import conversation_branch_service as service
    from app.services.session_tool_runtime import ToolEffectReconciliationRequired

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    anchor = _event(
        session_id=source_session_id,
        sequence=10,
        event_type="user_message",
        role="user",
        content="repeat the write",
    )
    db = _FakeDB(anchor=anchor, prefix=[])

    async def fail_closed(_db, *, tenant_id, session_id, **_kwargs):
        raise ToolEffectReconciliationRequired(
            session_id=session_id,
            run_ids=(uuid4(),),
            invocation_ids=(uuid4(),),
        )

    monkeypatch.setattr(service, "assert_session_tool_effects_settled", fail_closed)
    with pytest.raises(HTTPException) as exc:
        await service.create_conversation_branch(
            db=db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            user=SimpleNamespace(id=user_id),
            source_session=SimpleNamespace(
                id=source_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Original",
                parent_session_id=None,
                root_session_id=None,
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            ),
            mode="edit",
            anchor_event_id=anchor.id,
            content="repeat the write",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "tool_effect_reconciliation_required",
        "session_id": str(source_session_id),
        "retryable": False,
    }
    assert db.added == []


def test_generated_edit_branch_title_does_not_repeat_legacy_machine_suffixes():
    from app.services.conversation_branch_service import _branch_title

    source_session = SimpleNamespace(
        title="Original (edit) (edit)",
        parent_session_id=uuid4(),
        transcript_metadata_json={"branch_mode": "edit"},
    )

    assert _branch_title(source_session, "edit", None) == "Original"


@pytest.mark.asyncio
async def test_fork_branch_copies_prefix_through_anchor(monkeypatch):
    from app.models.chat_session import ChatSession
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    user_event = _event(
        session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question"
    )
    assistant_event = _event(
        session_id=source_session_id,
        sequence=20,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    db = _FakeDB(anchor=assistant_event, prefix=[user_event, assistant_event])
    copied = []
    copied_event_ids = [uuid4(), uuid4()]
    clone_calls = []
    inherited_snapshot = {
        "checkpoint_event_id": str(user_event.id),
        "manifest_path": "runtime_artifacts/session_workspace_snapshots/source/checkpoint/manifest.json",
    }
    excluded_event_id = uuid4()

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=copied_event_ids[len(copied) - 1],
            sequence=kwargs.get("sequence", 1),
            message_id=kwargs.get("message_id"),
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    def fake_clone_workspace_snapshot_for_session(**kwargs):
        clone_calls.append(kwargs)
        return {
            "checkpoint_event_id": str(kwargs["target_checkpoint_event_id"]),
            "source_checkpoint_event_id": str(user_event.id),
            "manifest_path": (
                f"runtime_artifacts/session_workspace_snapshots/{kwargs['target_session_id']}/"
                f"{kwargs['target_checkpoint_event_id']}/manifest.json"
            ),
            "complete": True,
        }

    monkeypatch.setattr(
        "app.services.conversation_branch_service.clone_workspace_snapshot_for_session",
        fake_clone_workspace_snapshot_for_session,
    )

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=SimpleNamespace(
            id=source_session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Original",
            parent_session_id=None,
            root_session_id=None,
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
            transcript_metadata_json={
                "workspace_snapshots": {
                    str(user_event.id): inherited_snapshot,
                    str(excluded_event_id): {
                        "checkpoint_event_id": str(excluded_event_id),
                        "manifest_path": "runtime_artifacts/session_workspace_snapshots/source/later/manifest.json",
                    },
                }
            },
        ),
        mode="fork",
        anchor_event_id=assistant_event.id,
    )

    assert [item["content"] for item in copied] == ["question", "answer"]
    assert [item["metadata"]["copied_from_event_id"] for item in copied] == [
        str(user_event.id),
        str(assistant_event.id),
    ]
    assert [item["bridge_to_t0"] for item in copied] == [False, False]
    assert [item["metadata"]["projection_only"] for item in copied] == [True, True]
    assert [item["metadata"]["semantic_memory_eligible"] for item in copied] == [False, False]
    assert [item["metadata"]["projection_source"] for item in copied] == [
        "conversation_branch_prefix",
        "conversation_branch_prefix",
    ]
    assert result.run_request is None
    branch_session = next(item for item in db.added if isinstance(item, ChatSession))
    inherited = branch_session.transcript_metadata_json["workspace_snapshots"]
    assert list(inherited) == [str(copied_event_ids[0])]
    assert inherited[str(copied_event_ids[0])]["checkpoint_event_id"] == str(copied_event_ids[0])
    assert inherited[str(copied_event_ids[0])]["source_checkpoint_event_id"] == str(user_event.id)
    assert inherited[str(copied_event_ids[0])]["manifest_path"] != inherited_snapshot["manifest_path"]
    assert clone_calls[0]["source_snapshot"] == inherited_snapshot


@pytest.mark.asyncio
async def test_rewind_is_not_accepted_as_a_conversation_branch_mode():
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    first_user = _event(
        session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="first"
    )
    assistant_event = _event(
        session_id=source_session_id,
        sequence=20,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    rewind_target = _event(
        session_id=source_session_id,
        sequence=30,
        event_type="user_message",
        role="user",
        content="second",
    )
    db = _FakeDB(anchor=rewind_target, prefix=[first_user, assistant_event])
    with pytest.raises(HTTPException) as exc:
        await create_conversation_branch(
            db=db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            user=SimpleNamespace(id=user_id),
            source_session=SimpleNamespace(
                id=source_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Original",
                parent_session_id=None,
                root_session_id=None,
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            ),
            mode="rewind",
            anchor_event_id=rewind_target.id,
        )

    assert getattr(exc.value, "status_code", None) == 400
    assert "Use the rewind session command" in str(getattr(exc.value, "detail", ""))
    assert db.added == []


@pytest.mark.asyncio
async def test_branch_from_user_checkpoint_copies_prefix_before_checkpoint_and_returns_draft(monkeypatch):
    from app.models.chat_session import ChatSession
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    first_user = _event(
        session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="first"
    )
    assistant_event = _event(
        session_id=source_session_id,
        sequence=20,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    branch_target = _event(
        session_id=source_session_id,
        sequence=30,
        event_type="user_message",
        role="user",
        content="selected prompt should become composer draft",
    )

    class _BoundaryDB(_FakeDB):
        def __init__(self):
            super().__init__(anchor=branch_target, prefix=[])

        async def execute(self, stmt):
            if "session_tool_invocations" in str(stmt):
                return _ScalarResult(values=[])
            if len(self._results) == 2:
                return self._results.pop(0)
            sql = str(stmt)
            prefix = [first_user, assistant_event, branch_target] if "<=" in sql else [first_user, assistant_event]
            return _ScalarResult(values=prefix)

    db = _BoundaryDB()
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=SimpleNamespace(
            id=source_session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Original",
            parent_session_id=None,
            root_session_id=None,
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
            last_message_at=None,
        ),
        mode="branch",
        anchor_event_id=branch_target.id,
    )

    assert [item["content"] for item in copied] == ["first", "answer"]
    branch_session = next(item for item in db.added if isinstance(item, ChatSession))
    assert branch_session.title == "Original"
    assert result.run_request is None
    assert result.branch["mode"] == "branch"
    assert result.branch["anchor_event_id"] == str(branch_target.id)
    assert result.branch["draft_content"] == "selected prompt should become composer draft"


@pytest.mark.asyncio
async def test_regenerate_branch_runs_from_previous_user_without_appending_duplicate(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    user_event = _event(
        session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question"
    )
    assistant_event = _event(
        session_id=source_session_id,
        sequence=20,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    db = _FakeDB(anchor=assistant_event, prefix=[user_event])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=SimpleNamespace(
            id=source_session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Original",
            parent_session_id=None,
            root_session_id=None,
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        ),
        mode="regenerate",
        anchor_event_id=assistant_event.id,
    )

    assert [item["content"] for item in copied] == ["question"]
    assert result.run_request is not None
    assert result.run_request.content == "question"
    assert result.run_request.display_content == "question"
    assert result.run_request.append_user_message is False
    assert result.run_request.extra_metadata["regenerate_prompt_source_event_id"] == str(user_event.id)
    assert result.run_request.extra_metadata["regenerate_prompt"] == "question"
    assert result.run_request.extra_metadata["semantic_source_refs"] == [
        {
            "session_id": str(source_session_id),
            "event_id": str(user_event.id),
            "role": "user",
            "kind": "regenerate_prompt",
        }
    ]


@pytest.mark.asyncio
async def test_side_question_branch_is_durable_unlisted_one_turn_session(monkeypatch):
    from app.models.chat_session import ChatSession
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    user_event = _event(
        session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question"
    )
    assistant_event = _event(
        session_id=source_session_id,
        sequence=20,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    db = _FakeDB(anchor=assistant_event, prefix=[user_event, assistant_event])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=SimpleNamespace(
            id=source_session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Original",
            parent_session_id=None,
            root_session_id=None,
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        ),
        mode="side_question",
        anchor_event_id=assistant_event.id,
        content="What does btw mean here?",
        display_content="btw: What does this mean?",
    )

    branch_session = next(item for item in db.added if isinstance(item, ChatSession))
    assert branch_session.parent_session_id == source_session_id
    assert branch_session.root_session_id == source_session_id
    assert branch_session.session_kind == "side_question"
    assert branch_session.runtime_source == "side_question"
    assert branch_session.listed_surface == "sidechain"
    assert branch_session.transcript_metadata_json["branch_mode"] == "side_question"
    assert branch_session.transcript_metadata_json["side_session"] is True
    assert branch_session.transcript_metadata_json["tool_policy"] == "disabled_by_default"
    assert branch_session.transcript_metadata_json["max_turns"] == 1
    assert [item["content"] for item in copied] == ["question", "answer"]
    assert result.run_request is not None
    assert result.run_request.content == "What does btw mean here?"
    assert result.run_request.display_content == "btw: What does this mean?"
    assert result.run_request.extra_metadata["side_session"] is True
    assert result.run_request.extra_metadata["tool_policy"] == "disabled_by_default"
    assert result.run_request.extra_metadata["max_turns"] == 1


@pytest.mark.asyncio
async def test_branch_maps_cloud_session_to_worktree_and_workspace_context_hooks(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    anchor = _event(
        session_id=source_session_id,
        sequence=10,
        event_type="user_message",
        role="user",
        content="fork here",
    )
    db = _FakeDB(anchor=anchor, prefix=[])
    captured = []

    async def fake_emit(event, **kwargs):
        captured.append((event.value, kwargs))
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=SimpleNamespace(
            id=source_session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="Original",
            parent_session_id=None,
            root_session_id=None,
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        ),
        mode="fork",
        anchor_event_id=anchor.id,
    )

    branch_id = str(result.session.id)
    assert [event for event, _kwargs in captured] == [
        "worktree_create",
        "cwd_changed",
        "workspace_context_changed",
    ]
    assert captured[0][1]["metadata"]["cloud_workspace_kind"] == "conversation_branch"
    assert captured[0][1]["metadata"]["target_session_id"] == branch_id
    assert captured[1][1]["metadata"]["old_cwd"] == f"session://{source_session_id}/workspace"
    assert captured[1][1]["metadata"]["new_cwd"] == f"session://{branch_id}/workspace"


@pytest.mark.asyncio
async def test_worktree_create_hook_can_block_branch_before_mutation(monkeypatch):
    from app.runtime.hooks import HookResult
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    source_session_id = uuid4()
    anchor = _event(
        session_id=source_session_id,
        sequence=10,
        event_type="user_message",
        role="user",
        content="fork here",
    )
    db = _FakeDB(anchor=anchor, prefix=[])

    async def fake_emit(_event, **_kwargs):
        return HookResult(block=True, reason="branch denied")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    with pytest.raises(Exception) as exc_info:
        await create_conversation_branch(
            db=db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            user=SimpleNamespace(id=uuid4()),
            source_session=SimpleNamespace(
                id=source_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=uuid4(),
                title="Original",
                parent_session_id=None,
                root_session_id=None,
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            ),
            mode="fork",
            anchor_event_id=anchor.id,
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert db.added == []


def _v2_event(
    *,
    session_id,
    sequence,
    item_id=None,
    item_kind="human_input",
    lifecycle="accepted",
    content_parts=None,
):
    resolved_item = item_id or uuid4()
    if content_parts is None:
        content_parts = [{"type": "text", "text": f"v2 prompt {sequence}"}] if item_kind == "human_input" else []
    return SimpleNamespace(
        id=uuid4(),
        sequence=sequence,
        item_id=resolved_item,
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=session_id,
        run_id=None,
        parent_event_id=None,
        root_session_id=None,
        parent_session_id=None,
        message_id=uuid4(),
        actor_type="user" if item_kind == "human_input" else "assistant",
        event_type=f"{item_kind}.{lifecycle}",
        item_kind=item_kind,
        lifecycle=lifecycle,
        visibility_scope="direct_user",
        listed_surface="chat",
        content="",
        parts_json=None,
        metadata_json={
            "v2_payload": (
                {"input_id": str(resolved_item), "content_parts": content_parts}
                if item_kind == "human_input"
                else {"phase": "final"}
            ),
            "actor": {"type": "user" if item_kind == "human_input" else "assistant"},
        },
        created_at=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )


def _branch_source_session(agent_id, tenant_id, user_id):
    from datetime import datetime as _datetime

    source_session_id = uuid4()
    session = SimpleNamespace(
        id=source_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title="Original",
        parent_session_id=None,
        root_session_id=None,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        created_at=_datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc),
        last_message_at=_datetime(2026, 8, 26, 11, 30, tzinfo=timezone.utc),
        transcript_metadata_json={},
    )
    return source_session_id, session


@pytest.mark.asyncio
async def test_branch_at_v2_human_input_checkpoint_uses_exact_draft_and_skips_anchor(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id, source_session = _branch_source_session(agent_id, tenant_id, user_id)
    earlier = _event(
        session_id=source_session_id,
        sequence=2,
        event_type="assistant_message",
        role="assistant",
        content="prior answer",
    )
    anchor = _v2_event(
        session_id=source_session_id,
        sequence=10,
        content_parts=[{"type": "text", "text": "J-06 exercise the production journey contract."}],
    )
    db = _FakeDB(anchor=anchor, prefix=[earlier])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="branch",
        anchor_event_id=anchor.id,
        title="J-06 branch",
    )

    assert result.branch["draft_content"] == "J-06 exercise the production journey contract."
    assert result.branch["anchor_event_id"] == str(anchor.id)
    assert [kwargs.get("event_type") for kwargs in copied] == ["assistant_message"]
    branch_session = db.added[0]
    assert branch_session.parent_session_id == source_session_id
    assert branch_session.root_session_id == source_session_id


@pytest.mark.asyncio
async def test_branch_at_revised_v2_checkpoint_does_not_copy_superseded_accepted_bytes(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id, source_session = _branch_source_session(agent_id, tenant_id, user_id)
    item_id = uuid4()
    # Production order: a revision replaces an unbound input, so the revised
    # lifecycle directly follows its superseded accepted row.
    accepted = _v2_event(
        session_id=source_session_id,
        sequence=1,
        item_id=item_id,
        content_parts=[{"type": "text", "text": "superseded draft"}],
    )
    anchor = _v2_event(
        session_id=source_session_id,
        sequence=2,
        item_id=item_id,
        lifecycle="revised",
        content_parts=[{"type": "text", "text": "revised exact prompt"}],
    )
    db = _FakeDB(anchor=anchor, prefix=[accepted])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="branch",
        anchor_event_id=anchor.id,
    )

    assert result.branch["draft_content"] == "revised exact prompt"
    assert copied == []


@pytest.mark.asyncio
async def test_branch_prefix_copy_renders_v2_human_input_content_once_per_item(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id, source_session = _branch_source_session(agent_id, tenant_id, user_id)
    item_id = uuid4()
    accepted = _v2_event(
        session_id=source_session_id,
        sequence=2,
        item_id=item_id,
        content_parts=[{"type": "text", "text": "old prompt"}],
    )
    revised = _v2_event(
        session_id=source_session_id,
        sequence=5,
        item_id=item_id,
        lifecycle="revised",
        content_parts=[{"type": "text", "text": "new prompt"}],
    )
    answer = _event(
        session_id=source_session_id,
        sequence=8,
        event_type="assistant_message",
        role="assistant",
        content="answer",
    )
    anchor = _event(
        session_id=source_session_id,
        sequence=10,
        event_type="assistant_message",
        role="assistant",
        content="latest answer",
    )
    db = _FakeDB(anchor=anchor, prefix=[accepted, revised, answer])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="branch",
        anchor_event_id=anchor.id,
    )

    assert [kwargs.get("event_type") for kwargs in copied] == ["human_input.revised", "assistant_message"]
    assert copied[0]["content"] == "new prompt"
    assert copied[0]["role"] == "user"


@pytest.mark.asyncio
async def test_regenerate_from_v2_assistant_final_uses_latest_v2_user_prompt(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id, source_session = _branch_source_session(agent_id, tenant_id, user_id)
    user_prompt = _v2_event(
        session_id=source_session_id,
        sequence=1,
        content_parts=[{"type": "text", "text": "v2 user prompt exact"}],
    )
    anchor = _v2_event(session_id=source_session_id, sequence=10, item_kind="assistant_final", lifecycle="completed")
    db = _FakeDB(anchor=anchor, prefix=[user_prompt])
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(
            event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id")
        )

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="regenerate",
        anchor_event_id=anchor.id,
    )

    assert result.run_request is not None
    assert result.run_request.content == "v2 user prompt exact"
    assert result.run_request.append_user_message is False
    assert result.run_request.extra_metadata["regenerate_prompt"] == "v2 user prompt exact"


@pytest.mark.asyncio
async def test_branch_draft_renders_single_part_content_key_exact_bytes(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id, source_session = _branch_source_session(agent_id, tenant_id, user_id)
    anchor = _v2_event(
        session_id=source_session_id,
        sequence=1,
        content_parts=[{"type": "text", "content": "exact-api-bytes"}],
    )
    db = _FakeDB(anchor=anchor, prefix=[])

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(event_id=uuid4(), sequence=1, message_id=None)

    monkeypatch.setattr("app.services.conversation_branch_service.append_session_event", fake_append_session_event)

    result = await create_conversation_branch(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        user=SimpleNamespace(id=user_id),
        source_session=source_session,
        mode="branch",
        anchor_event_id=anchor.id,
    )

    assert result.branch["draft_content"] == "exact-api-bytes"
