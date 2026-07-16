from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _delegation_request(*, message: str = "complete the task"):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        OrchestrationPolicy,
        _build_delegation_execution_receipt,
    )
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    owner_id = uuid4()
    parent_agent_id = uuid4()
    target_agent_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=parent_agent_id,
        requester_user_id=owner_id,
        root_session_id="root-session-a2a",
        root_runtime_task_id="root-task-a2a",
        delegation_chain=(f"agent:{parent_agent_id}",),
    )
    request = AgentDelegationRequest(
        target=SimpleNamespace(
            id=target_agent_id,
            tenant_id=tenant_id,
            name="Target",
            role_description="Worker",
        ),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": message}],
        owner_id=owner_id,
        session_id="child-session-a2a",
        parent_agent_id=parent_agent_id,
        parent_session_id="root-session-a2a",
        trace_id="trace-a2a",
        policy=OrchestrationPolicy(tool_profile="worker_safe"),
        execution_principal=principal.to_evidence(),
        root_runtime_task_id="root-task-a2a",
        tenant_id=tenant_id,
        runtime_task_id="task-a2a",
        permission_profile={
            "mode": "dontAsk",
            "allowed_tools": ["read_file"],
            "sandbox": "read_only",
        },
    )
    request.execution_receipt = _build_delegation_execution_receipt(
        request,
        task_id="task-a2a",
        trace_id="trace-a2a",
        status="pending",
    )
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_state", "reason_code"),
    [
        ("missing", "a2a_authority_receipt_missing"),
        ("capability_drift", "a2a_authority_snapshot_drift"),
    ],
)
async def test_persisted_dispatch_holds_invalid_authority_receipt_before_spawn(
    monkeypatch,
    receipt_state,
    reason_code,
):
    from app.agents import orchestrator

    request = _delegation_request()
    if receipt_state == "missing":
        request.execution_receipt = None
    else:
        request.permission_profile = {
            "mode": "dontAsk",
            "allowed_tools": ["read_file", "write_file"],
            "sandbox": "workspace_write",
        }
    record = {
        "task_id": "task-a2a",
        "task_type": "delegation",
        "status": "pending",
        "trace_id": "trace-a2a",
        "child_session_id": "child-session-a2a",
        "metadata": {"resumable_delegation": True, "resume_after_restart": True},
    }
    updates: list[dict] = []
    spawns: list[dict] = []

    async def fake_get(_task_id):
        return record

    async def fake_build(_record):
        return request

    async def fake_update(_task_id, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(orchestrator, "get_runtime_task_record", fake_get)
    monkeypatch.setattr(orchestrator, "_build_delegation_request_from_runtime_record", fake_build)
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(orchestrator, "_spawn_async_delegation_task", lambda **kwargs: spawns.append(kwargs))

    dispatched = await orchestrator.dispatch_persisted_async_delegation("task-a2a")

    assert dispatched is False
    assert spawns == []
    assert updates[-1]["status"] == "needs_reconciliation"
    evidence = updates[-1]["metadata_json"]
    assert evidence["restart_resume_blocker"] == reason_code
    assert evidence["automatic_retry_disabled"] is True
    assert evidence["authority_reconciliation"]["reason_code"] == reason_code


@pytest.mark.asyncio
async def test_delegate_rechecks_persisted_receipt_before_model_path(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request()
    request.conversation_messages.append({"role": "user", "content": "changed after receipt"})
    delegated = False

    async def fake_delegate_after_cycle_check(*_args, **_kwargs):
        nonlocal delegated
        delegated = True
        raise AssertionError("model path must not run after receipt drift")

    monkeypatch.setattr(orchestrator, "_delegate_after_cycle_check", fake_delegate_after_cycle_check)

    result = await orchestrator._delegate(request)

    assert delegated is False
    assert result.failed is True
    assert result.terminal_reason == "a2a_request_snapshot_drift"
    assert result.parts[0]["status"] == "needs_reconciliation"


@pytest.mark.asyncio
async def test_fresh_a2a_without_execution_principal_stops_before_model_path(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request()
    request.execution_principal = None
    request.execution_receipt = None
    delegated = False

    async def fake_delegate_after_cycle_check(*_args, **_kwargs):
        nonlocal delegated
        delegated = True
        raise AssertionError("an A2A run without authenticated principal must not enter the model path")

    monkeypatch.setattr(orchestrator, "_delegate_after_cycle_check", fake_delegate_after_cycle_check)

    result = await orchestrator._delegate(request)

    assert delegated is False
    assert result.failed is True
    assert result.terminal_reason == "a2a_execution_principal_missing"
    assert result.parts[0]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_fresh_sync_delegation_stamps_generated_trace_into_authority_snapshot(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request()
    request.trace_id = None
    request.execution_receipt = None
    captured: dict[str, str | None] = {}

    async def fake_delegate_after_cycle_check(current, *, trace_id, **_kwargs):
        captured["request_trace_id"] = current.trace_id
        captured["snapshot_trace_id"] = orchestrator._delegation_authority_snapshot(current)["trace_id"]
        return orchestrator.AgentDelegationResult(
            content="ok",
            child_session_id=current.session_id,
            trace_id=trace_id,
            depth=current.depth,
        )

    monkeypatch.setattr(orchestrator, "_delegate_after_cycle_check", fake_delegate_after_cycle_check)

    result = await orchestrator._delegate(request)

    assert result.failed is False
    assert captured["request_trace_id"] == result.trace_id
    assert captured["snapshot_trace_id"] == result.trace_id


@pytest.mark.asyncio
async def test_restart_resume_uses_canonical_request_rehydration_and_holds_drift(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request()
    request.permission_profile = {
        "mode": "dontAsk",
        "allowed_tools": ["read_file", "write_file"],
        "sandbox": "workspace_write",
    }
    record = {
        "task_id": "task-a2a",
        "task_type": "delegation",
        "status": "pending",
        "trace_id": "trace-a2a",
        "child_session_id": "child-session-a2a",
        "metadata": {
            "resumable_delegation": True,
            "resume_after_restart": True,
            "tool_profile": "research_readonly",
        },
    }
    builds: list[dict] = []
    updates: list[dict] = []

    async def fake_list_active(**_kwargs):
        return [record]

    async def fake_build(runtime_record):
        builds.append(runtime_record)
        return request

    async def fake_update(_task_id, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(orchestrator, "list_active_runtime_task_records", fake_list_active)
    monkeypatch.setattr(orchestrator, "_build_delegation_request_from_runtime_record", fake_build)
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(
        orchestrator,
        "_spawn_async_delegation_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("drifted task must not spawn")),
    )
    orchestrator._async_tasks.clear()

    resumed = await orchestrator.resume_persisted_async_delegations()

    assert resumed == []
    assert builds == [record]
    assert updates[-1]["status"] == "needs_reconciliation"
    assert updates[-1]["metadata_json"]["restart_resume_blocker"] == "a2a_authority_snapshot_drift"


def test_nested_a2a_principal_preserves_root_and_extends_chain():
    from app.agents.orchestrator import AgentDelegationRequest, _child_execution_principal
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = uuid4()
    owner_id = uuid4()
    agent_a = uuid4()
    agent_b = uuid4()
    agent_c = uuid4()
    root = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=agent_a,
        requester_user_id=owner_id,
        root_session_id="root-session-nested",
        root_runtime_task_id="root-task-nested",
        delegation_chain=(f"agent:{agent_a}",),
    )
    request_ab = AgentDelegationRequest(
        target=SimpleNamespace(id=agent_b, tenant_id=tenant_id),
        target_model=object(),
        conversation_messages=[{"role": "user", "content": "A to B"}],
        owner_id=owner_id,
        session_id="session-b",
        parent_agent_id=agent_a,
        execution_principal=root.to_evidence(),
        tenant_id=tenant_id,
    )
    principal_b = _child_execution_principal(request_ab)
    assert principal_b is not None
    request_bc = AgentDelegationRequest(
        target=SimpleNamespace(id=agent_c, tenant_id=tenant_id),
        target_model=object(),
        conversation_messages=[{"role": "user", "content": "B to C"}],
        owner_id=owner_id,
        session_id="session-c",
        parent_agent_id=agent_b,
        execution_principal=principal_b.to_evidence(),
        tenant_id=tenant_id,
    )

    principal_c = _child_execution_principal(request_bc)

    assert principal_c is not None
    assert principal_c.tenant_id == tenant_id
    assert principal_c.requester_user_id == owner_id
    assert principal_c.root_session_id == "root-session-nested"
    assert principal_c.root_runtime_task_id == "root-task-nested"
    assert principal_c.source_agent_id == agent_c
    assert principal_c.delegation_chain == (
        f"agent:{agent_a}",
        f"agent:{agent_b}",
        f"agent:{agent_c}",
    )


def test_child_principal_rejects_target_tenant_drift_even_with_matching_request_tenant():
    from app.agents.orchestrator import _child_execution_principal

    request = _delegation_request()
    request.target.tenant_id = uuid4()

    with pytest.raises(ValueError, match="target Agent tenant"):
        _child_execution_principal(request)


@pytest.mark.asyncio
async def test_fresh_a2a_requires_explicit_parent_agent_binding(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request()
    request.parent_agent_id = None
    request.execution_receipt = None

    async def unexpected_model_path(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("A2A without a bound parent Agent must not enter the model path")

    monkeypatch.setattr(orchestrator, "_delegate_after_cycle_check", unexpected_model_path)

    result = await orchestrator._delegate(request)

    assert result.failed is True
    assert result.terminal_reason == "a2a_execution_principal_drift"
    assert "parent Agent" in result.parts[0]["authority_reconciliation"]["actual"]["execution_principal_error"]


@pytest.mark.asyncio
async def test_valid_receipt_does_not_scan_benign_security_tool_words(monkeypatch):
    from app.agents import orchestrator

    request = _delegation_request(
        message="Explain why security approval tool and secret are words in this harmless sentence."
    )
    record = {
        "task_id": "task-a2a",
        "task_type": "delegation",
        "status": "pending",
        "metadata": {"coordination_publish_state": "published"},
    }
    spawns: list[dict] = []

    async def fake_get(_task_id):
        return record

    async def fake_build(_record):
        return request

    async def fake_update(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "get_runtime_task_record", fake_get)
    monkeypatch.setattr(orchestrator, "_build_delegation_request_from_runtime_record", fake_build)
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(orchestrator, "_spawn_async_delegation_task", lambda **kwargs: spawns.append(kwargs))

    dispatched = await orchestrator.dispatch_persisted_async_delegation("task-a2a")

    assert dispatched is True
    assert len(spawns) == 1
