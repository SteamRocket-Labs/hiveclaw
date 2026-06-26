from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _stub_activity_logger(monkeypatch):
    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    async def fake_delegation_plan_gate_allows(_request):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", fake_delegation_plan_gate_allows)


def test_delegation_runtime_uses_replayable_transcript_writer() -> None:
    import ast

    source_path = Path(__file__).resolve().parents[2] / "app" / "agents" / "orchestrator.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    delegate_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_delegate_after_cycle_check"
    )

    append_session_event_calls = [
        node
        for node in ast.walk(delegate_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_session_event"
    ]

    assert append_session_event_calls


@pytest.mark.asyncio
async def test_delegate_to_agent_builds_runtime_request(monkeypatch):
    from app.agents.orchestrator import delegate_to_agent
    from app.core.execution_context import ExecutionIdentity, clear_execution_identity, set_execution_identity

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
    owner_id = uuid4()
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="delegated reply")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    user_id = uuid4()
    set_execution_identity(ExecutionIdentity(identity_type="delegated_user", identity_id=user_id, label="User via web"))
    try:
        reply = await delegate_to_agent(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=owner_id,
            session_id="session-1",
            tool_executor=tool_executor,
            system_prompt_suffix="A2A_SUFFIX",
            max_tool_rounds=7,
        )
    finally:
        clear_execution_identity()

    request = captured["request"]
    assert reply == "delegated reply"
    assert request.agent_id == target.id
    assert request.agent_name == "Target Agent"
    assert request.role_description == "Helpful"
    assert request.model is target_model
    assert len(request.messages) == 1
    assert request.messages[0]["role"] == "user"
    # F-1 dispatch symmetry: instruction passes verbatim, no envelope wrapper
    assert request.messages[0]["content"] == "hello"
    assert "Delegated Task Brief" not in request.messages[0]["content"]
    assert request.memory_messages == []
    assert request.memory_session_id == "session-1"
    assert request.tool_executor is tool_executor
    assert request.session_context is not None
    assert request.session_context.source == "agent"
    assert request.session_context.channel == "agent"
    assert request.session_context.session_id == "session-1"
    assert request.core_tools_only is True
    # D-14: delegation applies the single-source base deny-list (shared with
    # subagents) plus save_skill plus memory read+write denials. Assert the
    # security-relevant invariants against the deny-list source of truth rather
    # than a brittle hand-maintained ordered snapshot.
    from app.agents.orchestrator import (
        _DELEGATION_BASE_EXCLUDED_TOOLS,
        _DELEGATION_MEMORY_WRITE_TOOLS,
    )

    excluded = set(request.excluded_tool_names)
    assert set(_DELEGATION_BASE_EXCLUDED_TOOLS).issubset(excluded)
    assert set(_DELEGATION_MEMORY_WRITE_TOOLS).issubset(excluded)
    assert "save_skill" in excluded
    assert {"search_memory", "load_memory"}.issubset(excluded)
    # recursion guard: source/control tools must be denied on the delegate surface
    assert {"delegate_to_agent", "spawn_subagent", "check_subagent"}.issubset(excluded)
    assert request.max_tool_rounds == 7
    assert "A2A_SUFFIX" in request.system_prompt_suffix
    # F-1: slim worker prompt — isolation_contract + tool_policy remain; forced
    # return template (Completed/Evidence/Blockers) was removed as an L1 violation.
    assert "<isolation_contract>" in request.system_prompt_suffix
    assert "<tool_policy>" in request.system_prompt_suffix
    assert "Completed:" not in request.system_prompt_suffix
    assert "Evidence:" not in request.system_prompt_suffix
    assert "Blockers:" not in request.system_prompt_suffix
    assert "Do NOT read or write long-term memory" in request.system_prompt_suffix
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_safe"
    assert request.session_context.metadata["delegation_memory_policy"] == "isolated_no_long_term_memory"
    assert request.delegation_token is not None
    assert request.delegation_token.parent_agent_id == owner_id
    assert request.delegation_token.child_agent_id == target.id
    assert request.delegation_token.inherit_parent_capabilities is False
    assert "workspace.file.read" in request.delegation_token.granted_capabilities
    assert "workspace.file.write" in request.delegation_token.granted_capabilities
    assert "agent.memory.write" not in request.delegation_token.granted_capabilities
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "delegated_user"
    assert request.execution_identity.identity_id == user_id
    assert request.execution_identity.label == "User via web"
    assert request.session_context.metadata["delegation_token_id"] == request.delegation_token.delegation_id
    assert "agent.memory.write" not in request.session_context.metadata["delegation_token_capabilities"]


