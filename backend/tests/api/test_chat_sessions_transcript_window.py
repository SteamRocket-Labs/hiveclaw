"""B4 — transcript windowing: backward (tail) reads must not break the
existing after_sequence incremental contract (owner-pinned constraint,
docs/performance-slimming-plan-2026-07-02.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        value = self._value if isinstance(self._value, list) else []
        return SimpleNamespace(all=lambda: value)


class _DB:
    def __init__(self, *values):
        self.values = list(values)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self.values:
            return _Result(None)
        return _Result(self.values.pop(0))

    async def commit(self):
        return None


def _event(session_id, sequence):
    return SimpleNamespace(
        id=uuid4(),
        session_id=session_id,
        run_id=None,
        message_id=None,
        sequence=sequence,
        event_type="user_message",
        actor_type="user",
        content=f"c{sequence}",
        parts_json=[],
        metadata_json={"role": "user"},
        visibility_scope="session",
        listed_surface="chat",
        created_at=None,
    )


def _setup(monkeypatch):
    import app.api.chat_sessions as api

    agent = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), agent_id=agent.id, user_id=uuid4())
    user = SimpleNamespace(id=session.user_id, role="member")

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        api,
        "_serialize_transcript_event",
        lambda event, **_kwargs: {"sequence": event.sequence},
    )
    return api, agent, session, user


@pytest.mark.asyncio
async def test_transcript_forward_after_sequence_contract_unchanged(monkeypatch):
    api, agent, session, user = _setup(monkeypatch)
    rows = [_event(session.id, seq) for seq in (6, 7, 8)]
    db = _DB(session, rows)

    payload = await api.get_session_transcript(
        agent_id=agent.id,
        session_id=session.id,
        after_sequence=5,
        current_user=user,
        db=db,
    )

    assert [item["sequence"] for item in payload] == [6, 7, 8]


@pytest.mark.asyncio
async def test_transcript_backward_returns_latest_window_ascending(monkeypatch):
    api, agent, session, user = _setup(monkeypatch)
    # DB returns DESC rows for backward queries; endpoint must re-ascend.
    rows = [_event(session.id, seq) for seq in (30, 29, 28)]
    db = _DB(session, rows)

    payload = await api.get_session_transcript(
        agent_id=agent.id,
        session_id=session.id,
        direction="backward",
        limit=3,
        current_user=user,
        db=db,
    )

    assert [item["sequence"] for item in payload] == [28, 29, 30]


@pytest.mark.asyncio
async def test_transcript_before_sequence_pages_older_history_ascending(monkeypatch):
    api, agent, session, user = _setup(monkeypatch)
    rows = [_event(session.id, seq) for seq in (27, 26, 25)]
    db = _DB(session, rows)

    payload = await api.get_session_transcript(
        agent_id=agent.id,
        session_id=session.id,
        before_sequence=28,
        limit=3,
        current_user=user,
        db=db,
    )

    assert [item["sequence"] for item in payload] == [25, 26, 27]


@pytest.mark.asyncio
async def test_transcript_default_limit_tightened_to_200(monkeypatch):
    import inspect

    import app.api.chat_sessions as api

    signature = inspect.signature(api.get_session_transcript)
    assert signature.parameters["limit"].default == 200


def test_transcript_projection_caps_oversize_event_payloads():
    import app.api.chat_sessions as api

    event = SimpleNamespace(
        id=uuid4(),
        session_id=uuid4(),
        run_id=None,
        message_id=None,
        sequence=42,
        event_type="tool_result",
        actor_type="tool",
        visibility_scope="session",
        listed_surface="chat",
        content="c" * 30_000,
        parts_json=[
            {
                "type": "tool_result",
                "content": "p" * 30_000,
                "summary": "s" * 12_000,
                "raw": {"rows": ["r" * 1_000 for _ in range(30)]},
            }
        ],
        metadata_json={
            "role": "tool",
            "summary": "m" * 12_000,
            "raw": {"payload": "x" * 30_000},
            "debug": {"payload": "d" * 30_000},
        },
        created_at=None,
    )

    payload = api._serialize_transcript_event(event)

    assert payload["schema"] == "hive.thread_item.v1"
    assert payload["schema_version"] == 1
    assert payload["item_type"] == "tool_result"
    assert payload["item_status"] == "succeeded"
    assert payload["item_data"]["event_type"] == "tool_result"
    assert payload["metadata"]["_payload_truncated"] is True
    assert "raw" not in payload["metadata"]
    assert "debug" not in payload["metadata"]
    assert payload["parts"][0]["_payload_truncated"] is True
    assert len(payload["content"]) <= api._TRANSCRIPT_CONTENT_CHAR_LIMIT + len("...[truncated]")
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 12_000


def test_transcript_projection_preserves_full_user_visible_model_answer() -> None:
    import app.api.chat_sessions as api

    tail = "DECISIVE_MODEL_OUTPUT_TAIL"
    content = ("answer evidence " * 1_000) + tail
    event = SimpleNamespace(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        message_id=uuid4(),
        sequence=43,
        event_type="assistant_message",
        actor_type="assistant",
        visibility_scope="direct_user",
        listed_surface="chat",
        content=content,
        parts_json=[],
        metadata_json={"role": "assistant"},
        created_at=None,
    )

    payload = api._serialize_transcript_event(event)

    assert payload["content"] == content
    assert tail in payload["content"]
    assert payload["metadata"].get("_payload_truncated") is not True


def test_transcript_projection_preserves_large_interactive_tool_card_json():
    import app.api.chat_sessions as api

    questions = [
        {
            "question": "这次 Agent Team 跑哪个方向的「创新型金融模式」报告?",
            "header": "报告方向",
            "options": [
                {
                    "label": f"方向 {index}",
                    "description": "ABS 优先级/劣后级 + 永续债转股权双重结构，适用于基础设施/新能源/产业基金等长期资本占用场景。"
                    * 8,
                }
                for index in range(8)
            ],
            "multiSelect": False,
        }
    ]
    result = {
        "status": "awaiting_user_clarification",
        "questions": questions,
        "blocking": True,
        "next_action": "END your turn now — the question card is shown to the user.",
    }
    content = json.dumps(
        {
            "name": "ask_user_question",
            "args": {"questions": questions},
            "status": "done",
            "tool_call_id": "toolu_question",
            "step_id": "tool:toolu_question",
            "visibility": "collapsed",
            "reasoning_content": "reasoning " * 900,
            "result": json.dumps(result, ensure_ascii=False),
            "content_replacement": {
                "inline_content": json.dumps(result, ensure_ascii=False),
                "inline_chars": len(json.dumps(result, ensure_ascii=False)),
            },
        },
        ensure_ascii=False,
    )
    assert len(content) > api._TRANSCRIPT_CONTENT_CHAR_LIMIT

    event = SimpleNamespace(
        id=uuid4(),
        session_id=uuid4(),
        run_id=None,
        message_id=None,
        sequence=43,
        event_type="tool_result",
        actor_type="tool",
        visibility_scope="session",
        listed_surface="chat",
        content=content,
        parts_json=[],
        metadata_json={"role": "tool_call", "tool_name": "ask_user_question"},
        created_at=None,
    )

    payload = api._serialize_transcript_event(event)
    envelope = json.loads(payload["content"])
    card = json.loads(envelope["result"])

    assert envelope["name"] == "ask_user_question"
    assert envelope["status"] == "done"
    assert envelope["tool_call_id"] == "toolu_question"
    assert "reasoning_content" not in envelope
    assert card["status"] == "awaiting_user_clarification"
    assert card["questions"][0]["question"] == questions[0]["question"]
    assert card["questions"][0]["options"][7]["label"] == "方向 7"
    assert payload["metadata"]["_payload_truncated"] is True


def test_transcript_projection_still_caps_generic_success_tool_payloads():
    import app.api.chat_sessions as api

    content = json.dumps(
        {
            "name": "read_file",
            "status": "done",
            "result": json.dumps({"status": "success", "content": "x" * 30_000}),
        },
        ensure_ascii=False,
    )
    event = SimpleNamespace(
        id=uuid4(),
        session_id=uuid4(),
        run_id=None,
        message_id=None,
        sequence=44,
        event_type="tool_result",
        actor_type="tool",
        visibility_scope="session",
        listed_surface="chat",
        content=content,
        parts_json=[],
        metadata_json={"role": "tool_call", "tool_name": "read_file"},
        created_at=None,
    )

    payload = api._serialize_transcript_event(event)

    assert payload["metadata"]["_payload_truncated"] is True
    assert len(payload["content"]) <= api._TRANSCRIPT_CONTENT_CHAR_LIMIT + len("...[truncated]")
