from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.audit import ChatMessage
from app.models.gateway_message import GatewayMessage


@pytest.fixture(autouse=True)
def _stub_activity_logger(monkeypatch):
    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)


@pytest.mark.asyncio
async def test_invoke_agent_message_runtime_delegates_to_runtime(monkeypatch):
    from app.services.agent_tool_domains.messaging import _invoke_agent_message_runtime

    source_agent_id = uuid4()
    target_id = uuid4()
    owner_id = uuid4()
    session_agent_id = uuid4()
    participant_id = uuid4()
    target = SimpleNamespace(
        id=target_id,
        name="Target Agent",
        role_description="Helpful agent",
        max_tool_rounds=9,
    )
    target_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )
    conversation_messages = [{"role": "user", "content": "[From Source] hello"}]

    captured = {}
    orchestrator_executor = object()

    async def fake_delegate(**kwargs):
        captured["kwargs"] = kwargs
        return "target reply"

    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging._build_agent_message_tool_executor",
        lambda *args, **kwargs: orchestrator_executor,
    )
    monkeypatch.setattr("app.agents.orchestrator.delegate_to_agent", fake_delegate)

    reply = await _invoke_agent_message_runtime(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        from_agent_id=source_agent_id,
        owner_id=owner_id,
        session_id="session-1",
        session_agent_id=session_agent_id,
        participant_id=participant_id,
    )

    assert reply == "target reply"
    assert captured["kwargs"]["target"] is target
    assert captured["kwargs"]["target_model"] is target_model
    assert captured["kwargs"]["conversation_messages"] == conversation_messages
    assert captured["kwargs"]["owner_id"] == owner_id
    assert captured["kwargs"]["session_id"] == "session-1"
    assert captured["kwargs"]["parent_agent_id"] == source_agent_id
    assert captured["kwargs"]["parent_session_id"] == "session-1"
    assert captured["kwargs"]["tool_executor"] is orchestrator_executor
    assert captured["kwargs"]["max_tool_rounds"] == 9
    assert captured["kwargs"]["interaction_type"] == "agent_message"
    assert "Agent-to-Agent Message" in captured["kwargs"]["system_prompt_suffix"]


@pytest.mark.asyncio
async def test_build_agent_message_tool_executor_persists_tool_calls(monkeypatch):
    from app.services.agent_tool_domains.messaging import _build_agent_message_tool_executor

    target_id = uuid4()
    owner_id = uuid4()
    participant_id = uuid4()
    calls = {}

    async def fake_execute_tool(tool_name, args, agent_id, user_id):
        calls["execute"] = (tool_name, args, agent_id, user_id)
        return "TOOL_RESULT"

    async def fake_persist(**kwargs):
        calls["persist"] = kwargs

    monkeypatch.setattr("app.services.agent_tool_domains.messaging.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging._persist_agent_tool_call", fake_persist)

    executor = _build_agent_message_tool_executor(
        target_agent_id=target_id,
        owner_id=owner_id,
        session_agent_id=uuid4(),
        session_id="session-2",
        participant_id=participant_id,
    )

    result = await executor("read_file", {"path": "skills/test/SKILL.md"})

    assert result == "TOOL_RESULT"
    assert calls["execute"] == (
        "read_file",
        {"path": "skills/test/SKILL.md"},
        target_id,
        owner_id,
    )
    assert calls["persist"]["tool_name"] == "read_file"
    assert calls["persist"]["tool_args"] == {"path": "skills/test/SKILL.md"}
    assert calls["persist"]["tool_result"] == "TOOL_RESULT"
    assert calls["persist"]["participant_id"] == participant_id


