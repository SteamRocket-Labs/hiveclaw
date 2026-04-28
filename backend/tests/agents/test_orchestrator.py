from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _stub_activity_logger(monkeypatch):
    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)


@pytest.mark.asyncio
async def test_delegate_to_agent_builds_runtime_request(monkeypatch):
    from app.agents.orchestrator import delegate_to_agent

    target = SimpleNamespace(
        id=uuid4(),
        name="Target Agent",
        role_description="Helpful",
    )
    target_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )
    tool_executor = object()
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="delegated reply")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    reply = await delegate_to_agent(
        target=target,
        target_model=target_model,
        conversation_messages=[{"role": "user", "content": "hello"}],
        owner_id=uuid4(),
        session_id="session-1",
        tool_executor=tool_executor,
        system_prompt_suffix="A2A_SUFFIX",
        max_tool_rounds=7,
    )

    request = captured["request"]
    assert reply == "delegated reply"
    assert request.agent_id == target.id
    assert request.agent_name == "Target Agent"
    assert request.role_description == "Helpful"
    assert request.model is target_model
    assert len(request.messages) == 1
    assert request.messages[0]["role"] == "user"
    assert "Delegated Task Brief" in request.messages[0]["content"]
    assert "hello" in request.messages[0]["content"]
    assert request.memory_messages == []
    assert request.memory_session_id == "session-1"
    assert request.tool_executor is tool_executor
    assert request.session_context is not None
    assert request.session_context.source == "agent"
    assert request.session_context.channel == "agent"
    assert request.session_context.session_id == "session-1"
    assert request.core_tools_only is True
    assert request.excluded_tool_names == (
        "delegate_to_agent",
        "send_message_to_agent",
        "set_trigger",
        "send_channel_file",
        "check_async_task",
        "cancel_async_task",
        "list_async_tasks",
        "save_skill",
        "save_memory",
        "search_memory",
    )
    assert request.max_tool_rounds == 7
    assert "A2A_SUFFIX" in request.system_prompt_suffix
    assert "delegated worker" in request.system_prompt_suffix.lower()
    # PR-16 replaced the markdown "### Return format" with the `<return_format>` XML
    # block; the Completed/Evidence/Blockers triad remains the parent-parsed contract.
    assert "<return_format>" in request.system_prompt_suffix
    assert "Completed:" in request.system_prompt_suffix
    assert "Evidence:" in request.system_prompt_suffix
    assert "Blockers:" in request.system_prompt_suffix
    assert "Do NOT read or write long-term memory" in request.system_prompt_suffix
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_safe"
    assert request.session_context.metadata["delegation_memory_policy"] == "isolated_no_long_term_memory"


@pytest.mark.asyncio
async def test_delegate_to_agent_enforces_depth_limit(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")

    async def _unexpected_invoke(_request):
        raise AssertionError("invoke_agent should not be called when depth limit is exceeded")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _unexpected_invoke)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=uuid4(),
            session_id="session-1",
            depth=3,
            policy=OrchestrationPolicy(max_depth=2),
        )
    )

    assert result.timed_out is False
    assert result.depth_limited is True
    assert "delegation depth limit" in result.content.lower()


@pytest.mark.asyncio
async def test_delegate_to_agent_applies_timeout_and_trace_metadata(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    owner_id = uuid4()

    async def fake_invoke_agent(request):
        metadata = request.session_context.metadata
        assert metadata["delegation"] is True
        assert metadata["delegation_depth"] == 1
        assert metadata["delegation_parent_agent_id"] == "source-agent"
        assert metadata["delegation_parent_session_id"] == "parent-session"
        assert metadata["delegation_trace_id"] == "trace-123"
        await asyncio.sleep(0.05)
        return SimpleNamespace(content="late reply")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=owner_id,
            session_id="child-session",
            parent_agent_id="source-agent",
            parent_session_id="parent-session",
            trace_id="trace-123",
            depth=1,
            policy=OrchestrationPolicy(timeout_seconds=0.01),
        )
    )

    assert result.timed_out is True
    assert result.depth_limited is False
    assert result.trace_id == "trace-123"
    assert result.child_session_id == "child-session"


@pytest.mark.asyncio
async def test_delegate_to_agent_supports_memory_readonly_profile(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "search past memory and summarize it"}],
            owner_id=uuid4(),
            session_id="memory-child",
            policy=OrchestrationPolicy(tool_profile="memory_readonly"),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert "search_memory" not in request.excluded_tool_names
    assert "save_skill" in request.excluded_tool_names
    assert "save_memory" in request.excluded_tool_names
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_memory_readonly"
    assert request.session_context.metadata["delegation_memory_policy"] == "read_only_long_term_memory"
    assert "You MAY read long-term memory" in request.system_prompt_suffix


