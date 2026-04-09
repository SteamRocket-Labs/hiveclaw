from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if not self._execute_values:
            raise AssertionError("No fake execute result prepared")
        return _FakeScalarResult(self._execute_values.pop(0))

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = uuid4()

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1


@pytest.mark.asyncio
async def test_execute_task_delegates_to_runtime_invoker(monkeypatch):
    from app.services.task_executor import execute_task

    task_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()

    task = SimpleNamespace(
        id=task_id,
        title="整理周报",
        description="汇总本周关键进展",
        type="todo",
        status="pending",
        completed_at=None,
        supervision_target_name="",
    )
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Ops Agent",
        role_description="Operations",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )

    setup_session = _FakeSession([task])
    model_session = _FakeSession([agent, model, None])
    final_session = _FakeSession([task])
    sessions = [setup_session, model_session, final_session]

    captured = {}
    activity_calls = []

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="任务已完成")

    async def fake_log_activity(*args, **kwargs):
        activity_calls.append((args, kwargs))

    monkeypatch.setattr("app.services.task_executor.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    await execute_task(task_id, agent_id)

    request = captured["request"]
    assert request.model is model
    assert request.agent_id == agent_id
    assert request.user_id == creator_id
    assert request.core_tools_only is True
    assert "TASK EXECUTION MODE" in request.system_prompt_suffix
    assert request.messages == [{
        "role": "user",
        "content": "[任务执行] 整理周报\n任务描述: 汇总本周关键进展\n\n请认真完成此任务，给出详细的执行结果。",
    }]
    assert request.memory_messages == request.messages
    assert request.session_context is not None
    assert request.session_context.source == "task"
    assert request.session_context.channel == "task"
    assert request.session_context.metadata["task_id"] == str(task_id)
    assert request.session_context.metadata["task_type"] == "todo"
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "agent_bot"
    assert request.execution_identity.identity_id == agent_id
    assert request.execution_identity.label == "Agent: Ops Agent (task)"

    assert task.status == "done"
    assert task.completed_at is not None
    assert any("✅ 任务完成" in entry.content and "任务已完成" in entry.content for entry in final_session.added)
    assert activity_calls


@pytest.mark.asyncio
async def test_execute_task_persists_reflection_session_and_tool_calls(monkeypatch):
    from app.services.task_executor import execute_task

    task_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()
    participant_id = uuid4()

    task = SimpleNamespace(
        id=task_id,
        title="准备竞品分析",
        description="整理最近 3 家竞品动态",
        type="todo",
        status="pending",
        completed_at=None,
        supervision_target_name="",
    )
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Research Agent",
        role_description="Research",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        max_tool_rounds=12,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=participant_id)

    setup_session = _FakeSession([task])
    prepare_session = _FakeSession([agent, model, participant])
    tool_call_session = _FakeSession([])
    final_session = _FakeSession([task])
    sessions = [setup_session, prepare_session, tool_call_session, final_session]

    async def fake_invoke_agent(request):
        await request.on_tool_call(
            {
                "status": "done",
                "name": "web_search",
                "args": {"query": "competitor updates"},
                "result": "3 sources found",
            }
        )
        return SimpleNamespace(content="任务已完成，已整理竞品动态。")

    monkeypatch.setattr("app.services.task_executor.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)
    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    await execute_task(task_id, agent_id)

    created_session = next(item for item in prepare_session.added if getattr(item, "source_channel", None) == "task")
    user_prompt = next(item for item in prepare_session.added if getattr(item, "role", None) == "user")
    tool_call = next(item for item in tool_call_session.added if getattr(item, "role", None) == "tool_call")
    assistant_reply = next(item for item in final_session.added if getattr(item, "role", None) == "assistant")

    assert created_session.title.startswith("🧾 Task:")
    assert user_prompt.conversation_id == str(created_session.id)
    assert "准备竞品分析" in user_prompt.content
    assert '"name": "web_search"' in tool_call.content
    assert assistant_reply.conversation_id == str(created_session.id)
    assert "任务已完成" in assistant_reply.content