@pytest.mark.asyncio
async def test_delegate_to_agent_async_passes_tool_profile(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None)
    captured = {}

    async def fake_resolve(_from_agent_id, _agent_name, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(task_id="task-1", trace_id="trace-1", target_name="Target Agent")

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    result = await _delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Target Agent",
            "message": "search memory and summarize the result",
            "tool_profile": "memory_readonly",
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-1"
    assert captured["kwargs"]["policy"].tool_profile == "memory_readonly"


@pytest.mark.asyncio
async def test_delegate_to_agent_async_accepts_research_readonly_profile(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None)
    captured = {}

    async def fake_resolve(_from_agent_id, _agent_name, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(task_id="task-2", trace_id="trace-2", target_name="Target Agent")

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    result = await _delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Target Agent",
            "message": "research the latest competitive landscape and summarize it",
            "tool_profile": "research_readonly",
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-2"
    assert captured["kwargs"]["policy"].tool_profile == "research_readonly"


@pytest.mark.asyncio
async def test_send_message_to_agent_accepts_target_agent_id_for_precise_resolution(monkeypatch):
    from app.services.agent_tool_domains.messaging import _send_message_to_agent

    from_agent_id = uuid4()
    target_agent_id = uuid4()
    tenant_id = uuid4()
    source_agent = SimpleNamespace(id=from_agent_id, name="Source Agent", creator_id=uuid4(), tenant_id=tenant_id)
    target = SimpleNamespace(id=target_agent_id, name="Target Agent", status="stopped", tenant_id=tenant_id)
    captured = {"sql": []}

    class _ScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])

    class _FakeDB:
        def __init__(self):
            self._results = [_ScalarResult(source_agent), _ScalarResult(target)]

        async def execute(self, stmt):
            captured["sql"].append(str(stmt))
            return self._results.pop(0)

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.services.agent_tool_domains.messaging.async_session", lambda: _FakeSessionCtx())

    result = await _send_message_to_agent(
        from_agent_id,
        {
            "agent_name": "Target Agent",
            "target_agent_id": str(target_agent_id),
            "message": "Please verify the contract",
            "msg_type": "consult",
        },
    )

    assert "currently stopped" in result
    assert "agents.id =" in captured["sql"][1]
    assert "lower(agents.name) LIKE lower" not in captured["sql"][1]


@pytest.mark.asyncio
async def test_send_message_to_agent_rejects_task_delegate_msg_type():
    from app.services.agent_tool_domains.messaging import _send_message_to_agent

    result = await _send_message_to_agent(
        uuid4(),
        {
            "agent_name": "Target Agent",
            "message": "Please implement this in the background",
            "msg_type": "task_delegate",
        },
    )

    assert "Use delegate_to_agent" in result


@pytest.mark.asyncio
async def test_send_message_to_openclaw_target_persists_canonical_agent_pair_transcript(monkeypatch):
    from app.services.agent_tool_domains.messaging import _send_message_to_agent

    from_agent_id = uuid4()
    target_agent_id = uuid4()
    source_agent = SimpleNamespace(
        id=from_agent_id,
        name="Source Agent",
        creator_id=uuid4(),
        tenant_id=uuid4(),
    )
    target_agent = SimpleNamespace(
        id=target_agent_id,
        name="Target OpenClaw",
        status="running",
        agent_type="openclaw",
        creator_id=uuid4(),
        tenant_id=source_agent.tenant_id,
        openclaw_last_seen=None,
    )
    source_participant = SimpleNamespace(id=uuid4())
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)

    class _ScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])

    class _FakeDB:
        def __init__(self):
            self._results = [
                _ScalarResult(source_agent),
                _ScalarResult(target_agent),
                _ScalarResult(source_participant),
            ]
            self.added = []
            self.commit_count = 0

        async def execute(self, _stmt):
            if not self._results:
                raise AssertionError("Unexpected execute call")
            return self._results.pop(0)

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            self.commit_count += 1

    class _FakeSessionCtx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_db = _FakeDB()

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        return chat_session

    monkeypatch.setattr("app.services.agent_tool_domains.messaging.async_session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging.find_or_create_agent_pair_session",
        fake_find_or_create_agent_pair_session,
    )
    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging.session_conversation_id",
        lambda _session: "agent-pair-conv",
    )

    result = await _send_message_to_agent(
        from_agent_id,
        {
            "agent_name": target_agent.name,
            "target_agent_id": str(target_agent_id),
            "message": "Please send the release summary.",
            "msg_type": "consult",
        },
    )

    assert "queued" in result
    outbound_gateway = next(obj for obj in fake_db.added if isinstance(obj, GatewayMessage))
    outbound_chat = next(obj for obj in fake_db.added if isinstance(obj, ChatMessage))
    assert outbound_gateway.agent_id == target_agent.id
    assert outbound_gateway.sender_agent_id == from_agent_id
    assert outbound_gateway.sender_user_id is None
    assert outbound_gateway.conversation_id == "agent-pair-conv"
    assert outbound_gateway.content == "Please send the release summary."
    assert outbound_chat.agent_id == chat_session.agent_id
    assert outbound_chat.conversation_id == "agent-pair-conv"
    assert outbound_chat.role == "user"
    assert outbound_chat.content == "Please send the release summary."
    assert outbound_chat.user_id == target_agent.creator_id
    assert outbound_chat.participant_id == source_participant.id
    assert chat_session.last_message_at is not None
    assert fake_db.commit_count == 1