@pytest.mark.asyncio
async def test_delegate_async_serializes_duplicate_work_with_coordination_lease(monkeypatch):
    from app.agents.coordination import coordination_runtime
    from app.agents.orchestrator import delegate_async

    coordination_runtime.reset()
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    owner_id = uuid4()
    parent_id = uuid4()

    async def fake_create_runtime_task_record(**_kwargs):
        return None

    async def fake_update_runtime_task_record(*_args, **_kwargs):
        return None

    async def fake_persist_delegation_event(**_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", lambda **_kwargs: None)

    kwargs = {
        "target": target,
        "target_model": target_model,
        "conversation_messages": [{"role": "user", "content": "Prepare the market map"}],
        "owner_id": owner_id,
        "session_id": "session-lease",
        "parent_agent_id": parent_id,
    }
    first = await delegate_async(**kwargs)
    second = await delegate_async(**kwargs)

    assert first.status == "running"
    assert first.coordination_lease_id
    assert second.status == "blocked_by_lease"
    assert second.blocked_by_lease_id == first.coordination_lease_id
    assert coordination_runtime.read_signals(str(target.id), thread_id=first.signal_thread_id)


@pytest.mark.asyncio
async def test_delegate_async_captures_execution_identity_before_background_spawn(monkeypatch):
    from app.agents.orchestrator import delegate_async
    from app.core.execution_context import ExecutionIdentity, clear_execution_identity, set_execution_identity

    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}

    async def fake_create_runtime_task_record(**_kwargs):
        return None

    async def fake_update_runtime_task_record(*_args, **_kwargs):
        return None

    async def fake_persist_delegation_event(**_kwargs):
        return None

    def fake_spawn_async_delegation_task(*, task_id, request, trace_id):
        captured["task_id"] = task_id
        captured["request"] = request
        captured["trace_id"] = trace_id

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", fake_spawn_async_delegation_task)

    user_id = uuid4()
    set_execution_identity(
        ExecutionIdentity(identity_type="delegated_user", identity_id=user_id, label="User via Feishu")
    )
    try:
        handle = await delegate_async(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "Prepare the market map"}],
            owner_id=uuid4(),
            session_id="session-identity",
            parent_agent_id=uuid4(),
        )
    finally:
        clear_execution_identity()

    assert handle.status == "running"
    assert captured["request"].execution_identity is not None
    assert captured["request"].execution_identity.identity_type == "delegated_user"
    assert captured["request"].execution_identity.identity_id == user_id
    assert captured["request"].execution_identity.label == "User via Feishu"


