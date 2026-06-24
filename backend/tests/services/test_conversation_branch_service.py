from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


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
        return SimpleNamespace(event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id"))

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
    assert copied == []
    assert result.run_request.content == "new wording"
    assert result.run_request.display_content == "new wording"
    assert result.run_request.append_user_message is True


@pytest.mark.asyncio
async def test_fork_branch_copies_prefix_through_anchor(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    user_event = _event(session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question")
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
        return SimpleNamespace(event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id"))

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
        mode="fork",
        anchor_event_id=assistant_event.id,
    )

    assert [item["content"] for item in copied] == ["question", "answer"]
    assert [item["metadata"]["copied_from_event_id"] for item in copied] == [str(user_event.id), str(assistant_event.id)]
    assert [item["bridge_to_t0"] for item in copied] == [False, False]
    assert [item["metadata"]["projection_only"] for item in copied] == [True, True]
    assert [item["metadata"]["semantic_memory_eligible"] for item in copied] == [False, False]
    assert [item["metadata"]["projection_source"] for item in copied] == [
        "conversation_branch_prefix",
        "conversation_branch_prefix",
    ]
    assert result.run_request is None


@pytest.mark.asyncio
async def test_rewind_branch_copies_prefix_before_user_checkpoint(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    first_user = _event(session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="first")
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
    copied = []

    async def fake_append_session_event(**kwargs):
        copied.append(kwargs)
        return SimpleNamespace(event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id"))

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
        mode="rewind",
        anchor_event_id=rewind_target.id,
    )

    assert [item["content"] for item in copied] == ["first", "answer"]
    assert result.run_request is None
    assert result.branch["mode"] == "rewind"
    assert result.branch["anchor_event_id"] == str(rewind_target.id)


@pytest.mark.asyncio
async def test_regenerate_branch_runs_from_previous_user_without_appending_duplicate(monkeypatch):
    from app.services.conversation_branch_service import create_conversation_branch

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    source_session_id = uuid4()
    user_event = _event(session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question")
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
        return SimpleNamespace(event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id"))

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
    user_event = _event(session_id=source_session_id, sequence=10, event_type="user_message", role="user", content="question")
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
        return SimpleNamespace(event_id=uuid4(), sequence=kwargs.get("sequence", 1), message_id=kwargs.get("message_id"))

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