@pytest.mark.asyncio
async def test_delegate_to_agent_supports_review_readonly_profile(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Reviewer", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="reviewed")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "review these files and identify risks"}],
            owner_id=uuid4(),
            session_id="review-child",
            policy=OrchestrationPolicy(tool_profile="review_readonly"),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert request.core_tools_only is False
    assert request.allowed_tool_names == (
        "list_files",
        "read_file",
        "glob_search",
        "grep_search",
        "load_skill",
        "tool_search",
        "search_memory",
        "get_current_time",
    )
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_review_readonly"
    assert request.session_context.metadata["delegation_memory_policy"] == "read_only_long_term_memory"
    assert "Do NOT edit files" in request.system_prompt_suffix


@pytest.mark.asyncio
async def test_delegate_to_agent_supports_research_readonly_profile(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Researcher", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="researched")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "research the latest market movement"}],
            owner_id=uuid4(),
            session_id="research-child",
            policy=OrchestrationPolicy(tool_profile="research_readonly"),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert request.core_tools_only is False
    assert request.allowed_tool_names == (
        "list_files",
        "read_file",
        "glob_search",
        "grep_search",
        "load_skill",
        "tool_search",
        "search_memory",
        "get_current_time",
        "web_fetch",
        "web_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
    )
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_research_readonly"
    assert request.session_context.metadata["delegation_memory_policy"] == "read_only_long_term_memory"
    assert "You MAY browse and retrieve external sources" in request.system_prompt_suffix


@pytest.mark.asyncio
async def test_delegate_async_returns_handle_immediately(monkeypatch):
    from app.agents.orchestrator import delegate_async, check_async_delegation

    completed = asyncio.Event()

    async def fake_invoke(invocation):
        await completed.wait()
        return SimpleNamespace(content="async result")

    async def fake_create_task_record(**kwargs):
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*args, **kwargs):
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do research"}],
        owner_id=uuid4(),
        session_id="sess-1",
        parent_agent_id=uuid4(),
    )

    assert handle.task_id
    assert handle.target_name == "Worker"

    # Task should be running
    status = await check_async_delegation(handle.task_id)
    assert status["status"] == "running"

    # Let the task complete
    completed.set()
    await asyncio.sleep(0.05)

    # Now it should be completed
    status = await check_async_delegation(handle.task_id)
    assert status["status"] == "completed"
    assert status["result"] == "async result"


@pytest.mark.asyncio
async def test_delegate_async_persists_resumable_payload_for_restart_recovery(monkeypatch):
    from app.agents.orchestrator import delegate_async

    created = {}

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="async result")

    async def fake_create_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*args, **kwargs):
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_id = uuid4()
    parent_agent_id = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do research"}],
        owner_id=owner_id,
        session_id="sess-restart",
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session",
        max_tool_rounds=11,
    )

    assert handle.task_id
    assert created["status"] == "pending"
    assert created["child_agent_id"] == target.id
    assert created["parent_agent_id"] == parent_agent_id
    metadata = created["metadata_json"]
    assert metadata["resumable_delegation"] is True
    assert metadata["resume_after_restart"] is True
    assert metadata["owner_id"] == str(owner_id)
    assert metadata["target_agent_id"] == str(target.id)
    assert metadata["conversation_messages"] == [{"role": "user", "content": "do research"}]
    assert metadata["max_tool_rounds"] == 11


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_rehydrates_tasks(monkeypatch):
    from app.agents.orchestrator import (
        _async_tasks,
        check_async_delegation,
        resume_persisted_async_delegations,
    )

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        assert "pending" in statuses
        assert "running" in statuses
        return [
            {
                "task_id": task_id,
                "trace_id": "trace-resume",
                "parent_agent_id": str(parent_agent_id),
                "child_agent_id": str(target.id),
                "child_agent_name": target.name,
                "parent_session_id": "parent-session",
                "child_session_id": "child-session",
                "depth": 1,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "owner_id": str(owner_id),
                    "target_agent_id": str(target.id),
                    "conversation_messages": [{"role": "user", "content": "resume me"}],
                    "system_prompt_suffix": "",
                    "max_tool_rounds": 9,
                    "timeout_seconds": 120.0,
                },
            }
        ]

    async def fake_resolve_target_runtime(child_agent_id):
        assert child_agent_id == target.id
        return target, model

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="resumed async result")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == [task_id]
        assert task_id in _async_tasks

        await asyncio.sleep(0.05)
        status = await check_async_delegation(task_id, parent_agent_id=parent_agent_id)

        assert status["status"] == "completed"
        assert status["result"] == "resumed async result"
        assert any(task_id_arg == task_id and payload.get("status") == "running" for task_id_arg, payload in updates)
        assert any(task_id_arg == task_id and payload.get("status") == "completed" for task_id_arg, payload in updates)
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_delegate_async_waits_for_activity_log_persistence(monkeypatch):
    from app.agents.orchestrator import delegate_async

    log_started = asyncio.Event()
    allow_log_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="async result")

    async def fake_log_activity(*args, **kwargs):
        log_started.set()
        await allow_log_finish.wait()

    async def fake_create_task(**kwargs):
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*args, **kwargs):
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    pending = asyncio.create_task(
        delegate_async(
            target=target,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "do research"}],
            owner_id=uuid4(),
            session_id="sess-wait-log",
            parent_agent_id=uuid4(),
        )
    )

    await asyncio.wait_for(log_started.wait(), timeout=0.2)
    assert pending.done() is False

    allow_log_finish.set()
    handle = await asyncio.wait_for(pending, timeout=0.2)
    assert handle.task_id