@pytest.mark.asyncio
async def test_delegate_async_stops_before_task_creation_when_plan_gate_blocks(monkeypatch):
    from app.agents.orchestrator import delegate_async

    async def fake_delegation_plan_gate_allows(_request):
        return False, "no_confirmed_plan"

    async def unexpected_create_runtime_task_record(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("runtime task must not be created when Plan Mode blocks delegation")

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", fake_delegation_plan_gate_allows)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", unexpected_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", lambda **_kwargs: None)

    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")

    handle = await delegate_async(
        target=target,
        target_model=target_model,
        conversation_messages=[{"role": "user", "content": "Prepare the market map"}],
        owner_id=uuid4(),
        session_id="session-plan-block",
        parent_agent_id=uuid4(),
    )

    assert handle.task_id == "plan_required"
    assert handle.status == "plan_required:no_confirmed_plan"


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
async def test_delegate_fails_closed_when_delegation_token_cannot_be_issued(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _delegate

    target = SimpleNamespace(id="not-a-uuid", name="Broken Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")

    async def _unexpected_invoke(_request):
        raise AssertionError("invoke_agent must not run without a delegation token")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _unexpected_invoke)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=uuid4(),
            session_id="bad-token-child",
        )
    )

    assert result.failed is True
    assert "delegation token" in result.content.lower()


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
    assert "load_memory" not in request.excluded_tool_names
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
        "load_memory",
        "get_current_time",
    )
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_review_readonly"
    assert request.session_context.metadata["delegation_memory_policy"] == "read_only_long_term_memory"
    assert "Do NOT edit files" in request.system_prompt_suffix
    assert request.delegation_token is not None
    assert request.delegation_token.inherit_parent_capabilities is False
    assert request.delegation_token.granted_capabilities == frozenset(
        {
            "workspace.file.read",
            "agent.skill.read",
            "agent.tool.discover",
            "agent.memory.read",
            "system.time.read",
        }
    )


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
        "load_memory",
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
async def test_agent_message_profile_uses_target_agent_tool_surface_without_recursion(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Feishu Knowledge", role_description="Knowledge assistant")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="looked up")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "请查飞书知识库里的灵巧手报告"}],
            owner_id=uuid4(),
            session_id="agent-message-child",
            parent_agent_id=uuid4(),
            parent_session_id="parent-session",
            interaction_type="agent_message",
            policy=OrchestrationPolicy(timeout_seconds=120, tool_profile="agent_message"),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert request.core_tools_only is False
    assert request.allowed_tool_names == ()
    assert "send_message_to_agent" in request.excluded_tool_names
    assert "delegate_to_agent" in request.excluded_tool_names
    assert "save_memory" in request.excluded_tool_names
    assert "save_skill" in request.excluded_tool_names
    assert request.delegation_token is None
    assert request.session_context.metadata["agent_message_tool_policy"] == "peer_agent_tool_surface"
    assert request.session_context.metadata["agent_message_memory_policy"] == "peer_read_only_memory"
    assert "peer agent request" in request.system_prompt_suffix


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
async def test_delegate_async_default_worker_safe_persists_mutating_replay_contract(monkeypatch):
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
    assert metadata["side_effect_risk"] == "mutating"
    assert metadata["tool_profile"] == "worker_safe"
    assert metadata["owner_id"] == str(owner_id)
    assert metadata["target_agent_id"] == str(target.id)
    assert metadata["conversation_messages"] == [{"role": "user", "content": "do research"}]
    assert metadata["restart_replay_contract"]["schema"] == "runtime_restart_replay_contract.v1"
    assert metadata["restart_replay_contract"]["task_type"] == "delegation"
    assert metadata["restart_replay_contract"]["idempotency_key"] == f"delegation:{handle.task_id}:restart"
    assert metadata["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert metadata["restart_replay_journal"][0]["idempotency_key"] == (
        f"delegation:{handle.task_id}:restart:spawn_intent_recorded"
    )
    assert "restart_resume_blocker" not in metadata


@pytest.mark.asyncio
async def test_delegate_async_persists_readonly_resumable_payload_for_restart_recovery(monkeypatch):
    from app.agents.orchestrator import OrchestrationPolicy, delegate_async

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
        policy=OrchestrationPolicy(tool_profile="review_readonly"),
    )

    assert handle.task_id
    assert created["status"] == "pending"
    assert created["child_agent_id"] == target.id
    assert created["parent_agent_id"] == parent_agent_id
    metadata = created["metadata_json"]
    assert metadata["resumable_delegation"] is True
    assert metadata["resume_after_restart"] is True
    assert metadata["tool_profile"] == "review_readonly"
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
    delegated_user_id = uuid4()
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
                    "tool_profile": "review_readonly",
                    "execution_identity": {
                        "identity_type": "delegated_user",
                        "identity_id": str(delegated_user_id),
                        "label": "User via recovered session",
                    },
                },
            }
        ]

    async def fake_resolve_target_runtime(child_agent_id):
        assert child_agent_id == target.id
        return target, model

    async def fake_invoke(invocation):
        assert invocation.execution_identity is not None
        assert invocation.execution_identity.identity_type == "delegated_user"
        assert invocation.execution_identity.identity_id == delegated_user_id
        assert invocation.execution_identity.label == "User via recovered session"
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
async def test_resume_persisted_async_delegations_refuses_mutating_profile_without_replay_contract(monkeypatch):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": task_id,
                "trace_id": "trace-resume",
                "parent_agent_id": str(uuid4()),
                "child_agent_id": str(target.id),
                "child_agent_name": target.name,
                "parent_session_id": "parent-session",
                "child_session_id": "child-session",
                "depth": 1,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "tool_profile": "worker_safe",
                    "owner_id": str(uuid4()),
                    "target_agent_id": str(target.id),
                    "conversation_messages": [{"role": "user", "content": "resume me"}],
                },
            }
        ]

    async def fake_resolve_target_runtime(_child_agent_id):  # pragma: no cover - must not run
        raise AssertionError("non replay-safe delegation must not resolve/replay the child runtime")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == []
        assert task_id not in _async_tasks
        assert updates[-1][0] == task_id
        assert updates[-1][1]["status"] == "needs_reconciliation"
        assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
        assert updates[-1][1]["metadata_json"]["side_effect_risk"] == "mutating"
        assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_tool_profile"
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_reconciles_worker_safe_even_with_spawn_journal(monkeypatch):
    from app.agents.orchestrator import (
        _async_tasks,
        resume_persisted_async_delegations,
    )

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
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
                    "tool_profile": "worker_safe",
                    "owner_id": str(owner_id),
                    "target_agent_id": str(target.id),
                    "conversation_messages": [{"role": "user", "content": "resume me"}],
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"delegation:{task_id}:restart",
                        "task_type": "delegation",
                    },
                    "restart_replay_journal": [
                        {
                            "schema": "runtime_restart_replay_journal.v1",
                            "idempotency_key": f"delegation:{task_id}:restart:spawn_intent_recorded",
                            "task_type": "delegation",
                            "task_id": task_id,
                            "phase": "spawn_intent_recorded",
                            "side_effect_risk": "mutating",
                        }
                    ],
                },
            }
        ]

    async def fake_resolve_target_runtime(_child_agent_id):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation must not resolve/replay the child runtime")

    async def fake_invoke(_invocation):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation must not invoke from spawn intent")

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
        assert resumed == []
        assert task_id not in _async_tasks
        assert updates[-1][0] == task_id
        assert updates[-1][1]["status"] == "needs_reconciliation"
        assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
        assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_tool_profile"
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_refuses_mutating_contract_without_replay_journal(monkeypatch):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": task_id,
                "trace_id": "trace-resume",
                "parent_agent_id": str(uuid4()),
                "child_agent_id": str(target.id),
                "child_agent_name": target.name,
                "parent_session_id": "parent-session",
                "child_session_id": "child-session",
                "depth": 1,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "tool_profile": "worker_safe",
                    "side_effect_risk": "mutating",
                    "owner_id": str(uuid4()),
                    "target_agent_id": str(target.id),
                    "conversation_messages": [{"role": "user", "content": "resume me"}],
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"delegation:{task_id}:restart",
                        "task_type": "delegation",
                    },
                },
            }
        ]

    async def fake_resolve_target_runtime(_child_agent_id):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation without replay journal must not be replayed")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == []
        assert task_id not in _async_tasks
        assert updates[-1][0] == task_id
        assert updates[-1][1]["status"] == "needs_reconciliation"
        assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_tool_profile"
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

    status = await cancel_async_delegation(handle.task_id, parent_agent_id=owner_id, force=True)

    assert status["status"] == "killed"
    assert status["task_id"] == handle.task_id
    assert any(kind == "update" and payload["status"] == "killed" for kind, payload in persisted)


