from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueuedDB:
    def __init__(self, values):
        self.values = list(values)
        self.added = []
        self.flushed = 0
        self.commits = 0

    async def execute(self, _stmt):
        if not self.values:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self.values.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_channel_legacy_runtime_writes_replayable_t0_turn(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.channel_agent_runtime import call_agent_llm

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    model_id = uuid4()
    tool_result_tail = "END_OF_CHANNEL_TOOL_RESULT"
    agent = SimpleNamespace(
        id=agent_id,
        name="Channel Agent",
        role_description="Assistant",
        tenant_id=None,
        primary_model_id=model_id,
        fallback_model_id=None,
        expires_at=None,
        deleted_at=None,
        deactivated_at=None,
        sponsor=None,
        sponsor_is_active=None,
    )
    model = SimpleNamespace(id=model_id, provider="openai", model="test-model", supports_vision=False)
    db = _QueuedDB([agent, model, None])
    forwarded_events: list[dict] = []

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_call_llm(*_args, **kwargs):
        assert kwargs["auto_close_session"] is False
        await kwargs["on_tool_call"](
            {
                "tool": "web_search",
                "status": "running",
                "args": {"q": "RWA"},
            }
        )
        await kwargs["on_tool_call"](
            {
                "tool": "web_search",
                "status": "done",
                "result": "x" * 2500 + tool_result_tail,
            }
        )
        return "channel final answer"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)

    reply = await call_agent_llm(
        db,
        agent_id,
        "用户通过渠道发来的原始消息",
        user_id=user_id,
        session_id=str(session_id),
        session_source="feishu",
        session_channel="feishu",
        on_tool_call=lambda event: forwarded_events.append(event),
    )

    assert reply == "channel final answer"
    assert [event["status"] for event in forwarded_events] == ["running", "done"]

    chat_messages = [item for item in db.added if isinstance(item, ChatMessage)]
    transcript_events = [item for item in db.added if isinstance(item, ChatTranscriptEvent)]
    assert chat_messages == []
    assert [event.event_type for event in transcript_events] == [
        "user_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [
        ("user_message", "user"),
        ("tool_call", "tool"),
        ("tool_result", "tool"),
        ("assistant_message", "assistant"),
        ("segment_boundary", "system"),
    ]
    assert events[0].content == "用户通过渠道发来的原始消息"
    assert events[2].content.endswith(tool_result_tail)
    assert events[3].content == "channel final answer"
    assert events[4].metadata["reason"] == "invoke_complete"
