"""X3 — the member history endpoint serves the typed tool summary.

`GET /agents/{id}/sessions/{id}/transcript?schema_version=2` is the reload and
reconcile path: for a summarized tool result the member payload must carry the
typed summary and none of the raw result bytes, while an operator view keeps
them.
"""

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

    async def execute(self, stmt):
        if not self.values:
            return _Result(None)
        return _Result(self.values.pop(0))

    async def commit(self):
        return None


def _tool_result_row(name: str, result: str, *, url: str | None = None) -> SimpleNamespace:
    envelope = {
        "name": name,
        "args": ({"url": url} if url else {"query": "周报"}),
        "status": "done",
        "tool_call_id": "call_x3",
        "visibility": "collapsed",
        "result": result,
    }
    return SimpleNamespace(
        id=uuid4(),
        sequence=41,
        tenant_id=uuid4(),
        session_id=uuid4(),
        agent_id=uuid4(),
        run_id=None,
        message_id=None,
        schema_version=1,
        item_type="tool_result",
        item_status="succeeded",
        turn_id=str(uuid4()),
        causation_id=None,
        correlation_id=None,
        actor_type="tool",
        event_type="tool_result",
        visibility_scope="direct_user",
        listed_surface="chat",
        content=json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        parts_json=[],
        metadata_json={"source": "web_chat_runtime", "tool_name": name, "status": "done"},
        created_at="2026-11-12T10:00:00Z",
    )


def _setup(monkeypatch):
    import app.api.chat_sessions as api

    agent = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), agent_id=agent.id, user_id=uuid4())
    user = SimpleNamespace(id=session.user_id, role="member")

    async def fake_check_agent_history_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_check_agent_operator_reachability(_db, _user, _agent_id):
        return agent, "operator"

    monkeypatch.setattr(api, "check_agent_history_access", fake_check_agent_history_access)
    monkeypatch.setattr(api, "check_agent_operator_reachability", fake_check_agent_operator_reachability)
    return api, agent, session, user


@pytest.mark.asyncio
async def test_member_history_v2_serves_typed_summary_not_raw_result(monkeypatch):
    api, agent, session, user = _setup(monkeypatch)
    from app.services.session_delivery_cursor import SessionDeliveryCursor

    raw = (
        "## 读取到的记忆\n"
        "管理引用（不要向用户展示）：id=explicit_284ff1e71ca75b9f92800d2c6fc40ab9\n"
        "session-tool-invocation:4f10ca36-8bed-5bc0-88f2-ff509041ab08 scope=sessions"
    )
    rows = [_tool_result_row("load_memory", raw)]
    db = _DB(session, rows)

    cursor = SessionDeliveryCursor(mode="identity", event_count=41, storage_first_sequence=1, storage_last_sequence=41)

    async def fake_load_cursor(_db, *, session_id):
        return cursor

    async def fake_load_events(_db, **options):
        return [(rows[0], 41)]

    monkeypatch.setattr("app.services.session_delivery_cursor.load_session_delivery_cursor", fake_load_cursor)
    monkeypatch.setattr("app.services.session_delivery_cursor.load_session_delivery_events", fake_load_events)

    payload = await api.get_session_transcript(
        agent_id=agent.id,
        session_id=session.id,
        schema_version=2,
        current_user=user,
        db=db,
    )

    assert len(payload) == 1
    envelope = payload[0]
    body = envelope["payload"]
    rendered = " ".join(
        [str(body.get("result") or ""), str(body.get("content") or "")]
        + [str(part.get("text") or "") for part in body.get("parts") or [] if isinstance(part, dict)]
    )
    for leak in ("不要向用户展示", "284ff1e71ca75b9f92800d2c6fc40ab9", "4f10ca36", "scope=sessions", "读取到的记忆"):
        assert leak not in rendered, leak
    assert body["member_summary"] == {"kind": "memory_read", "failed": False}


@pytest.mark.asyncio
async def test_operator_history_v2_keeps_raw_result(monkeypatch):
    api, agent, session, user = _setup(monkeypatch)
    from app.services.session_delivery_cursor import SessionDeliveryCursor

    raw = "整页 Firecrawl 转储：[Syntax error in textmermaid version 11.16.1]"
    rows = [_tool_result_row("web_fetch", raw, url="https://deepwiki.com/react")]
    db = _DB(session, rows)

    cursor = SessionDeliveryCursor(mode="identity", event_count=41, storage_first_sequence=1, storage_last_sequence=41)

    async def fake_load_cursor(_db, *, session_id):
        return cursor

    async def fake_load_events(_db, **options):
        return [(rows[0], 41)]

    monkeypatch.setattr("app.services.session_delivery_cursor.load_session_delivery_cursor", fake_load_cursor)
    monkeypatch.setattr("app.services.session_delivery_cursor.load_session_delivery_events", fake_load_events)

    async def fake_authorize(**_kwargs):
        return "operator_inspect_grant"

    monkeypatch.setattr(api, "_authorize_loaded_session", fake_authorize)

    payload = await api.get_session_transcript(
        agent_id=agent.id,
        session_id=session.id,
        schema_version=2,
        operator_view=True,
        operator_reason="x3 test",
        current_user=user,
        db=db,
    )

    body = payload[0]["payload"]
    assert "Syntax error" in str(body.get("result") or "") + str(body.get("content") or "")
    assert "member_summary" not in body