@pytest.mark.asyncio
async def test_cancel_async_delegation_defers_fresh_running_task_without_force(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation, check_async_delegation, delegate_async

    never_finish = asyncio.Event()
    parent_agent_id = uuid4()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    async def fake_create_task(**kwargs):
        return kwargs["task_id"]

    async def fake_update_task(*args, **kwargs):
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)

    target = SimpleNamespace(id=uuid4(), name="FreshWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "scan a large knowledge base"}],
        owner_id=uuid4(),
        session_id="sess-fresh-cancel",
        parent_agent_id=parent_agent_id,
    )
    started_at = datetime.now(timezone.utc) - timedelta(seconds=75)

    async def fake_get_runtime_task_record(task_id):
        assert task_id == handle.task_id
        return {
            "task_id": task_id,
            "status": "running",
            "parent_agent_id": str(parent_agent_id),
            "created_at": started_at.isoformat(),
            "started_at": started_at.isoformat(),
        }

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)

    status = await cancel_async_delegation(
        handle.task_id,
        parent_agent_id=parent_agent_id,
        min_runtime_seconds=180.0,
    )

    assert status["status"] == "running"
    assert status["cancellation_deferred"] is True
    assert "still running" in status["result"]

    running = await check_async_delegation(handle.task_id, parent_agent_id=parent_agent_id)
    assert running["status"] == "running"

    never_finish.set()
    await check_async_delegation(handle.task_id, parent_agent_id=parent_agent_id)


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