@pytest.mark.asyncio
async def test_list_async_tasks_filters_in_memory_fallback(monkeypatch):
    from app.agents.orchestrator import check_async_delegation, delegate_async
    from app.services.agent_tool_domains.messaging import _list_async_tasks

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    async def fake_list_runtime_task_records(**_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.services.runtime_task_service.list_runtime_task_records", fake_list_runtime_task_records)

    target = SimpleNamespace(id=uuid4(), name="ScopedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()
    owner_b = uuid4()

    handle_a = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-a"}],
        owner_id=uuid4(),
        session_id="msg-scope-a",
        parent_agent_id=owner_a,
    )
    handle_b = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-b"}],
        owner_id=uuid4(),
        session_id="msg-scope-b",
        parent_agent_id=owner_b,
    )

    payload = json.loads(await _list_async_tasks(owner_a))
    task_ids = {task["task_id"] for task in payload}
    assert handle_a.task_id in task_ids
    assert handle_b.task_id not in task_ids

    never_finish.set()
    await check_async_delegation(handle_a.task_id)
    await check_async_delegation(handle_b.task_id)


@pytest.mark.asyncio
async def test_check_async_task_rejects_other_agent_when_db_lookup_unavailable(monkeypatch):
    from app.agents.orchestrator import check_async_delegation, delegate_async
    from app.services.agent_tool_domains.messaging import _check_async_task

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    async def fake_get_runtime_task_record(_task_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.services.runtime_task_service.get_runtime_task_record", fake_get_runtime_task_record)

    target = SimpleNamespace(id=uuid4(), name="ProtectedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()
    owner_b = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-a"}],
        owner_id=uuid4(),
        session_id="msg-check-a",
        parent_agent_id=owner_a,
    )

    result = await _check_async_task(owner_b, {"task_id": handle.task_id})
    assert "does not belong" in result

    never_finish.set()
    await check_async_delegation(handle.task_id)


@pytest.mark.asyncio
async def test_cancel_async_task_rejects_other_agent_when_db_lookup_unavailable(monkeypatch):
    from app.agents.orchestrator import check_async_delegation, delegate_async
    from app.services.agent_tool_domains.messaging import _cancel_async_task

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    async def fake_get_runtime_task_record(_task_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.services.runtime_task_service.get_runtime_task_record", fake_get_runtime_task_record)

    target = SimpleNamespace(id=uuid4(), name="ProtectedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()
    owner_b = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-a"}],
        owner_id=uuid4(),
        session_id="msg-cancel-a",
        parent_agent_id=owner_a,
    )

    result = await _cancel_async_task(owner_b, {"task_id": handle.task_id})
    assert "does not belong" in result

    never_finish.set()
    await check_async_delegation(handle.task_id)
