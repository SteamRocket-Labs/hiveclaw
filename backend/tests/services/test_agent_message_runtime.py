from __future__ import annotations

import asyncio
import ast
import json
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


def test_a2a_tool_call_persistence_uses_replayable_transcript_writer() -> None:
    source_path = Path(__file__).resolve().parents[2] / "app" / "services" / "agent_tool_domains" / "messaging.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    persist_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_persist_agent_tool_call"
    )

    direct_chat_message_calls = [
        node
        for node in ast.walk(persist_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChatMessage"
    ]
    append_session_event_calls = [
        node
        for node in ast.walk(persist_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_session_event"
    ]

    assert direct_chat_message_calls == []
    assert append_session_event_calls


@pytest.mark.asyncio
async def test_invoke_agent_message_runtime_delegates_to_runtime(monkeypatch):
    from app.services.agent_tools import _invoke_agent_message_runtime

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
        permission_profile={
            "mode": "bypassPermissions",
            "allowed_tools": ["web_search", "feishu_doc_read"],
        },
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
    assert captured["kwargs"]["policy"].timeout_seconds == 300.0
    assert captured["kwargs"]["policy"].tool_profile == "agent_message"
    assert captured["kwargs"]["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["web_search", "feishu_doc_read"],
    }
    # PR-19 rewrote A2A_SYSTEM_PROMPT_SUFFIX with XML structure; the A2A
    # identity signal is now carried by "agent-to-agent\ncommunication, 'A2A'"
    # and "peer agent" inside <role>.
    suffix = captured["kwargs"]["system_prompt_suffix"]
    assert "agent-to-agent" in suffix.lower()
    assert "peer agent" in suffix.lower()


@pytest.mark.asyncio
async def test_build_agent_message_tool_executor_persists_tool_calls(monkeypatch):
    from app.services.agent_tools import _build_agent_message_tool_executor

    target_id = uuid4()
    owner_id = uuid4()
    participant_id = uuid4()
    calls = {}

    async def fake_execute_tool(tool_name, args, agent_id, user_id, *, emit_runtime_hooks=True):
        calls["execute"] = (tool_name, args, agent_id, user_id, emit_runtime_hooks)
        return "TOOL_RESULT"

    async def fake_persist(**kwargs):
        calls["persist"] = kwargs

    monkeypatch.setattr("app.services.agent_tools.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging._persist_agent_tool_call", fake_persist)

    executor = _build_agent_message_tool_executor(
        target_agent_id=target_id,
        owner_id=owner_id,
        session_agent_id=uuid4(),
        session_id="session-2",
        participant_id=participant_id,
    )

    result = await executor("read_file", {"path": "skills/test/SKILL.md"}, emit_runtime_hooks=False)

    assert result == "TOOL_RESULT"
    assert calls["execute"] == (
        "read_file",
        {"path": "skills/test/SKILL.md"},
        target_id,
        owner_id,
        False,
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
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
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
            "confirmed_plan_id": "plan-1",
            "confirmed_plan_version": 2,
            "confirmed_plan_hash": "sha256:plan",
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-1"
    assert payload["session_id"] == captured["kwargs"]["session_id"]
    assert payload["child_session_id"] == captured["kwargs"]["session_id"]
    assert "send_agent_session_message" in payload["next_action"]
    assert captured["kwargs"]["policy"].tool_profile == "memory_readonly"
    assert captured["kwargs"]["policy"].timeout_seconds == 600.0
    assert captured["kwargs"]["confirmed_plan_id"] == "plan-1"
    assert captured["kwargs"]["confirmed_plan_version"] == 2
    assert captured["kwargs"]["confirmed_plan_hash"] == "sha256:plan"


@pytest.mark.asyncio
async def test_delegate_to_agent_async_threads_runtime_budget_to_cloud_delegation(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    budget_run_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4(), tenant_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
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
            "_budget_run_id": str(budget_run_id),
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-1"
    assert captured["kwargs"]["budget_run_id"] == budget_run_id


@pytest.mark.asyncio
async def test_delegate_async_reserves_budget_before_creating_runtime_task(monkeypatch):
    from app.agents import orchestrator
    from app.agents.orchestrator import delegate_async

    captured: dict = {}

    class FakeBudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            return object()

    async def fake_create_runtime_task_record(**kwargs):
        captured["create"] = kwargs
        return kwargs["task_id"]

    async def fake_acquire_lease(**_kwargs):
        return SimpleNamespace(acquired=True, lease=SimpleNamespace(id="lease-1"))

    async def fake_send_signal(**_kwargs):
        return SimpleNamespace(id="signal-1", thread_id="trace-1")

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        acquire_lease = staticmethod(fake_acquire_lease)
        send_signal = staticmethod(fake_send_signal)

    def fake_gateway_scope(*_args, **_kwargs):
        return FakeGateway()

    monkeypatch.setattr(orchestrator, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(orchestrator, "gateway_scope", fake_gateway_scope)

    budget_run_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="BudgetedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do delegated work"}],
        owner_id=uuid4(),
        session_id="delegation-session",
        parent_agent_id=uuid4(),
        budget_run_id=budget_run_id,
        budget_service=FakeBudgetService(),
    )

    reservation = captured["reservation"]
    assert reservation.budget_run_id == budget_run_id
    assert reservation.reservation_key == f"delegation:{handle.task_id}:start"
    assert reservation.delegations == 1
    assert reservation.background_tasks == 1
    assert reservation.tokens >= 50_000
    assert reservation.cache_miss_tokens == reservation.tokens
    assert captured["create"]["budget_run_id"] == budget_run_id
    assert captured["create"]["budget_reservation_key"] == reservation.reservation_key
    assert captured["create"]["budget_admission_status"] == "reserved"
    receipt = handle.receipt
    assert receipt["schema"] == "hive.execution_receipt.v1"
    assert receipt["status"] == "pending"
    assert receipt["request_hash"]
    assert receipt["capability_snapshot_hash"]
    assert receipt["replay_key"] == f"delegation:{handle.task_id}"
    assert receipt["span_id"] == f"remote-action:{handle.task_id}"
    assert captured["create"]["metadata_json"]["execution_receipt"] == receipt


@pytest.mark.asyncio
async def test_delegate_async_approval_wait_persists_exact_task_without_dispatch(monkeypatch):
    from app.agents import orchestrator
    from app.agents.orchestrator import delegate_async
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    captured: dict = {}

    class WaitingBudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["delegations"],
            )

    async def fake_create_runtime_task_record(**kwargs):
        captured["create"] = kwargs
        return kwargs["task_id"]

    async def fake_notify(**kwargs):
        captured["notify"] = kwargs

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def acquire_lease(self, **_kwargs):
            return SimpleNamespace(acquired=True, lease=SimpleNamespace(id="lease-1"))

        async def send_signal(self, **_kwargs):
            return SimpleNamespace(id="signal-1", thread_id="trace-1")

    monkeypatch.setattr(orchestrator, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(orchestrator, "gateway_scope", lambda *_args, **_kwargs: FakeGateway())
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    handle = await delegate_async(
        target=SimpleNamespace(id=uuid4(), name="BudgetedWorker", role_description=""),
        target_model=SimpleNamespace(
            provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None
        ),
        conversation_messages=[{"role": "user", "content": "do delegated work"}],
        owner_id=uuid4(),
        session_id="delegation-session",
        parent_agent_id=uuid4(),
        budget_run_id=uuid4(),
        budget_service=WaitingBudgetService(),
    )

    assert handle.status == "waiting_budget_approval"
    assert captured["create"]["status"] == "pending"
    assert captured["create"]["budget_admission_status"] == "waiting_budget_approval"
    assert captured["create"]["budget_reservation_key"] == captured["reservation"].reservation_key
    assert "notify" not in captured


@pytest.mark.asyncio
async def test_local_agent_delegation_releases_budget_reservation_when_enqueue_fails(monkeypatch):
    from app.services import local_agent_channel_service
    from app.services.agent_tool_domains import messaging
    from app.services.agent_tool_domains.messaging import _delegate_to_local_agent_channel

    source_agent = SimpleNamespace(id=uuid4(), name="Source Agent", creator_id=uuid4(), tenant_id=uuid4())
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target Agent",
        creator_id=source_agent.creator_id,
        tenant_id=source_agent.tenant_id,
    )
    budget_run_id = uuid4()
    captured: dict[str, list] = {"reserve": [], "settle": []}

    class FakeBudgetService:
        async def reserve(self, reservation):
            captured["reserve"].append(reservation)

        async def settle(self, settlement):
            captured["settle"].append(settlement)

    class FakeSession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_exc):
            return False

    async def fail_create_channel_session(*_args, **_kwargs):
        raise RuntimeError("local channel unavailable")

    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", FakeBudgetService)
    monkeypatch.setattr(messaging, "tenant_scoped_session", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(local_agent_channel_service, "create_channel_session", fail_create_channel_session)

    with pytest.raises(RuntimeError, match="local channel unavailable"):
        await _delegate_to_local_agent_channel(
            source_agent=source_agent,
            target_agent=target_agent,
            message_text="Please work from the local machine.",
            args={},
            budget_run_id=budget_run_id,
        )

    assert len(captured["reserve"]) == 1
    assert len(captured["settle"]) == 1
    assert captured["settle"][0].budget_run_id == budget_run_id
    assert captured["settle"][0].reservation_key == captured["reserve"][0].reservation_key
    assert captured["settle"][0].reason == "local_agent_delegation_enqueue_failed"


@pytest.mark.asyncio
async def test_local_agent_delegation_waits_for_budget_approval_before_channel_write(monkeypatch):
    from app.services import local_agent_channel_service
    from app.services.agent_tool_domains.messaging import _delegate_to_local_agent_channel
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source Agent",
        creator_id=uuid4(),
        tenant_id=uuid4(),
    )
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target Agent",
        creator_id=uuid4(),
        tenant_id=source_agent.tenant_id,
    )

    class WaitingBudgetService:
        async def reserve(self, reservation):
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["delegations"],
            )

    async def fail_channel_write(*_args, **_kwargs):
        raise AssertionError("local channel must not be written before approval")

    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", WaitingBudgetService)
    monkeypatch.setattr(local_agent_channel_service, "create_channel_session", fail_channel_write)

    result = await _delegate_to_local_agent_channel(
        source_agent=source_agent,
        target_agent=target_agent,
        message_text="Please work locally.",
        args={},
        budget_run_id=uuid4(),
    )

    assert result["status"] == "waiting_budget_approval"
    assert result["error_code"] == "runtime_budget_approval_required"