@pytest.mark.asyncio
async def test_cancel_async_delegation_marks_task_killed(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation, delegate_async

    never_finish = asyncio.Event()
    persisted: list[tuple[str, dict]] = []

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    async def fake_create_task(**kwargs):
        persisted.append(("create", kwargs))
        return kwargs["task_id"]

    async def fake_update_task(task_id, **kwargs):
        persisted.append(("update", {"task_id": task_id, **kwargs}))
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)

    target = SimpleNamespace(id=uuid4(), name="KillableWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_id = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do research"}],
        owner_id=uuid4(),
        session_id="sess-kill",
        parent_agent_id=owner_id,
    )

    status = await cancel_async_delegation(handle.task_id, parent_agent_id=owner_id)

    assert status["status"] == "killed"
    assert status["task_id"] == handle.task_id
    assert any(kind == "update" and payload["status"] == "killed" for kind, payload in persisted)


@pytest.mark.asyncio
async def test_cancel_async_delegation_rejects_other_parent(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation, delegate_async

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="KillableWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do research"}],
        owner_id=uuid4(),
        session_id="sess-kill-owner",
        parent_agent_id=owner_a,
    )

    status = await cancel_async_delegation(handle.task_id, parent_agent_id=uuid4())
    assert status["status"] == "forbidden"

    never_finish.set()


@pytest.mark.asyncio
async def test_check_async_delegation_not_found():
    from app.agents.orchestrator import check_async_delegation

    status = await check_async_delegation("nonexistent-id")
    assert status["status"] == "not_found"


@pytest.mark.asyncio
async def test_delegate_async_handles_failure(monkeypatch):
    from app.agents.orchestrator import delegate_async, check_async_delegation

    async def fake_invoke(invocation):
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="Crasher", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "crash"}],
        owner_id=uuid4(),
        session_id="sess-2",
    )

    await asyncio.sleep(0.05)

    status = await check_async_delegation(handle.task_id)
    assert status["status"] == "failed"
    assert "failed" in status["result"]


@pytest.mark.asyncio
async def test_list_async_delegations(monkeypatch):
    from app.agents.orchestrator import delegate_async, list_async_delegations

    never_finish = asyncio.Event()

    async def fake_invoke(invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="Lister", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "list test"}],
        owner_id=uuid4(),
        session_id="sess-3",
    )

    tasks = list_async_delegations()
    assert any(t["task_id"] == handle.task_id for t in tasks)
    assert any(t["status"] == "running" for t in tasks)

    # Cleanup
    never_finish.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_list_async_delegations_filters_by_parent_agent(monkeypatch):
    from app.agents.orchestrator import check_async_delegation, delegate_async, list_async_delegations

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="ScopedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()
    owner_b = uuid4()

    handle_a = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-a"}],
        owner_id=uuid4(),
        session_id="sess-scope-a",
        parent_agent_id=owner_a,
    )
    handle_b = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "task-b"}],
        owner_id=uuid4(),
        session_id="sess-scope-b",
        parent_agent_id=owner_b,
    )

    tasks = list_async_delegations(parent_agent_id=owner_a)
    task_ids = {task["task_id"] for task in tasks}
    assert handle_a.task_id in task_ids
    assert handle_b.task_id not in task_ids

    never_finish.set()
    await asyncio.sleep(0.05)
    await check_async_delegation(handle_a.task_id)
    await check_async_delegation(handle_b.task_id)


@pytest.mark.asyncio
async def test_delegate_async_persists_runtime_task_lifecycle(monkeypatch):
    from app.agents.orchestrator import check_async_delegation, delegate_async

    persisted: list[tuple[str, dict]] = []

    async def fake_create_task(**kwargs):
        persisted.append(("create", kwargs))
        return kwargs["task_id"]

    async def fake_update_task(task_id, **kwargs):
        persisted.append(("update", {"task_id": task_id, **kwargs}))

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="async result")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)

    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do research"}],
        owner_id=uuid4(),
        session_id="sess-runtime",
        parent_agent_id=uuid4(),
    )

    await asyncio.sleep(0.05)
    status = await check_async_delegation(handle.task_id)

    assert status["status"] == "completed"
    assert persisted[0][0] == "create"
    assert persisted[0][1]["task_id"] == handle.task_id
    assert any(kind == "update" and payload["status"] == "completed" for kind, payload in persisted)