# ── P0-3a/b: cycle detection on shared trace_id ────────────────────────


def _delegation_request(*, target, owner_id, depth=1, trace_id=None, max_depth=5):
    """Helper that builds a minimal AgentDelegationRequest for cycle tests."""
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy

    return AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "hello"}],
        owner_id=owner_id,
        session_id="cycle-session",
        depth=depth,
        trace_id=trace_id,
        policy=OrchestrationPolicy(max_depth=max_depth),
    )


@pytest.mark.asyncio
async def test_delegate_blocks_revisiting_same_agent_on_same_trace(monkeypatch):
    """A→B→A: when target is already in the visited set for this trace, refuse."""
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace

    _visited_agents_by_trace.clear()
    target_a_id = uuid4()
    target_a = SimpleNamespace(id=target_a_id, name="AgentA", role_description="x")
    owner_id = uuid4()

    # Pre-seed visited as if A is already mid-flight on this trace.
    trace = "trace-A2A"
    _visited_agents_by_trace[trace] = {str(target_a_id)}

    async def _unexpected(_request):
        raise AssertionError("invoke_agent must NOT run when cycle detected")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _unexpected)

    result = await _delegate(_delegation_request(target=target_a, owner_id=owner_id, trace_id=trace))

    assert result.failed is True
    assert "cycle" in result.content.lower()
    assert "AgentA" in result.content
    assert result.trace_id == trace
    # Failed cycle entries do NOT pollute the visited set with a new add.
    assert _visited_agents_by_trace.get(trace) == {str(target_a_id)}

    _visited_agents_by_trace.clear()


@pytest.mark.asyncio
async def test_delegate_allows_distinct_agents_on_same_trace(monkeypatch):
    """A→B→C is fine — only revisits trip the detector."""
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace

    _visited_agents_by_trace.clear()
    trace = "trace-linear"
    a_id, b_id, c_id = uuid4(), uuid4(), uuid4()
    _visited_agents_by_trace[trace] = {str(a_id), str(b_id)}

    target_c = SimpleNamespace(id=c_id, name="AgentC", role_description="x")

    captured = {}

    async def _stub_invoke(req):
        captured["agent_id"] = req.agent_id
        return SimpleNamespace(content="ok", parts=[], tokens_used=0, final_tools=None)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _stub_invoke)

    result = await _delegate(_delegation_request(target=target_c, owner_id=uuid4(), trace_id=trace))

    assert result.failed is False
    assert "cycle" not in result.content.lower()
    assert captured["agent_id"] == c_id
    # finally-clause must drop C from the set, leaving the seeded {A,B}.
    assert _visited_agents_by_trace.get(trace) == {str(a_id), str(b_id)}

    _visited_agents_by_trace.clear()


@pytest.mark.asyncio
async def test_delegate_cleans_up_visited_set_after_completion(monkeypatch):
    """Successful single-hop delegation leaves no residue in the trace map."""
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace

    _visited_agents_by_trace.clear()
    target = SimpleNamespace(id=uuid4(), name="Solo", role_description="x")

    async def _stub_invoke(_req):
        return SimpleNamespace(content="done", parts=[], tokens_used=0, final_tools=None)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _stub_invoke)

    result = await _delegate(_delegation_request(target=target, owner_id=uuid4()))

    assert result.failed is False
    # When the trace's set drops to empty, the dict entry should be removed
    # entirely (no per-trace memory leak).
    assert result.trace_id not in _visited_agents_by_trace


@pytest.mark.asyncio
async def test_delegate_cleans_up_visited_set_after_invoke_exception(monkeypatch):
    """If the underlying invoke_agent raises, finally still clears the agent
    from the visited set so a retry on the same trace isn't false-positively
    flagged as a cycle. _delegate wraps invoke errors into a failed result
    rather than propagating, so we check both: result.failed AND visited cleanup."""
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace

    _visited_agents_by_trace.clear()
    target = SimpleNamespace(id=uuid4(), name="Crashy", role_description="x")

    async def _explode(_req):
        raise RuntimeError("downstream LLM blew up")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _explode)

    result = await _delegate(_delegation_request(target=target, owner_id=uuid4(), trace_id="t-recover"))

    # Error gets converted to a failed result, not propagated.
    assert result.failed is True
    # Visited set fully cleaned regardless of how invoke_agent ended.
    assert "t-recover" not in _visited_agents_by_trace


