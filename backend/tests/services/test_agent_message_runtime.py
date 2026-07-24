from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _a2a_authority_kwargs(*, target, owner_id, parent_agent_id, session_id: str) -> dict:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = getattr(target, "tenant_id", None) or uuid4()
    target.tenant_id = tenant_id
    return {
        "tenant_id": tenant_id,
        "execution_principal": ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=parent_agent_id,
            requester_user_id=owner_id,
            root_session_id=session_id,
            delegation_chain=(f"agent:{parent_agent_id}",),
        ).to_evidence(),
    }


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

    async def fake_execute_tool(tool_name, args, agent_id, user_id, *, emit_runtime_hooks=True, **_kwargs):
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
async def test_agent_message_tool_executor_propagates_bounded_a2a_context(monkeypatch):
    from app.services.agent_tools import _build_agent_message_tool_executor

    captured = {}

    async def fake_execute_tool(tool_name, args, *_args, **_kwargs):
        captured["tool_name"] = tool_name
        captured["args"] = dict(args)
        return "ok"

    async def fake_persist(**_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_tools.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging._persist_agent_tool_call", fake_persist)
    executor = _build_agent_message_tool_executor(
        target_agent_id=uuid4(),
        owner_id=uuid4(),
        session_agent_id=uuid4(),
        session_id="nested-session",
        participant_id=uuid4(),
        delegation_trace_id="trace-a-b-c",
        delegation_depth=1,
        delegation_max_depth=3,
    )

    await executor("send_message_to_agent", {"agent_name": "C", "message": "continue"})

    assert captured["args"]["_a2a_trace_id"] == "trace-a-b-c"
    assert captured["args"]["_a2a_depth"] == 2
    assert captured["args"]["_a2a_max_depth"] == 3


@pytest.mark.asyncio
async def test_agent_message_tool_executor_forwards_complete_authority_frame(monkeypatch):
    from app.core.execution_context import A2AToolAuthorityFrame, ExecutionIdentity, ExecutionPrincipal
    from app.runtime.ccplus_contracts import permission_profile_snapshot, permission_profile_snapshot_hash
    from app.services.agent_tools import _build_agent_message_tool_executor
    from app.services.execution_receipts import canonical_payload_hash

    target_id = uuid4()
    owner_id = uuid4()
    tenant_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=target_id,
        requester_user_id=owner_id,
        root_session_id="root-session-1",
        root_runtime_task_id="root-task-1",
        delegation_chain=("agent:source", f"agent:{target_id}"),
    )
    identity = ExecutionIdentity(
        identity_type="delegated_user",
        identity_id=owner_id,
        label="requester via web",
    )
    profile = {
        "mode": "dontAsk",
        "allowed_tools": ["read_file"],
        "sandbox": "read_only",
    }
    trace_sink: dict[str, object] = {}
    captured: dict[str, object] = {}
    profile_snapshot = permission_profile_snapshot(profile)
    capability_snapshot = {
        "schema": "hive.a2a_authority_snapshot.v1",
        "tenant_id": str(tenant_id),
        "owner_id": str(owner_id),
        "source_agent_id": "source-agent",
        "target_agent_id": str(target_id),
        "session_id": "child-session-1",
        "parent_session_id": "root-session-1",
        "trace_id": "trace-a2a-1",
        "runtime_task_id": "child-task-1",
        "root_runtime_task_id": "root-task-1",
        "budget_run_id": "budget-1",
        "interaction_type": "agent_message",
        "depth": 2,
        "tool_profile": "agent_message",
        "permission_profile": profile_snapshot,
        "execution_identity": {
            "identity_type": identity.identity_type,
            "identity_id": str(identity.identity_id),
            "label": identity.label,
        },
        "execution_principal": principal.to_evidence(),
    }
    authority_frame = A2AToolAuthorityFrame(
        schema="hive.a2a_tool_authority_frame.v1",
        principal=principal,
        capability_snapshot=capability_snapshot,
        capability_snapshot_hash=canonical_payload_hash(capability_snapshot),
        policy_snapshot_hash=permission_profile_snapshot_hash(profile),
        permission_profile=profile_snapshot,
        execution_identity=identity,
        session_id="child-session-1",
        parent_session_id="root-session-1",
        runtime_task_id="child-task-1",
        root_runtime_task_id="root-task-1",
        budget_run_id="budget-1",
        trace_id="trace-a2a-1",
        sandbox_profile="read_only",
        approval_policy=str(profile_snapshot["approval_policy"]),
    )

    async def fake_execute_tool(tool_name, args, agent_id, user_id, **kwargs):
        captured.update(
            {
                "tool_name": tool_name,
                "args": dict(args),
                "agent_id": agent_id,
                "user_id": user_id,
                "kwargs": kwargs,
            }
        )
        return "ok"

    async def fake_persist(**_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_tools.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging._persist_agent_tool_call", fake_persist)
    executor = _build_agent_message_tool_executor(
        target_agent_id=target_id,
        owner_id=owner_id,
        session_agent_id=target_id,
        session_id="child-session-1",
        participant_id=uuid4(),
        delegation_trace_id="trace-a2a-1",
        delegation_depth=2,
        delegation_max_depth=4,
    )

    result = await executor(
        "read_file",
        {"path": "workspace/report.md"},
        authority_frame=authority_frame,
        event_callback="event-callback",
        tool_call_id="tool-call-1",
        trace_metadata_sink=trace_sink,
        turn_id="turn-1",
        origin_channel="agent",
        round_state={"trace_id": "trace-a2a-1"},
        t0_refs=("t0://event/1",),
        plan_mode_interactive_available=False,
        plan_mode_unattended_available=True,
        emit_runtime_hooks=False,
    )

    assert result == "ok"
    assert captured["agent_id"] == target_id
    assert captured["user_id"] == owner_id
    kwargs = captured["kwargs"]
    assert kwargs["execution_identity"] is identity
    assert kwargs["authority_frame"] is authority_frame
    assert kwargs["delegation_token"] is None
    assert kwargs["event_callback"] == "event-callback"
    assert kwargs["permission_profile"] == profile_snapshot
    assert kwargs["tool_call_id"] == "tool-call-1"
    assert kwargs["trace_metadata_sink"] is trace_sink
    assert kwargs["session_id"] == "child-session-1"
    assert kwargs["turn_id"] == "turn-1"
    assert kwargs["runtime_task_id"] == "child-task-1"
    assert kwargs["budget_run_id"] == "budget-1"
    assert kwargs["origin_channel"] == "agent"
    assert kwargs["round_state"] == {"trace_id": "trace-a2a-1"}
    assert kwargs["t0_refs"] == ("t0://event/1",)
    assert kwargs["plan_mode_unattended_available"] is True
    assert kwargs["emit_runtime_hooks"] is False


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
    requester_user_id = uuid4()
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
            "_execution_principal": {
                "schema": "hive.execution_principal.v1",
                "tenant_id": str(source_agent.tenant_id),
                "source_agent_id": str(from_agent_id),
                "requester_user_id": str(requester_user_id),
                "root_session_id": "root-session-7",
                "root_runtime_task_id": "root-task-7",
                "origin": "agent_tool",
                "delegation_chain": [],
            },
        },
    )

    payload = json.loads(result)
    assert payload["task_id"] == "task-1"
    assert captured["kwargs"]["budget_run_id"] == budget_run_id
    assert captured["kwargs"]["owner_id"] == requester_user_id
    assert captured["kwargs"]["parent_session_id"] == "root-session-7"
    assert captured["kwargs"]["root_runtime_task_id"] == "root-task-7"
    assert captured["kwargs"]["execution_principal"]["requester_user_id"] == str(requester_user_id)