@pytest.mark.asyncio
async def test_local_agent_delegation_targets_target_owner_and_returns_stable_receipt(monkeypatch):
    from app.services import local_agent_channel_service
    from app.services.agent_tool_domains import messaging
    from app.services.agent_tool_domains.messaging import _delegate_to_local_agent_channel

    source_owner_id = uuid4()
    target_owner_id = uuid4()
    tenant_id = uuid4()
    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source Agent",
        creator_id=source_owner_id,
        owner_user_id=source_owner_id,
        tenant_id=tenant_id,
    )
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target Mac",
        creator_id=target_owner_id,
        owner_user_id=target_owner_id,
        tenant_id=tenant_id,
    )
    session_id = uuid4()
    message_id = uuid4()
    runtime_task_id = uuid4()
    captured = {}

    class FakeSession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_exc):
            return False

    async def fake_create_channel_session(_db, **kwargs):
        captured["session"] = kwargs
        return {"id": session_id, "chat_session_id": None}

    async def fake_enqueue_channel_message(_db, **kwargs):
        captured["message"] = kwargs
        return {
            "id": str(message_id),
            "receipt": {
                "schema": "hive.execution_receipt.v1",
                "request_hash": "b" * 64,
                "capability_snapshot_hash": "a" * 64,
                "result_refs": [],
                "status": "pending",
                "replay_key": "local:task-1",
                "trace_id": f"local-agent:{message_id}",
                "span_id": f"remote-action:{message_id}",
            },
        }

    class FakeWsManager:
        async def send_to_user(self, owner_user_id, payload):
            captured["fanout"] = (owner_user_id, payload)

    monkeypatch.setattr(messaging, "tenant_scoped_session", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(local_agent_channel_service, "create_channel_session", fake_create_channel_session)
    monkeypatch.setattr(local_agent_channel_service, "enqueue_channel_message", fake_enqueue_channel_message)
    monkeypatch.setattr("app.api.local_agent_channel.channel_ws_manager", FakeWsManager())

    result = await _delegate_to_local_agent_channel(
        source_agent=source_agent,
        target_agent=target_agent,
        message_text="Inspect the target machine.",
        args={
            "_runtime_task_id": str(runtime_task_id),
            "parent_session_id": "parent-session-1",
        },
    )

    assert captured["session"]["owner_user_id"] == target_owner_id
    assert captured["session"]["actor_user_id"] == source_owner_id
    assert captured["session"]["source_agent_id"] == target_agent.id
    assert captured["message"]["owner_user_id"] == target_owner_id
    assert captured["message"]["sender_user_id"] == source_owner_id
    assert captured["message"]["sender_agent_id"] == source_agent.id
    assert captured["message"]["idempotency_key"].startswith(f"a2a-local:{runtime_task_id}:")
    assert captured["fanout"][0] == target_owner_id
    assert result["receipt"]["schema"] == "hive.execution_receipt.v1"
    assert result["chat_session_id"] is None


@pytest.mark.asyncio
async def test_delegate_to_agent_async_threads_cross_workspace_target_artifacts(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4(), tenant_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
    captured = {}
    target_artifacts = [
        {
            "path": "workspace/board-review.pptx",
            "workspace_scope": "target_agent_workspace",
            "expected_action": "modify_existing",
        },
        {
            "path": "workspace/src/forecast.py",
            "workspace_scope": "target_agent_workspace",
            "expected_action": "modify_existing",
        },
    ]

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
            "message": "Update the deck and code file.",
            "target_artifacts": target_artifacts,
            "edit_mode": "modify_existing",
        },
    )

    payload = json.loads(result)
    assert payload["target_artifacts"] == target_artifacts
    assert payload["edit_mode"] == "modify_existing"
    assert captured["kwargs"]["target_artifacts"] == target_artifacts
    assert captured["kwargs"]["edit_mode"] == "modify_existing"