@pytest.mark.asyncio
async def test_delegate_concurrent_traces_do_not_interfere(monkeypatch):
    """Two concurrent traces hitting the same agent must each see their own
    visited set — no cross-trace cycle false positive."""
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace

    _visited_agents_by_trace.clear()
    shared_target = SimpleNamespace(id=uuid4(), name="Shared", role_description="x")

    invocations: list[str] = []

    async def _track(req):
        invocations.append(req.session_context.session_id if req.session_context else "?")
        await asyncio.sleep(0.01)
        return SimpleNamespace(content="ok", parts=[], tokens_used=0, final_tools=None)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _track)

    async def run_trace(trace_id, session_id):
        from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy

        return await _delegate(
            AgentDelegationRequest(
                target=shared_target,
                target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
                conversation_messages=[{"role": "user", "content": "x"}],
                owner_id=uuid4(),
                session_id=session_id,
                trace_id=trace_id,
                policy=OrchestrationPolicy(max_depth=5),
            )
        )

    r1, r2 = await asyncio.gather(
        run_trace("trace-1", "s1"),
        run_trace("trace-2", "s2"),
    )
    assert r1.failed is False and r2.failed is False
    assert _visited_agents_by_trace == {}  # both finally branches cleaned up


# ── P1-W3-2 — AgentDelegationResult JSON serialization ──────────


class TestDelegationResultSerialization:
    """to_dict / to_json carry enough structure that a parent agent can
    branch on the outcome without regex-matching the content prefix."""

    def test_ok_status_when_no_failure_flags(self) -> None:
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(
            content="answer body",
            child_session_id="cs",
            trace_id="t",
            depth=1,
        )
        payload = result.to_dict()

        assert payload["status"] == "ok"
        assert payload["content"] == "answer body"
        assert payload["child_session_id"] == "cs"
        assert payload["trace_id"] == "t"
        assert payload["depth"] == 1
        assert payload["failed"] is False
        assert payload["timed_out"] is False
        assert payload["depth_limited"] is False

    def test_status_is_depth_limited_when_both_flags_set(self) -> None:
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(
            content="...",
            child_session_id="cs",
            trace_id="t",
            depth=3,
            failed=True,
            depth_limited=True,
        )
        assert result.to_dict()["status"] == "depth_limited"

    def test_status_is_timed_out_when_failure_came_from_timeout(self) -> None:
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(
            content="...",
            child_session_id="cs",
            trace_id="t",
            depth=1,
            failed=True,
            timed_out=True,
        )
        assert result.to_dict()["status"] == "timed_out"

    def test_status_is_failed_for_generic_failure(self) -> None:
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(
            content="...",
            child_session_id="cs",
            trace_id="t",
            depth=1,
            failed=True,
        )
        assert result.to_dict()["status"] == "failed"

    def test_to_json_round_trips_through_json_loads(self) -> None:
        import json
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(
            content="hello 世界",
            child_session_id="cs",
            trace_id="t",
            depth=2,
            timed_out=True,
            failed=True,
        )
        decoded = json.loads(result.to_json())

        assert decoded["status"] == "timed_out"
        assert decoded["content"] == "hello 世界"  # ensure_ascii=False preserves CJK
        assert decoded["depth"] == 2

    def test_to_json_is_valid_json_for_clean_result(self) -> None:
        import json
        from app.agents.orchestrator import AgentDelegationResult

        result = AgentDelegationResult(content="ok", child_session_id="cs", trace_id="t", depth=1)
        # Will raise on malformed JSON.
        json.loads(result.to_json())