@pytest.mark.asyncio
async def test_delegate_async_propagates_authority_unavailable_as_typed_failure(monkeypatch):
    from app.core.execution_context import ExecutionPrincipal
    from app.services.agent_tool_domains.messaging import _delegate_to_agent_async_outcome

    from_agent_id = uuid4()
    requester_user_id = uuid4()
    tenant_id = uuid4()
    source_agent = SimpleNamespace(
        id=from_agent_id,
        name="Source Agent",
        creator_id=requester_user_id,
        tenant_id=tenant_id,
    )
    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful agent", tenant_id=tenant_id)
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=from_agent_id,
        requester_user_id=requester_user_id,
        root_session_id="root-session-authority-failure",
    )

    async def fake_resolve(*_args, **_kwargs):
        return source_agent, target, target_model, None

    async def fake_delegate_async(**_kwargs):
        return SimpleNamespace(
            task_id="authority_unavailable",
            trace_id="trace-authority-failure",
            target_name=target.name,
            status="authority_unavailable:a2a_execution_principal_drift",
        )

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    outcome = await _delegate_to_agent_async_outcome(
        from_agent_id,
        {"agent_name": target.name, "message": "do work"},
        principal=principal,
    )

    assert outcome.ok is False
    assert outcome.status == "unavailable"
    assert outcome.error_code == "a2a_execution_principal_drift"