@pytest.mark.asyncio
async def test_delegate_to_agent_async_defaults_to_peer_agent_tool_surface(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4(), tenant_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Feishu Knowledge", role_description="Knowledge worker")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
    captured = {}

    async def fake_resolve(_from_agent_id, _agent_name, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(task_id="task-peer", trace_id="trace-peer", target_name="Feishu Knowledge")

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    result = await _delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Feishu Knowledge",
            "message": "请查飞书知识库并返回报告",
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-peer"
    assert captured["kwargs"]["policy"].tool_profile == "agent_message"
    assert captured["kwargs"]["tenant_id"] == source_agent.tenant_id


@pytest.mark.asyncio
async def test_delegate_to_agent_async_accepts_timeout_override(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Knowledge Agent", role_description="Knowledge worker")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
    captured = {}

    async def fake_resolve(_from_agent_id, _agent_name, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(task_id="task-timeout", trace_id="trace-timeout", target_name="Knowledge Agent")

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    result = await _delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Knowledge Agent",
            "message": "scan the knowledge base and return cited findings",
            "timeout_seconds": 900,
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-timeout"
    assert payload["session_id"] == captured["kwargs"]["session_id"]
    assert captured["kwargs"]["policy"].timeout_seconds == 900.0


@pytest.mark.asyncio
async def test_delegate_to_agent_async_accepts_research_readonly_profile(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
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
    assert payload["session_id"] == captured["kwargs"]["session_id"]
    assert captured["kwargs"]["policy"].tool_profile == "research_readonly"


@pytest.mark.asyncio
async def test_delegate_to_agent_async_threads_permission_profile(monkeypatch):
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

    from_agent_id = uuid4()
    source_agent = SimpleNamespace(name="Source Agent", creator_id=uuid4(), tenant_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent")
    target_model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
    )
    captured = {}

    async def fake_resolve(_from_agent_id, _agent_name, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(task_id="task-permission", trace_id="trace-permission", target_name="Target Agent")

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    result = await _delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Target Agent",
            "message": "research and report",
            "parent_session_id": "parent-session-1",
            "_permission_profile": {
                "mode": "bypassPermissions",
                "allowed_tools": ["web_search", "feishu_doc_read"],
            },
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-permission"
    assert captured["kwargs"]["parent_session_id"] == "parent-session-1"
    assert captured["kwargs"]["permission_profile"] == {
        "mode": "bypassPermissions",
        "allowed_tools": ["web_search", "feishu_doc_read"],
    }


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