def _capture_parent_session_events(monkeypatch):
    """Stub tenant_scoped_session + append_session_event, returning captured calls."""
    import contextlib

    captured: list[dict] = []

    class _FakeDB:
        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def fake_tenant_scoped_session(_tenant=None):
        yield _FakeDB()

    async def fake_append_session_event(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    return captured


@pytest.mark.asyncio
async def test_delegation_completion_projects_child_session_event_to_parent(monkeypatch):
    """Async delegation completion must append a child_session event to the parent
    session timeline (parity with subagent_run_service), not only update RuntimeTask.

    Revert-sensitive: removing the parent projection in
    `_project_delegation_completion_to_parent` drops the captured event and fails this.
    """
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)

    parent_session_id = uuid4().hex
    child_session_id = uuid4().hex
    parent_agent_id = uuid4()
    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Researcher", tenant_id=tenant_id)

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "go"}],
        owner_id=uuid4(),
        session_id=child_session_id,
        parent_agent_id=parent_agent_id,
        parent_session_id=parent_session_id,
        trace_id="trace-xyz",
        depth=1,
        tenant_id=tenant_id,
        runtime_task_id="task-1",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-1",
        status="completed",
        summary="Found the answer.",
    )

    import uuid as _uuid

    assert len(captured) == 1, "expected exactly one parent-session projection event"
    event = captured[0]
    assert _uuid.UUID(str(event["session_id"])) == _uuid.UUID(parent_session_id)
    assert event["event_type"] == "child_session"
    assert _uuid.UUID(event["metadata"]["child_session_id"]) == _uuid.UUID(child_session_id)
    assert event["metadata"]["status"] == "completed"
    assert event["metadata"]["reason"] == "delegation_completed"
    assert event["content"] == "Found the answer."
    assert event["listed_surface"] == "chat"


@pytest.mark.asyncio
async def test_delegation_failure_projects_failed_child_session_event_to_parent(monkeypatch):
    """Failed delegations must also surface on the parent timeline with a failed reason."""
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)

    parent_session_id = uuid4().hex
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="Worker", tenant_id=uuid4()),
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "go"}],
        owner_id=uuid4(),
        session_id=uuid4().hex,
        parent_agent_id=uuid4(),
        parent_session_id=parent_session_id,
        trace_id="trace-fail",
        depth=2,
        tenant_id=uuid4(),
        runtime_task_id="task-2",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-2",
        status="failed",
        summary="It broke.",
    )

    assert len(captured) == 1
    assert captured[0]["metadata"]["status"] == "failed"
    assert captured[0]["metadata"]["reason"] == "delegation_failed"


@pytest.mark.asyncio
async def test_delegation_completion_skips_projection_for_headless_parent(monkeypatch):
    """Headless delegation (no parent_session_id) must skip projection without error."""
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)

    request = AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="Worker", tenant_id=uuid4()),
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "go"}],
        owner_id=uuid4(),
        session_id=uuid4().hex,
        parent_agent_id=uuid4(),
        parent_session_id=None,
        trace_id="trace-headless",
        depth=1,
        tenant_id=uuid4(),
        runtime_task_id="task-3",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-3",
        status="completed",
        summary="done",
    )

    assert captured == [], "headless delegation must not project to a parent session"


@pytest.mark.asyncio
async def test_spawn_async_delegation_task_wires_parent_projection_on_completion(monkeypatch):
    """The background completion path must call the parent projection on terminal state.

    Revert-sensitive: removing the `_project_delegation_completion_to_parent` call from
    `_spawn_async_delegation_task._run` leaves `projected` empty and fails this.
    """
    from app.agents.orchestrator import (
        AgentDelegationResult,
        _async_tasks,
        _spawn_async_delegation_task,
    )

    projected: list[dict] = []

    async def fake_delegate(request):
        return AgentDelegationResult(
            content="child output",
            child_session_id=request.session_id,
            trace_id=request.trace_id,
            depth=request.depth,
        )

    async def fake_update_runtime_task_record(*_args, **_kwargs):
        return True

    async def fake_project(**kwargs):
        projected.append(kwargs)

    async def fake_plan_gate(_request):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegate", fake_delegate)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._project_delegation_completion_to_parent", fake_project)
    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", fake_plan_gate)

    from app.agents.orchestrator import AgentDelegationRequest

    task_id = "task-wire-1"
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="Worker", tenant_id=uuid4()),
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "go"}],
        owner_id=uuid4(),
        session_id=uuid4().hex,
        parent_agent_id=uuid4(),
        parent_session_id=uuid4().hex,
        trace_id="trace-wire",
        depth=1,
        tenant_id=uuid4(),
        runtime_task_id=task_id,
    )

    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-wire")
    state = _async_tasks.get(task_id)
    assert state is not None
    await state.task

    assert len(projected) == 1
    assert projected[0]["task_id"] == task_id
    assert projected[0]["status"] == "completed"
    assert projected[0]["summary"] == "child output"