def test_delegation_runtime_failure_maps_typed_unavailable():
    from app.services.agent_tool_domains.messaging import _delegation_runtime_failure_outcome

    outcome = _delegation_runtime_failure_outcome(
        "consult",
        SimpleNamespace(
            failed=True,
            content="A2A authority verification failed; the delegated invocation was not started.",
            terminal_reason="a2a_execution_principal_missing",
            parts=(
                {
                    "type": "runtime_status",
                    "status": "unavailable",
                    "error_code": "a2a_execution_principal_missing",
                    "retryable": False,
                },
            ),
        ),
    )

    assert outcome is not None
    assert outcome.ok is False
    assert outcome.status == "unavailable"
    assert outcome.error_code == "a2a_execution_principal_missing"
    assert outcome.retryable is False


def test_delegation_runtime_provider_error_cannot_be_reported_as_success():
    from app.services.agent_tool_domains.messaging import _delegation_runtime_failure_outcome

    outcome = _delegation_runtime_failure_outcome(
        "consult",
        SimpleNamespace(
            failed=False,
            content="[LLM Error] AI 模型额度或余额不足。",
            terminal_reason="provider_error",
            parts=(),
        ),
    )

    assert outcome is not None
    assert outcome.ok is False
    assert outcome.status == "failed"
    assert outcome.error_code == "provider_error"


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

    async def fake_update_runtime_task_record(*_args, **kwargs):
        captured["update"] = kwargs
        return True

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
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(orchestrator, "gateway_scope", fake_gateway_scope)

    budget_run_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="BudgetedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_id = uuid4()
    parent_agent_id = uuid4()

    handle = await delegate_async(
        target=target,
        target_model=model,
        conversation_messages=[{"role": "user", "content": "do delegated work"}],
        owner_id=owner_id,
        session_id="delegation-session",
        parent_agent_id=parent_agent_id,
        budget_run_id=budget_run_id,
        budget_service=FakeBudgetService(),
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            parent_agent_id=parent_agent_id,
            session_id="delegation-session",
        ),
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

    target = SimpleNamespace(id=uuid4(), name="BudgetedWorker", role_description="")
    owner_id = uuid4()
    parent_agent_id = uuid4()
    handle = await delegate_async(
        target=target,
        target_model=SimpleNamespace(
            provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None
        ),
        conversation_messages=[{"role": "user", "content": "do delegated work"}],
        owner_id=owner_id,
        session_id="delegation-session",
        parent_agent_id=parent_agent_id,
        budget_run_id=uuid4(),
        budget_service=WaitingBudgetService(),
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            parent_agent_id=parent_agent_id,
            session_id="delegation-session",
        ),
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
    from app.core.execution_context import ExecutionPrincipal
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
    parent_session_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent.id,
        requester_user_id=source_owner_id,
        root_session_id=str(parent_session_id),
        root_runtime_task_id=str(runtime_task_id),
    )
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
            "parent_session_id": str(parent_session_id),
            "_execution_principal": principal.to_evidence(),
        },
    )

    assert captured["session"]["owner_user_id"] == target_owner_id
    assert captured["session"]["actor_user_id"] == source_owner_id
    assert captured["session"]["source_agent_id"] == target_agent.id
    assert captured["session"]["commit"] is False
    assert captured["session"]["reuse_existing"] is True
    assert captured["message"]["owner_user_id"] == target_owner_id
    assert captured["message"]["sender_user_id"] == source_owner_id
    assert captured["message"]["sender_agent_id"] == source_agent.id
    assert captured["message"]["metadata"]["target_agent_name"] == "Target Mac"
    assert captured["message"]["metadata"]["parent_session_id"] == str(parent_session_id)
    assert captured["message"]["metadata"]["execution_principal"] == principal.to_evidence()
    assert captured["message"]["idempotency_key"].startswith(f"a2a-local:{runtime_task_id}:")
    assert captured["fanout"][0] == target_owner_id
    assert result["receipt"]["schema"] == "hive.execution_receipt.v1"
    assert result["chat_session_id"] is None
    assert "delivered automatically back into this source Agent session" in result["next_action"]
    assert "check_async_task with message_id" in result["next_action"]


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
async def test_list_async_tasks_fails_closed_when_authority_db_is_unavailable(monkeypatch):
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

    from app.core.execution_context import ExecutionPrincipal

    result = await _list_async_tasks(
        owner_a,
        principal=ExecutionPrincipal(
            tenant_id=uuid4(),
            source_agent_id=owner_a,
            requester_user_id=uuid4(),
            root_session_id="root-session-a",
        ),
    )
    assert "authority evidence is unavailable" in result

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

    from app.core.execution_context import ExecutionPrincipal

    result = await _check_async_task(
        owner_b,
        {"task_id": handle.task_id},
        principal=ExecutionPrincipal(
            tenant_id=uuid4(),
            source_agent_id=owner_b,
            requester_user_id=uuid4(),
            root_session_id="root-session-b",
        ),
    )
    assert "authority evidence is unavailable" in result

    never_finish.set()
    await check_async_delegation(handle.task_id)


@pytest.mark.asyncio
async def test_check_async_task_reads_authorized_local_agent_message_result(monkeypatch):
    from app.core.execution_context import ExecutionPrincipal
    from app.services.agent_tool_domains import messaging
    from app.services.agent_tool_domains.messaging import _check_async_task

    tenant_id = uuid4()
    requester_user_id = uuid4()
    source_agent_id = uuid4()
    target_agent_id = uuid4()
    target_owner_user_id = uuid4()
    parent_session_id = uuid4()
    message_id = uuid4()
    notification_id = uuid4()
    stored_principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
        root_runtime_task_id=str(uuid4()),
    )
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=requester_user_id,
        root_session_id=str(parent_session_id),
        root_runtime_task_id=str(uuid4()),
    )
    message = SimpleNamespace(
        id=message_id,
        tenant_id=tenant_id,
        owner_user_id=target_owner_user_id,
        sender_agent_id=source_agent_id,
        sender_user_id=requester_user_id,
        source_agent_id=target_agent_id,
        status="completed",
        result="Local evidence is complete.",
        metadata_json={
            "source": "a2a",
            "execution_target": "local_agent",
            "sender_agent_id": str(source_agent_id),
            "target_agent_id": str(target_agent_id),
            "target_agent_name": "Target Mac",
            "target_owner_user_id": str(target_owner_user_id),
            "parent_session_id": str(parent_session_id),
            "execution_principal": stored_principal.to_evidence(),
            "report": {"artifacts": [{"path": "workspace/local-result.md"}]},
            "source_delivery": {
                "status": "queued",
                "notification_id": str(notification_id),
            },
        },
        request_hash=None,
        capability_snapshot_hash=None,
        replay_key="local:test",
        receipt_trace_id=None,
        receipt_span_id=None,
        completed_at=None,
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        tenant_id=tenant_id,
        agent_id=source_agent_id,
        user_id=requester_user_id,
    )
    outbox = SimpleNamespace(
        id=notification_id,
        status="delivered",
        attempt_count=1,
        last_error=None,
        delivered_at=None,
        delivery_receipt_json={"status": "continued"},
    )

    class _ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self):
            self.values = [message, parent_session, outbox]

        async def execute(self, _stmt):
            return _ScalarResult(self.values.pop(0))

    class _Session:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(messaging, "tenant_scoped_session", lambda *_args, **_kwargs: _Session())

    result = await _check_async_task(
        source_agent_id,
        {"message_id": str(message_id)},
        principal=principal,
    )
    payload = json.loads(result)

    assert payload["kind"] == "local_agent_delegation"
    assert payload["message_id"] == str(message_id)
    assert payload["status"] == "completed"
    assert payload["terminal"] is True
    assert payload["result"] == "Local evidence is complete."
    assert payload["artifacts"] == [{"path": "workspace/local-result.md"}]
    assert payload["target_agent_id"] == str(target_agent_id)
    assert payload["source_delivery"]["status"] == "delivered"
    assert payload["source_delivery"]["notification_id"] == str(notification_id)


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

    from app.core.execution_context import ExecutionPrincipal

    result = await _cancel_async_task(
        owner_b,
        {"task_id": handle.task_id},
        principal=ExecutionPrincipal(
            tenant_id=uuid4(),
            source_agent_id=owner_b,
            requester_user_id=uuid4(),
            root_session_id="root-session-b",
        ),
    )
    assert "authority evidence is unavailable" in result

    never_finish.set()
    await check_async_delegation(handle.task_id)
