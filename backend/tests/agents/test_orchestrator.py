from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


def _a2a_authority_kwargs(
    *,
    target,
    owner_id,
    session_id: str,
    parent_agent_id=None,
    parent_session_id: str | None = None,
    root_runtime_task_id: str | None = None,
) -> dict:
    """Build the authenticated parent frame required by a live delegation."""
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = getattr(target, "tenant_id", None) or uuid4()
    target.tenant_id = tenant_id
    source_agent_id = parent_agent_id or uuid4()
    root_session_id = parent_session_id or session_id
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=owner_id,
        root_session_id=root_session_id,
        root_runtime_task_id=root_runtime_task_id,
        delegation_chain=(f"agent:{source_agent_id}",),
    )
    return {
        "parent_agent_id": source_agent_id,
        "parent_session_id": root_session_id,
        "execution_principal": principal.to_evidence(),
        "root_runtime_task_id": root_runtime_task_id,
    }


def _persisted_a2a_authority_metadata(
    *,
    task_id: str,
    trace_id: str,
    target,
    target_model,
    owner_id,
    parent_agent_id,
    parent_session_id: str,
    child_session_id: str,
    conversation_messages: list[dict],
    tool_profile: str,
    timeout_seconds: float = 120.0,
    max_tool_rounds: int | None = None,
    execution_identity_metadata: dict | None = None,
) -> dict:
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        OrchestrationPolicy,
        _build_delegation_execution_receipt,
        _execution_identity_from_metadata,
    )
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = getattr(target, "tenant_id", None) or uuid4()
    target.tenant_id = tenant_id
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=parent_agent_id,
        requester_user_id=owner_id,
        root_session_id=parent_session_id,
        delegation_chain=(f"agent:{parent_agent_id}",),
    )
    request = AgentDelegationRequest(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        owner_id=owner_id,
        session_id=child_session_id,
        system_prompt_suffix="",
        max_tool_rounds=max_tool_rounds,
        parent_agent_id=parent_agent_id,
        parent_session_id=parent_session_id,
        trace_id=trace_id,
        policy=OrchestrationPolicy(timeout_seconds=timeout_seconds, tool_profile=tool_profile),
        execution_identity=_execution_identity_from_metadata(execution_identity_metadata),
        execution_principal=principal.to_evidence(),
        runtime_task_id=task_id,
    )
    request.execution_receipt = _build_delegation_execution_receipt(
        request,
        task_id=task_id,
        trace_id=trace_id,
        status="pending",
    )
    return {
        "tenant_id": str(tenant_id),
        "execution_principal": principal.to_evidence(),
        "execution_receipt": request.execution_receipt,
    }


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
    parent_agent_id = uuid4()
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
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="session-1",
                parent_agent_id=parent_agent_id,
            ),
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
    assert request.core_tools_only is False
    # Default delegation inherits the governed parent capability surface. The
    # harness may deny human-facing interaction tools, but it must not replace
    # the child's judgment by mechanically removing Memory, Skill, or bounded
    # nested-collaboration capabilities.
    from app.agents.orchestrator import _DELEGATION_BASE_EXCLUDED_TOOLS

    excluded = set(request.excluded_tool_names)
    assert excluded == set(_DELEGATION_BASE_EXCLUDED_TOOLS)
    assert {"save_memory", "save_skill", "search_memory", "load_memory"}.isdisjoint(excluded)
    assert {"delegate_to_agent", "spawn_subagent", "check_subagent"}.isdisjoint(excluded)
    assert request.max_tool_rounds == 7
    assert "A2A_SUFFIX" in request.system_prompt_suffix
    # F-1: slim worker prompt — isolation_contract + tool_policy remain; forced
    # return template (Completed/Evidence/Blockers) was removed as an L1 violation.
    assert "<isolation_contract>" in request.system_prompt_suffix
    assert "<tool_policy>" in request.system_prompt_suffix
    assert "Completed:" not in request.system_prompt_suffix
    assert "Evidence:" not in request.system_prompt_suffix
    assert "Blockers:" not in request.system_prompt_suffix
    assert (
        "all durable writes still pass evidence, permission, review, and rollback governance"
        in request.system_prompt_suffix
    )
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_inherited_governed"
    assert request.session_context.metadata["delegation_memory_policy"] == "governed_long_term_memory"
    assert request.delegation_token is not None
    assert request.delegation_token.parent_agent_id == parent_agent_id
    assert request.delegation_token.child_agent_id == target.id
    assert request.delegation_token.inherit_parent_capabilities is True
    assert request.delegation_token.granted_capabilities == frozenset()
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "delegated_user"
    assert request.execution_identity.identity_id == user_id
    assert request.execution_identity.label == "User via web"
    assert request.session_context.metadata["delegation_token_id"] == request.delegation_token.delegation_id
    assert request.session_context.metadata["delegation_token_capabilities"] == []


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

    projected: list[dict] = []

    async def fake_project_terminal(**kwargs):
        projected.append(kwargs)

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.agents.orchestrator._project_delegation_request_terminal_to_parent",
        fake_project_terminal,
        raising=False,
    )

    kwargs = {
        "target": target,
        "target_model": target_model,
        "conversation_messages": [{"role": "user", "content": "Prepare the market map"}],
        "owner_id": owner_id,
        "session_id": "session-lease",
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="session-lease",
            parent_agent_id=parent_id,
        ),
    }
    first = await delegate_async(**kwargs)
    second = await delegate_async(**kwargs)

    assert first.status == "queued"
    assert first.coordination_lease_id
    assert second.status == "blocked_by_lease"
    assert second.blocked_by_lease_id == first.coordination_lease_id
    assert coordination_runtime.read_signals(str(target.id), thread_id=first.signal_thread_id)
    assert projected == [
        {
            "request": projected[0]["request"],
            "status": "blocked",
            "summary": "Delegation was not admitted because equivalent work holds the coordination lease.",
            "reason": "blocked_by_coordination_lease",
        }
    ]


@pytest.mark.asyncio
async def test_delegate_async_captures_execution_identity_before_background_spawn(monkeypatch):
    from app.agents.orchestrator import delegate_async
    from app.core.execution_context import ExecutionIdentity, clear_execution_identity, set_execution_identity

    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    created: dict = {}
    spawned: list[dict] = []
    wakeups: list[dict] = []

    async def fake_create_runtime_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*_args, **_kwargs):
        return None

    async def fake_persist_delegation_event(**_kwargs):
        return None

    async def fake_notify_runtime_task_worker(**kwargs):
        wakeups.append(kwargs)

    def fake_spawn_async_delegation_task(*, task_id, request, trace_id):
        spawned.append({"task_id": task_id, "request": request, "trace_id": trace_id})

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", fake_spawn_async_delegation_task)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify_runtime_task_worker)

    user_id = uuid4()
    owner_id = uuid4()
    parent_agent_id = uuid4()
    set_execution_identity(
        ExecutionIdentity(identity_type="delegated_user", identity_id=user_id, label="User via Feishu")
    )
    try:
        handle = await delegate_async(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "Prepare the market map"}],
            owner_id=owner_id,
            session_id="session-identity",
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="session-identity",
                parent_agent_id=parent_agent_id,
            ),
        )
    finally:
        clear_execution_identity()

    assert handle.status == "queued"
    assert spawned == []
    assert wakeups == [{"reason": "delegation_created", "runtime_task_id": handle.task_id}]
    metadata = created["metadata_json"]
    assert metadata["execution_identity"]["identity_type"] == "delegated_user"
    assert metadata["execution_identity"]["identity_id"] == str(user_id)
    assert metadata["execution_identity"]["label"] == "User via Feishu"


@pytest.mark.asyncio
async def test_delegate_async_enqueues_for_worker_claim_instead_of_in_process_spawn(monkeypatch):
    from app.agents.orchestrator import delegate_async

    target = SimpleNamespace(id=uuid4(), name="Target Agent", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    created: dict = {}
    updates: list[tuple[tuple, dict]] = []
    wakeups: list[dict] = []
    spawned: list[dict] = []

    async def fake_create_runtime_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*args, **kwargs):
        updates.append((args, kwargs))
        return True

    async def fake_persist_delegation_event(**_kwargs):
        return None

    async def fake_notify_runtime_task_worker(**kwargs):
        wakeups.append(kwargs)

    def fake_spawn_async_delegation_task(**kwargs):
        spawned.append(kwargs)

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", fake_spawn_async_delegation_task)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify_runtime_task_worker)

    owner_id = uuid4()
    parent_agent_id = uuid4()
    handle = await delegate_async(
        target=target,
        target_model=target_model,
        conversation_messages=[{"role": "user", "content": "Prepare the market map"}],
        owner_id=owner_id,
        session_id="session-worker-claim",
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="session-worker-claim",
            parent_agent_id=parent_agent_id,
            parent_session_id="parent-session-worker-claim",
        ),
    )

    assert handle.status == "queued"
    assert created["task_type"] == "delegation"
    assert created["status"] == "suspended"
    assert spawned == []
    assert len(updates) == 1
    assert updates[0][1]["status"] == "pending"
    assert updates[0][1]["metadata_json"]["coordination_publish_state"] == "published"
    assert wakeups == [{"reason": "delegation_created", "runtime_task_id": handle.task_id}]


@pytest.mark.asyncio
async def test_delegate_async_commits_enqueue_before_coordination_publish_and_worker_wake(monkeypatch):
    from app.agents.coordination import Lease, LeaseAcquireResult
    from app.agents.orchestrator import delegate_async

    order: list[str] = []
    created: dict = {}

    class Gateway:
        async def acquire_lease(self, *, task_key, agent_id, ttl_seconds):
            order.append("lease")
            return LeaseAcquireResult(
                acquired=True,
                lease=Lease(
                    id="lease-1",
                    task_key=task_key,
                    agent_id=agent_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
                ),
            )

        async def send_signal(self, **_kwargs):
            order.append("signal")
            return SimpleNamespace(id="signal-1", thread_id="thread-1")

    async def fake_create_runtime_task_record(**kwargs):
        order.append("enqueue")
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_admit_peer_session(_request, *, state):
        assert state == "queued"
        order.append("session_admission")
        return True

    async def fake_notify_runtime_task_worker(**_kwargs):
        order.append("worker_wake")

    async def fake_update_runtime_task_record(*_args, **_kwargs):
        order.append("admission_commit")
        return True

    async def fake_persist_delegation_event(**_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(
        "app.agents.orchestrator._ensure_peer_delegation_session",
        fake_admit_peer_session,
        raising=False,
    )
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify_runtime_task_worker)

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="")
    owner_id = uuid4()
    parent_agent_id = uuid4()
    handle = await delegate_async(
        target=target,
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "inspect"}],
        owner_id=owner_id,
        session_id="child-session",
        coordination_gateway=Gateway(),
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="child-session",
            parent_agent_id=parent_agent_id,
            parent_session_id="root-session",
        ),
    )

    assert handle.status == "queued"
    assert order == ["enqueue", "session_admission", "lease", "signal", "admission_commit", "worker_wake"]
    assert created["root_item_intent_key"] == f"a2a:{handle.task_id}"
    assert created["root_item_work_type"] == "a2a"
    assert created["root_item_state"] == "queued"


@pytest.mark.asyncio
async def test_coordination_recovery_reuses_existing_signal_after_lease_expiry() -> None:
    from app.agents.coordination import Lease, LeaseAcquireResult, Signal
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        OrchestrationPolicy,
        _ensure_delegation_coordination_published,
    )

    task_id = str(uuid4())
    target = SimpleNamespace(id=uuid4(), name="Target", role_description="")
    parent_agent_id = uuid4()
    existing_signal = Signal(
        id="signal-existing",
        from_agent_id=str(parent_agent_id),
        to_agent_id=str(target.id),
        content="inspect",
        signal_type="delegation_started",
        thread_id="trace-recovery",
        created_at=datetime.now(timezone.utc),
        metadata={"runtime_task_id": task_id},
    )
    sent: list[dict] = []

    class Gateway:
        async def acquire_lease(self, *, task_key, agent_id, ttl_seconds):
            return LeaseAcquireResult(
                acquired=True,
                lease=Lease(
                    id="lease-reacquired",
                    task_key=task_key,
                    agent_id=agent_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
                ),
            )

        async def read_signals(self, agent_id, *, thread_id=None):
            assert agent_id == str(target.id)
            assert thread_id == "trace-recovery"
            return [existing_signal]

        async def send_signal(self, **kwargs):
            sent.append(kwargs)
            raise AssertionError("recovery must reuse the durable signal")

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "inspect"}],
        owner_id=uuid4(),
        session_id="child-recovery",
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-recovery",
        trace_id="trace-recovery",
        policy=OrchestrationPolicy(timeout_seconds=120),
        runtime_task_id=task_id,
    )

    admission = await _ensure_delegation_coordination_published(
        task_id=task_id,
        request=request,
        coordination_gateway=Gateway(),
    )

    assert admission.published is True
    assert admission.lease_id == "lease-reacquired"
    assert admission.signal_id == "signal-existing"
    assert admission.signal_thread_id == "trace-recovery"
    assert sent == []


@pytest.mark.asyncio
async def test_coordination_publish_failure_releases_newly_acquired_lease() -> None:
    from app.agents.coordination import CoordinationRuntime
    from app.agents.coordination_gateway import InProcessCoordinationGateway
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        _delegation_coordination_key,
        _ensure_delegation_coordination_published,
    )

    runtime = CoordinationRuntime()

    class FailingGateway(InProcessCoordinationGateway):
        async def send_signal(self, **_kwargs):
            raise RuntimeError("signal transport unavailable")

    task_id = uuid4().hex
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="Target", role_description=""),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "inspect"}],
        owner_id=uuid4(),
        session_id="child-publish-failure",
        parent_agent_id=uuid4(),
        trace_id="trace-publish-failure",
        runtime_task_id=task_id,
    )
    gateway = FailingGateway(runtime)

    with pytest.raises(RuntimeError, match="signal transport unavailable"):
        await _ensure_delegation_coordination_published(
            task_id=task_id,
            request=request,
            coordination_gateway=gateway,
        )

    assert (
        runtime.acquire_lease(
            task_key=_delegation_coordination_key(request),
            agent_id="runtime-task:retry",
            ttl_seconds=60,
        ).acquired
        is True
    )


@pytest.mark.asyncio
async def test_terminal_coordination_release_records_evidence_and_unblocks_retry(monkeypatch) -> None:
    from app.agents.coordination import CoordinationRuntime
    from app.agents.coordination_gateway import InProcessCoordinationGateway
    from app.agents.orchestrator import _release_delegation_coordination_lease

    runtime = CoordinationRuntime()
    gateway = InProcessCoordinationGateway(runtime)
    task_id = uuid4().hex
    task_key = "delegate:parent:worker:instruction"
    lease = await gateway.acquire_lease(
        task_key=task_key,
        agent_id=f"runtime-task:{task_id}",
        ttl_seconds=60,
    )
    updates: list[dict] = []

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return True

    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)

    assert (
        await _release_delegation_coordination_lease(
            task_id=task_id,
            tenant_id=uuid4(),
            task_key=task_key,
            lease_id=lease.lease.id if lease.lease else None,
            reason="delegation_terminal",
            coordination_gateway=gateway,
        )
        is True
    )
    assert updates[-1]["metadata_json"]["coordination_release_state"] == "released"
    assert (
        await gateway.acquire_lease(
            task_key=task_key,
            agent_id="runtime-task:retry",
            ttl_seconds=60,
        )
    ).acquired is True


@pytest.mark.asyncio
async def test_delegate_async_enqueue_failure_cannot_publish_or_return_queued(monkeypatch):
    from app.agents.orchestrator import delegate_async

    effects: list[str] = []

    class Gateway:
        async def acquire_lease(self, **_kwargs):
            effects.append("lease")
            raise AssertionError("coordination must not run after enqueue failure")

    async def fail_create_runtime_task_record(**_kwargs):
        raise RuntimeError("durable enqueue unavailable")

    async def unexpected_wake(**_kwargs):
        effects.append("worker_wake")

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fail_create_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", unexpected_wake)

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="")
    owner_id = uuid4()
    with pytest.raises(RuntimeError, match="durable enqueue unavailable"):
        await delegate_async(
            target=target,
            target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
            conversation_messages=[{"role": "user", "content": "inspect"}],
            owner_id=owner_id,
            session_id="child-session",
            coordination_gateway=Gateway(),
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="child-session",
                parent_agent_id=uuid4(),
                parent_session_id="root-session",
            ),
        )

    assert effects == []


@pytest.mark.asyncio
async def test_delegate_async_persists_cycle_as_not_admitted_without_side_effects(monkeypatch):
    from app.agents.orchestrator import delegate_async
    from app.core.execution_context import ExecutionPrincipal

    created: dict = {}
    updated: dict = {}
    effects: list[str] = []

    class Gateway:
        async def acquire_lease(self, **_kwargs):
            effects.append("lease")
            raise AssertionError("cycle must stop before coordination")

    async def fake_create_runtime_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        updated.update(kwargs)

    async def unexpected_wake(**_kwargs):
        effects.append("worker_wake")

    async def fake_persist_delegation_event(**_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", fake_persist_delegation_event)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", unexpected_wake)

    parent_agent_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Target", role_description="", tenant_id=uuid4())
    owner_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=target.tenant_id,
        source_agent_id=parent_agent_id,
        requester_user_id=owner_id,
        root_session_id="root-session",
        delegation_chain=(f"agent:{parent_agent_id}", f"agent:{target.id}"),
    )
    handle = await delegate_async(
        target=target,
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "re-enter"}],
        owner_id=owner_id,
        session_id="cycle-session",
        parent_agent_id=parent_agent_id,
        parent_session_id="root-session",
        tenant_id=target.tenant_id,
        execution_principal=principal.to_evidence(),
        coordination_gateway=Gateway(),
    )

    assert handle.status == "cycle_blocked"
    assert created["status"] == "skipped"
    assert created["root_item_state"] == "not_admitted"
    assert created["root_item_admission_disposition"] == "not_admitted"
    assert created["root_item_reason_code"] == "runtime_root_cycle_detected"
    assert updated["status"] == "skipped"
    assert updated["root_item_state"] == "not_admitted"
    assert updated["root_item_reason_code"] == "runtime_root_cycle_detected"
    assert effects == []


@pytest.mark.asyncio
async def test_approved_a2a_task_publishes_coordination_before_worker_execution(monkeypatch):
    from app.agents.orchestrator import (
        _DelegationCoordinationAdmission,
        dispatch_persisted_async_delegation,
    )

    task_id = uuid4().hex
    owner_id = uuid4()
    parent_agent_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Approved Worker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None)
    authority_metadata = _persisted_a2a_authority_metadata(
        task_id=task_id,
        trace_id="trace-approved",
        target=target,
        target_model=model,
        owner_id=owner_id,
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-approved",
        child_session_id="child-approved",
        conversation_messages=[{"role": "user", "content": "continue after approval"}],
        tool_profile="review_readonly",
    )
    order = []

    async def fake_get_runtime_task_record(_task_id):
        return {
            "task_id": task_id,
            "task_type": "delegation",
            "status": "pending",
            "trace_id": "trace-approved",
            "parent_agent_id": str(parent_agent_id),
            "child_agent_id": str(target.id),
            "parent_session_id": "parent-approved",
            "child_session_id": "child-approved",
            "depth": 1,
            "budget_admission_status": "approved",
            "metadata": {
                "owner_id": str(owner_id),
                "target_agent_id": str(target.id),
                "conversation_messages": [{"role": "user", "content": "continue after approval"}],
                "tool_profile": "review_readonly",
                "coordination_publish_state": "pending",
                **authority_metadata,
            },
        }

    async def fake_resolve_target_runtime(_child_agent_id, *, tenant_id):
        assert tenant_id
        return target, model

    async def fake_publish(**_kwargs):
        order.append("coordination")
        return _DelegationCoordinationAdmission(
            published=True,
            lease_id="lease-approved",
            task_key="task-key-approved",
            signal_id="signal-approved",
            signal_thread_id="trace-approved",
        )

    async def fake_update(_task_id, **kwargs):
        order.append("running_commit")
        assert kwargs["status"] == "running"
        assert kwargs["metadata_json"]["coordination_publish_state"] == "published"
        return True

    def fake_spawn(**_kwargs):
        order.append("execute")

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator._ensure_delegation_coordination_published", fake_publish)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", fake_spawn)

    assert await dispatch_persisted_async_delegation(task_id) is True
    assert order == ["coordination", "running_commit", "execute"]


@pytest.mark.asyncio
async def test_unavailable_resumable_target_projects_terminal_parent_state(monkeypatch):
    import app.agents.orchestrator as orchestrator

    task_id = uuid4().hex
    parent_session_id = uuid4()
    child_session_id = uuid4()
    parent_agent_id = uuid4()
    child_agent_id = uuid4()
    owner_id = uuid4()
    tenant_id = uuid4()
    record = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "pending",
        "tenant_id": str(tenant_id),
        "parent_agent_id": str(parent_agent_id),
        "child_agent_id": str(child_agent_id),
        "child_agent_name": "Unavailable Researcher",
        "parent_session_id": str(parent_session_id),
        "child_session_id": str(child_session_id),
        "metadata": {
            "owner_id": str(owner_id),
            "tenant_id": str(tenant_id),
            "target_agent_id": str(child_agent_id),
            "conversation_messages": [{"role": "user", "content": "research"}],
        },
    }
    updates: list[dict] = []
    projections: list[dict] = []
    resolved_tenants: list[UUID] = []

    async def fake_resolve(_target_agent_id, *, tenant_id):
        resolved_tenants.append(tenant_id)
        return None

    async def fake_update(_task_id, **kwargs):
        updates.append(kwargs)
        return True

    async def fake_project(**kwargs):
        projections.append(kwargs)

    monkeypatch.setattr(orchestrator, "_resolve_resumable_target_runtime", fake_resolve)
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(orchestrator, "_project_delegation_record_terminal_to_parent", fake_project, raising=False)

    request = await orchestrator._build_delegation_request_from_runtime_record(record)

    assert request is None
    assert resolved_tenants == [tenant_id]
    assert updates[0]["status"] == "needs_reconciliation"
    assert updates[0]["root_item_reason_code"] == "dispatch_request_unavailable"
    assert updates[0]["metadata_json"]["restart_resume_blocker"] == "dispatch_request_unavailable"
    # The terminal RuntimeTask/outbox is now the sole parent-projection owner;
    # the dispatch failure path must not race it with an immediate projection.
    assert projections == []


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

    target = SimpleNamespace(id=uuid4(), name="Broken Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    owner_id = uuid4()

    async def _unexpected_invoke(_request):
        raise AssertionError("invoke_agent must not run without a delegation token")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _unexpected_invoke)
    monkeypatch.setattr("app.agents.orchestrator._issue_delegation_token_for_request", lambda *_args: None)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=owner_id,
            session_id="bad-token-child",
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="bad-token-child"),
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
    parent_agent_id = uuid4()

    async def fake_invoke_agent(request):
        metadata = request.session_context.metadata
        assert metadata["delegation"] is True
        assert metadata["delegation_depth"] == 1
        assert metadata["delegation_parent_agent_id"] == str(parent_agent_id)
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
            trace_id="trace-123",
            depth=1,
            policy=OrchestrationPolicy(timeout_seconds=0.01),
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="child-session",
                parent_agent_id=parent_agent_id,
                parent_session_id="parent-session",
            ),
        )
    )

    assert result.timed_out is True
    assert result.depth_limited is False
    assert result.trace_id == "trace-123"
    assert result.child_session_id == "child-session"


@pytest.mark.asyncio
async def test_delegate_to_agent_threads_permission_profile_into_child_runtime(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate
    from app.core.execution_context import ExecutionPrincipal
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}
    tenant_id = uuid4()
    owner_id = uuid4()
    parent_agent_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=parent_agent_id,
        requester_user_id=owner_id,
        root_session_id="root-session-permission",
        root_runtime_task_id="root-task-permission",
        delegation_chain=(f"agent:{parent_agent_id}",),
    )

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "hello"}],
            owner_id=owner_id,
            session_id="child-session",
            parent_agent_id=parent_agent_id,
            parent_session_id="parent-session",
            trace_id="trace-permission",
            depth=1,
            policy=OrchestrationPolicy(timeout_seconds=5.0),
            execution_principal=principal.to_evidence(),
            root_runtime_task_id="root-task-permission",
            permission_profile=PermissionProfileV1(
                mode=PermissionMode.BYPASS_PERMISSIONS,
                allowed_tools=("web_search", "feishu_doc_read"),
            ),
        )
    )

    assert result.content == "done"
    metadata = captured["request"].session_context.metadata
    assert metadata["permission_mode"] == "bypassPermissions"
    assert metadata["permission_profile"]["mode"] == "bypassPermissions"
    assert metadata["permission_profile"]["allowed_tools"] == [
        "web_search",
        "feishu_doc_read",
    ]
    assert metadata["delegation_parent_session_id"] == "parent-session"
    assert metadata["a2a_authority_frame_schema"] == "hive.a2a_tool_authority_frame.v1"
    assert metadata["a2a_authority_required"] is True
    assert len(metadata["a2a_authority_snapshot_hash"]) == 64
    assert len(metadata["a2a_authority_policy_hash"]) == 64
    child_principal = ExecutionPrincipal.from_evidence(metadata["execution_principal"])
    assert child_principal is not None
    assert child_principal.tenant_id == tenant_id
    assert child_principal.source_agent_id == target.id
    assert child_principal.requester_user_id == owner_id
    assert child_principal.root_session_id == "root-session-permission"
    assert child_principal.root_runtime_task_id == "root-task-permission"
    assert child_principal.delegation_chain[-1] == f"agent:{target.id}"


@pytest.mark.asyncio
async def test_delegate_to_agent_supports_memory_readonly_profile(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Target", role_description="Helpful")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}
    owner_id = uuid4()

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "search past memory and summarize it"}],
            owner_id=owner_id,
            session_id="memory-child",
            policy=OrchestrationPolicy(tool_profile="memory_readonly"),
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="memory-child"),
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
    owner_id = uuid4()

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="reviewed")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "review these files and identify risks"}],
            owner_id=owner_id,
            session_id="review-child",
            policy=OrchestrationPolicy(tool_profile="review_readonly"),
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="review-child"),
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
    owner_id = uuid4()

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="researched")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "research the latest market movement"}],
            owner_id=owner_id,
            session_id="research-child",
            policy=OrchestrationPolicy(tool_profile="research_readonly"),
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="research-child"),
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
        "advanced_web_search",
        "advanced_web_fetch",
        "anysearch_get_sub_domains",
        "anysearch_search",
        "anysearch_batch_search",
        "anysearch_extract",
        "exa_search",
        "exa_fetch",
        "tavily_search",
        "tavily_extract",
        "firecrawl_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
    )
    assert request.session_context.metadata["delegation_tool_policy"] == "worker_research_readonly"
    assert request.session_context.metadata["delegation_memory_policy"] == "read_only_long_term_memory"
    assert "You MAY browse and retrieve external sources" in request.system_prompt_suffix


@pytest.mark.asyncio
async def test_agent_message_profile_inherits_target_tools_and_governed_memory(monkeypatch):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Feishu Knowledge", role_description="Knowledge assistant")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}
    emitted: list[object] = []
    owner_id = uuid4()
    parent_agent_id = uuid4()

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="looked up")

    async def fake_emit_hook(event, **_kwargs):
        emitted.append(event)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(runtime_hooks, "emit_hook", fake_emit_hook)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "请查飞书知识库里的灵巧手报告"}],
            owner_id=owner_id,
            session_id="agent-message-child",
            interaction_type="agent_message",
            policy=OrchestrationPolicy(timeout_seconds=120, tool_profile="agent_message"),
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="agent-message-child",
                parent_agent_id=parent_agent_id,
                parent_session_id="parent-session",
            ),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert request.core_tools_only is False
    assert request.allowed_tool_names == ()
    assert request.excluded_tool_names == ()
    assert request.delegation_token is None
    assert request.session_context.metadata["agent_message_tool_policy"] == "peer_agent_tool_surface"
    assert request.session_context.metadata["agent_message_memory_policy"] == "peer_governed_memory"
    assert "peer agent request" in request.system_prompt_suffix
    assert runtime_hooks.HookEvent.DELEGATION_END not in emitted
    assert runtime_hooks.HookEvent.RESPONSE_COMPLETE not in emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_error", [None, RuntimeError("transcript commit failed")])
async def test_sync_delegation_end_follows_committed_transcript_once(monkeypatch, commit_error):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, _delegate

    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Sync Worker", role_description="", tenant_id=uuid4())
    order: list[str] = []
    emitted: list[object] = []

    class _TranscriptSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            order.append("transcript_commit")
            if commit_error is not None:
                raise commit_error

    async def fake_invoke_agent(_request):
        return SimpleNamespace(content="done")

    async def fake_ensure_session(_request, *, state):
        assert state == "running"
        return True

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(
            message_id=None,
            transcript_event=SimpleNamespace(parts_json=None, metadata_json={}),
        )

    async def fake_emit_hook(event, **_kwargs):
        emitted.append(event)
        order.append(f"hook:{getattr(event, 'value', event)}")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.agents.orchestrator._ensure_peer_delegation_session", fake_ensure_session)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _TranscriptSession())
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime_hooks, "emit_hook", fake_emit_hook)

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "work"}],
        owner_id=owner_id,
        session_id=session_id,
        tenant_id=target.tenant_id,
        **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
    )
    if commit_error is not None:
        with pytest.raises(RuntimeError, match="transcript commit failed"):
            await _delegate(request)
        assert runtime_hooks.HookEvent.DELEGATION_END not in emitted
    else:
        result = await _delegate(request)
        assert result.transcript_committed is True
        assert emitted.count(runtime_hooks.HookEvent.DELEGATION_END) == 1
        assert order.index("transcript_commit") < order.index("hook:delegation_end")
    assert runtime_hooks.HookEvent.RESPONSE_COMPLETE not in emitted


@pytest.mark.asyncio
async def test_sync_delegation_without_committed_transcript_emits_no_terminal_hook(monkeypatch):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, _delegate

    target = SimpleNamespace(id=uuid4(), name="No Transcript Worker", role_description="")
    owner_id = uuid4()
    emitted: list[object] = []

    async def fake_invoke_agent(_request):
        return SimpleNamespace(content="done")

    async def fake_emit_hook(event, **_kwargs):
        emitted.append(event)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(runtime_hooks, "emit_hook", fake_emit_hook)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=SimpleNamespace(),
            conversation_messages=[{"role": "user", "content": "work"}],
            owner_id=owner_id,
            session_id="not-a-uuid",
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="not-a-uuid"),
        )
    )

    assert result.failed is False
    assert runtime_hooks.HookEvent.DELEGATION_START in emitted
    assert runtime_hooks.HookEvent.DELEGATION_END not in emitted
    assert runtime_hooks.HookEvent.RESPONSE_COMPLETE not in emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_outcome", [True, False, RuntimeError("terminal write failed")])
async def test_async_delegation_terminal_learning_waits_for_committed_outbox(monkeypatch, terminal_outcome):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, _async_tasks, _spawn_async_delegation_task
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    task_id = uuid4().hex
    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Async Worker", role_description="", tenant_id=uuid4())
    order: list[str] = []
    emitted: list[object] = []
    terminal_updates: list[dict] = []

    class _TranscriptSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            order.append("transcript_commit")

        async def scalar(self, _statement):
            return SimpleNamespace(status="running")

    async def fake_invoke_agent(_request):
        return SimpleNamespace(
            content="done",
            terminal_reason="turn_stop",
            response_complete_payload={
                "agent_id": target.id,
                "session_id": session_id,
                "messages": [{"role": "user", "content": "work"}],
                "source": "agent",
                "metadata": {
                    "tenant_id": str(target.tenant_id),
                    "final_response": "done",
                },
            },
        )

    async def fake_ensure_session(_request, *, state):
        assert state == "running"
        return True

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(
            message_id=None,
            transcript_event=SimpleNamespace(parts_json=None, metadata_json={}),
        )

    async def fake_emit_hook(event, **_kwargs):
        emitted.append(event)
        order.append(f"hook:{getattr(event, 'value', event)}")

    async def fake_update_runtime_task_record(_task_id, **fields):
        assert fields["status"] == "completed"
        terminal_updates.append(fields)
        order.append("runtime_task_terminal")
        if isinstance(terminal_outcome, Exception):
            raise terminal_outcome
        return terminal_outcome

    async def fake_terminal_evidence(**_kwargs):
        return {"status": "completed"}

    async def noop_async(*_args, **_kwargs):
        return None

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "work"}],
        owner_id=owner_id,
        session_id=session_id,
        runtime_task_id=task_id,
        trace_id="trace-terminal-order",
        tenant_id=target.tenant_id,
        **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
    )

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.agents.orchestrator._ensure_peer_delegation_session", fake_ensure_session)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _TranscriptSession())
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime_hooks, "emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_terminal_evidence", fake_terminal_evidence)
    monkeypatch.setattr("app.agents.orchestrator._settle_delegation_budget", noop_async)
    monkeypatch.setattr("app.agents.orchestrator._project_delegation_completion_to_parent", noop_async)
    monkeypatch.setattr("app.agents.orchestrator._release_delegation_coordination_lease", noop_async)

    fence_token = set_runtime_task_fence(task_id=task_id, claim_version=1, worker_id="worker-terminal-order")
    try:
        _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-terminal-order")
    finally:
        reset_runtime_task_fence(fence_token)
    state = _async_tasks[task_id]
    await state.task
    _async_tasks.pop(task_id, None)

    assert len(terminal_updates) == 1
    assert terminal_updates[0]["metadata_json"]["terminal_reason"] == "turn_stop"
    assert terminal_updates[0]["metadata_json"]["response_complete_payload"]["metadata"]["final_response"] == "done"
    if terminal_outcome is True:
        assert "runtime_task_terminal" in order
    assert runtime_hooks.HookEvent.DELEGATION_END not in emitted
    assert runtime_hooks.HookEvent.RESPONSE_COMPLETE not in emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_authority", ["killed", "reclaimed", "expired"])
async def test_async_delegation_final_transcript_drops_terminal_reclaimed_or_expired_claim(
    monkeypatch,
    lost_authority,
):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, _delegate
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    task_id = uuid4().hex
    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Cancelled Worker", role_description="", tenant_id=uuid4())
    statements: list[str] = []
    appended: list[dict] = []

    class _CancelledTranscriptSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def scalar(self, statement):
            rendered = str(statement)
            statements.append(rendered)
            assert "runtime_tasks.status =" in rendered
            assert "runtime_tasks.claim_version =" in rendered
            assert "runtime_tasks.claimed_by =" in rendered
            assert "runtime_tasks.claim_expires_at >" in rendered
            return None

        async def commit(self):
            raise AssertionError("a fenced assistant event must not commit")

    async def fake_invoke_agent(_request):
        return SimpleNamespace(
            content="late model output",
            terminal_reason="turn_stop",
            response_complete_payload={"metadata": {"final_response": "late model output"}},
        )

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        raise AssertionError("a killed RuntimeTask must reject late assistant bytes")

    async def fake_ensure_session(*_args, **_kwargs):
        return True

    async def noop_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.agents.orchestrator._ensure_peer_delegation_session", fake_ensure_session)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _CancelledTranscriptSession())
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime_hooks, "emit_hook", noop_hook)

    fence_token = set_runtime_task_fence(task_id=task_id, claim_version=4, worker_id="worker-cancel-fence")
    try:
        result = await _delegate(
            AgentDelegationRequest(
                target=target,
                target_model=SimpleNamespace(),
                conversation_messages=[{"role": "user", "content": "work"}],
                owner_id=owner_id,
                session_id=session_id,
                runtime_task_id=task_id,
                trace_id=f"trace-{lost_authority}-fence",
                tenant_id=target.tenant_id,
                **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
            )
        )
    finally:
        reset_runtime_task_fence(fence_token)

    assert result.transcript_committed is False
    assert appended == []
    assert len(statements) == 1
    assert "runtime_tasks.status" in statements[0]
    assert "FOR UPDATE" in statements[0]


@pytest.mark.asyncio
async def test_async_delegation_final_transcript_binds_current_worker_claim(monkeypatch):
    import app.runtime.hooks as runtime_hooks
    from app.agents.orchestrator import AgentDelegationRequest, _delegate
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    task_id = uuid4()
    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Claimed Worker", role_description="", tenant_id=uuid4())
    statements: list[str] = []
    appended: list[dict] = []

    class _ClaimedTranscriptSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def scalar(self, statement):
            rendered = str(statement)
            statements.append(rendered)
            assert "runtime_tasks.claim_version =" in rendered
            assert "runtime_tasks.claimed_by =" in rendered
            assert "runtime_tasks.claim_expires_at >" in rendered
            return SimpleNamespace(status="running")

        async def commit(self):
            return None

    async def fake_invoke_agent(_request):
        return SimpleNamespace(
            content="claimed output",
            terminal_reason="turn_stop",
            response_complete_payload={"metadata": {"final_response": "claimed output"}},
        )

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(
            message_id=None,
            transcript_event=SimpleNamespace(parts_json=None, metadata_json={}),
        )

    async def fake_ensure_session(*_args, **_kwargs):
        return True

    async def noop_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.agents.orchestrator._ensure_peer_delegation_session", fake_ensure_session)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _ClaimedTranscriptSession())
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime_hooks, "emit_hook", noop_hook)

    fence_token = set_runtime_task_fence(task_id=task_id, claim_version=7, worker_id="worker-a")
    try:
        result = await _delegate(
            AgentDelegationRequest(
                target=target,
                target_model=SimpleNamespace(),
                conversation_messages=[{"role": "user", "content": "work"}],
                owner_id=owner_id,
                session_id=session_id,
                runtime_task_id=task_id.hex,
                trace_id="trace-current-claim",
                tenant_id=target.tenant_id,
                **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
            )
        )
    finally:
        reset_runtime_task_fence(fence_token)

    assert result.transcript_committed is True
    assert len(statements) == 1
    assert len(appended) == 1


@pytest.mark.asyncio
async def test_async_delegation_fenced_terminal_does_not_persist_late_model_output(monkeypatch):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        AgentDelegationResult,
        _async_tasks,
        _spawn_async_delegation_task,
    )

    task_id = uuid4().hex
    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Cancelled Worker", role_description="", tenant_id=uuid4())
    updates: list[dict] = []
    evidence: list[dict] = []
    projected: list[dict] = []
    settlements: list[str] = []

    async def fake_delegate(_request):
        return AgentDelegationResult(
            content="late model output",
            child_session_id=session_id,
            trace_id="trace-cancel-fence",
            depth=1,
            terminal_reason="turn_stop",
            response_complete_payload={"metadata": {"final_response": "late model output"}},
            transcript_committed=False,
        )

    async def fake_terminal_evidence(**kwargs):
        evidence.append(kwargs)
        return {"status": kwargs["status"]}

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return False

    async def fake_project(**kwargs):
        projected.append(kwargs)

    async def fake_settle(*_args, status, **_kwargs):
        settlements.append(status)

    async def noop_async(*_args, **_kwargs):
        return None

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "work"}],
        owner_id=owner_id,
        session_id=session_id,
        runtime_task_id=task_id,
        trace_id="trace-cancel-fence",
        tenant_id=target.tenant_id,
        **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
    )
    monkeypatch.setattr("app.agents.orchestrator._delegate", fake_delegate)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_terminal_evidence", fake_terminal_evidence)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.agents.orchestrator._settle_delegation_budget", fake_settle)
    monkeypatch.setattr("app.agents.orchestrator._project_delegation_completion_to_parent", fake_project)
    monkeypatch.setattr("app.agents.orchestrator._release_delegation_coordination_lease", noop_async)

    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-cancel-fence")
    result = await _async_tasks[task_id].task
    _async_tasks.pop(task_id, None)

    assert result.failed is True
    assert "late model output" not in result.content
    assert result.response_complete_payload is None
    assert evidence == []
    assert settlements == []
    assert updates[0]["status"] == "needs_reconciliation"
    assert "late model output" not in updates[0]["result_summary"]
    assert updates[0]["metadata_json"]["response_projection_error"] == "transcript_commit_fenced"
    assert projected == []


@pytest.mark.asyncio
async def test_async_delegation_completion_lost_to_kill_cas_drops_completed_evidence(monkeypatch):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        AgentDelegationResult,
        _async_tasks,
        _spawn_async_delegation_task,
    )

    task_id = uuid4().hex
    session_id = uuid4().hex
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Cancelled Worker", role_description="", tenant_id=uuid4())
    evidence: list[dict] = []
    settlements: list[str] = []
    projected: list[dict] = []

    async def fake_delegate(_request):
        return AgentDelegationResult(
            content="late completed output",
            child_session_id=session_id,
            trace_id="trace-kill-won",
            depth=1,
            terminal_reason="turn_stop",
            response_complete_payload={"metadata": {"final_response": "late completed output"}},
            transcript_committed=True,
        )

    async def fake_terminal_evidence(**kwargs):
        evidence.append(kwargs)
        return {"status": kwargs["status"]}

    async def fake_update(_task_id, **_fields):
        return False

    async def fake_settle(*_args, status, **_kwargs):
        settlements.append(status)

    async def fake_project(**kwargs):
        projected.append(kwargs)

    async def noop_async(*_args, **_kwargs):
        return None

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "work"}],
        owner_id=owner_id,
        session_id=session_id,
        runtime_task_id=task_id,
        trace_id="trace-kill-won",
        tenant_id=target.tenant_id,
        **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id=session_id),
    )
    monkeypatch.setattr("app.agents.orchestrator._delegate", fake_delegate)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_terminal_evidence", fake_terminal_evidence)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.agents.orchestrator._settle_delegation_budget", fake_settle)
    monkeypatch.setattr("app.agents.orchestrator._project_delegation_completion_to_parent", fake_project)
    monkeypatch.setattr("app.agents.orchestrator._release_delegation_coordination_lease", noop_async)

    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-kill-won")
    result = await _async_tasks[task_id].task
    state_receipt = _async_tasks[task_id].receipt
    _async_tasks.pop(task_id, None)

    assert result.failed is True
    assert "late completed output" not in result.content
    assert result.response_complete_payload is None
    assert evidence == []
    assert settlements == []
    assert projected == []
    assert state_receipt["status"] == "pending"


@pytest.mark.asyncio
async def test_peer_agent_delegation_profile_inherits_capability_token_scope(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    target = SimpleNamespace(id=uuid4(), name="Feishu Knowledge", role_description="Knowledge assistant")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")
    captured = {}
    owner_id = uuid4()
    parent_agent_id = uuid4()

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="looked up")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

    result = await _delegate(
        AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=[{"role": "user", "content": "请查飞书知识库里的报告"}],
            owner_id=owner_id,
            session_id="peer-child",
            interaction_type="delegation",
            policy=OrchestrationPolicy(timeout_seconds=120, tool_profile="agent_message"),
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="peer-child",
                parent_agent_id=parent_agent_id,
                parent_session_id="parent-session",
            ),
        )
    )

    request = captured["request"]
    assert result.failed is False
    assert request.core_tools_only is False
    assert request.allowed_tool_names == ()
    assert request.delegation_token is not None
    assert request.delegation_token.inherit_parent_capabilities is True
    assert request.session_context.metadata["delegation_tool_policy"] == "peer_agent_tool_surface"
    assert request.session_context.metadata["delegation_memory_policy"] == "peer_governed_memory"


@pytest.mark.asyncio
async def test_nested_a2a_cycle_is_blocked_on_the_shared_trace(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate

    agent_a = SimpleNamespace(id=uuid4(), name="A", role_description="A")
    agent_b = SimpleNamespace(id=uuid4(), name="B", role_description="B")
    model = SimpleNamespace(provider="openai", model="gpt-4.1")
    nested = {}
    owner_id = uuid4()
    tenant_id = uuid4()
    agent_a.tenant_id = tenant_id
    agent_b.tenant_id = tenant_id

    async def fake_invoke_agent(_request):
        nested["result"] = await _delegate(
            AgentDelegationRequest(
                target=agent_a,
                target_model=model,
                conversation_messages=[{"role": "user", "content": "back to A"}],
                owner_id=owner_id,
                session_id="nested",
                trace_id="shared-a2a-trace",
                depth=2,
                policy=OrchestrationPolicy(max_depth=3, tool_profile="agent_message"),
                interaction_type="agent_message",
                **_a2a_authority_kwargs(
                    target=agent_a,
                    owner_id=owner_id,
                    session_id="nested",
                    parent_agent_id=agent_b.id,
                    parent_session_id="root",
                ),
            )
        )
        return SimpleNamespace(content=nested["result"].content)

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)
    await _delegate(
        AgentDelegationRequest(
            target=agent_b,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "ask B"}],
            owner_id=owner_id,
            session_id="root",
            trace_id="shared-a2a-trace",
            depth=1,
            policy=OrchestrationPolicy(max_depth=3, tool_profile="agent_message"),
            interaction_type="agent_message",
            **_a2a_authority_kwargs(
                target=agent_b,
                owner_id=owner_id,
                session_id="root",
                parent_agent_id=agent_a.id,
                parent_session_id="root",
            ),
        )
    )

    assert nested["result"].failed is True
    assert "cycle" in nested["result"].content.lower()


@pytest.mark.asyncio
async def test_delegate_async_returns_handle_immediately(monkeypatch):
    from app.agents.orchestrator import delegate_async

    created: dict = {}

    async def fake_create_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_update_runtime_task_record(*args, **kwargs):
        return True

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
        session_id="sess-1",
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="sess-1",
            parent_agent_id=parent_agent_id,
        ),
    )

    assert handle.task_id
    assert handle.target_name == "Worker"
    assert handle.status == "queued"
    assert created["task_type"] == "delegation"
    assert created["status"] == "suspended"


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
        max_tool_rounds=11,
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="sess-restart",
            parent_agent_id=parent_agent_id,
            parent_session_id="parent-session",
        ),
    )

    assert handle.task_id
    assert created["status"] == "suspended"
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
        max_tool_rounds=11,
        policy=OrchestrationPolicy(tool_profile="review_readonly"),
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id="sess-restart",
            parent_agent_id=parent_agent_id,
            parent_session_id="parent-session",
        ),
    )

    assert handle.task_id
    assert created["status"] == "suspended"
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
@pytest.mark.parametrize(
    ("status", "claim_expires_at", "expected_reason"),
    [
        ("pending", None, "delegation_restart_pending"),
        (
            "running",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            "delegation_lease_reclaimable",
        ),
    ],
)
async def test_resume_persisted_delegations_notifies_worker_without_direct_spawn(
    monkeypatch,
    status,
    claim_expires_at,
    expected_reason,
):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    notifications = []

    async def fake_list_active_runtime_task_records(*_args, **_kwargs):
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": status,
                "claimed_by": "dead-worker" if status == "running" else None,
                "claim_expires_at": claim_expires_at.isoformat() if claim_expires_at else None,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "tool_profile": "review_readonly",
                },
            }
        ]

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id):
        notifications.append((reason, str(runtime_task_id)))

    async def forbidden_build(_record):  # pragma: no cover - must not run
        raise AssertionError("startup must leave pending/reclaimable delegation hydration to the claimed worker")

    def forbidden_spawn(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("startup must never spawn a delegation outside run_claimed_runtime_task")

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records",
        fake_list_active_runtime_task_records,
    )
    monkeypatch.setattr("app.agents.orchestrator._build_delegation_request_from_runtime_record", forbidden_build)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", forbidden_spawn)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        assert await resume_persisted_async_delegations() == [task_id]
        assert task_id not in _async_tasks
        assert notifications == [(expected_reason, str(UUID(task_id)))]
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_active_running_delegation_does_not_duplicate_dispatch(monkeypatch):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    notifications = []

    async def fake_list_active_runtime_task_records(*_args, **_kwargs):
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": "running",
                "claimed_by": "live-worker",
                "claim_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "tool_profile": "review_readonly",
                },
            }
        ]

    async def fake_notify_runtime_task_worker(**kwargs):
        notifications.append(kwargs)

    async def forbidden_build(_record):  # pragma: no cover - must not run
        raise AssertionError("an active running lease already has an owning worker")

    def forbidden_spawn(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("startup must not duplicate an active delegation")

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records",
        fake_list_active_runtime_task_records,
    )
    monkeypatch.setattr("app.agents.orchestrator._build_delegation_request_from_runtime_record", forbidden_build)
    monkeypatch.setattr("app.agents.orchestrator._spawn_async_delegation_task", forbidden_spawn)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        assert await resume_persisted_async_delegations() == [task_id]
        assert task_id not in _async_tasks
        assert notifications == []
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_delegations_scopes_scan_and_rejects_cross_type_record(monkeypatch):
    from app.agents.orchestrator import resume_persisted_async_delegations

    async def fake_list_active_runtime_task_records(*, limit, statuses, task_types):
        assert limit == 50
        assert statuses == ("pending", "running", "suspended")
        assert task_types == ("delegation",)
        return [
            {
                "task_id": uuid4().hex,
                "task_type": "subagent",
                "status": "pending",
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                },
            }
        ]

    async def forbidden_notify(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("cross-type record must not reach the delegation worker wakeup")

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records",
        fake_list_active_runtime_task_records,
    )
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        forbidden_notify,
    )

    assert await resume_persisted_async_delegations() == []


@pytest.mark.asyncio
async def test_dispatch_reclaimed_mutating_delegation_requires_reconciliation_before_hydration(monkeypatch):
    from app.agents.orchestrator import dispatch_persisted_async_delegation

    task_id = uuid4().hex
    record = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "running",
        "trace_id": "trace-reclaimed-mutating",
        "metadata": {
            "resume_after_restart": True,
            "resumable_delegation": True,
            "tool_profile": "worker_safe",
            "side_effect_risk": "mutating",
            "reclaimed_expired_claim": True,
            "restart_replay_contract": {
                "schema": "runtime_restart_replay_contract.v1",
                "idempotency_key": f"delegation:{task_id}:restart",
                "task_type": "delegation",
                "task_id": task_id,
            },
        },
    }
    updates = []
    releases = []

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return record

    async def forbidden_build(_record):  # pragma: no cover - must not run
        raise AssertionError("expired mutating work must be reconciled before runtime hydration or replay")

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        updates.append((_task_id, kwargs))
        return True

    async def fake_release(_record, *, reason):
        releases.append((_record, reason))

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._build_delegation_request_from_runtime_record", forbidden_build)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.agents.orchestrator._release_delegation_coordination_lease_from_record",
        fake_release,
    )

    assert await dispatch_persisted_async_delegation(task_id) is False
    assert updates[-1][0] == task_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["side_effect_risk"] == "mutating"
    assert releases == [(record, "restart_replay_not_safe")]


@pytest.mark.asyncio
async def test_dispatch_reclaimed_readonly_delegation_requires_exact_restart_contract(monkeypatch):
    from app.agents.orchestrator import dispatch_persisted_async_delegation

    task_id = uuid4().hex
    record = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "running",
        "trace_id": "trace-reclaimed-missing-contract",
        "metadata": {
            "resume_after_restart": True,
            "resumable_delegation": True,
            "tool_profile": "review_readonly",
            "side_effect_risk": "read_only",
            "reclaimed_expired_claim": True,
        },
    }
    updates = []
    releases = []

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return record

    async def forbidden_build(_record):  # pragma: no cover - must not run
        raise AssertionError("reclaimed work without an exact restart contract must not hydrate")

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        updates.append((_task_id, kwargs))
        return True

    async def fake_release(_record, *, reason):
        releases.append((_record, reason))

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._build_delegation_request_from_runtime_record", forbidden_build)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.agents.orchestrator._release_delegation_coordination_lease_from_record",
        fake_release,
    )

    assert await dispatch_persisted_async_delegation(task_id) is False
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "restart_replay_contract_missing"
    assert releases == [(record, "restart_replay_not_safe")]


@pytest.mark.asyncio
async def test_dispatch_claimed_delegation_with_incomplete_request_is_typed_reconciliation(monkeypatch):
    from app.agents.orchestrator import dispatch_persisted_async_delegation

    task_id = uuid4().hex
    record = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "running",
        "trace_id": "trace-incomplete-request",
        "metadata": {
            "resume_after_restart": True,
            "resumable_delegation": True,
            "tool_profile": "review_readonly",
            "conversation_messages": [{"role": "user", "content": "missing durable identity"}],
        },
    }
    updates = []
    releases = []

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return record

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        updates.append((_task_id, kwargs))
        return True

    async def fake_release(_record, *, reason):
        releases.append((_record, reason))

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.agents.orchestrator._release_delegation_coordination_lease_from_record",
        fake_release,
    )

    assert await dispatch_persisted_async_delegation(task_id) is False
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "dispatch_request_unavailable"
    assert releases == [(record, "dispatch_request_unavailable")]


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_rehydrates_tasks(monkeypatch):
    from app.agents.orchestrator import (
        _async_tasks,
        resume_persisted_async_delegations,
    )

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    owner_id = uuid4()
    delegated_user_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    updates: list[tuple[str, dict]] = []
    notifications: list[tuple[str, str]] = []
    execution_identity_metadata = {
        "identity_type": "delegated_user",
        "identity_id": str(delegated_user_id),
        "label": "User via recovered session",
    }
    authority_metadata = _persisted_a2a_authority_metadata(
        task_id=task_id,
        trace_id="trace-resume",
        target=target,
        target_model=model,
        owner_id=owner_id,
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session",
        child_session_id="child-session",
        conversation_messages=[{"role": "user", "content": "resume me"}],
        tool_profile="review_readonly",
        max_tool_rounds=9,
        execution_identity_metadata=execution_identity_metadata,
    )

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert "pending" in statuses
        assert "running" in statuses
        assert task_types == ("delegation",)
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": "pending",
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
                    "execution_identity": execution_identity_metadata,
                    **authority_metadata,
                },
            }
        ]

    async def fake_resolve_target_runtime(child_agent_id, *, tenant_id):
        assert child_agent_id == target.id
        assert tenant_id == target.tenant_id
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

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id):
        notifications.append((reason, str(runtime_task_id)))

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == [task_id]
        assert task_id not in _async_tasks
        assert updates == []
        assert notifications == [("delegation_restart_pending", str(UUID(task_id)))]
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_refuses_mutating_profile_without_replay_contract(monkeypatch):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    updates: list[tuple[str, dict]] = []
    notifications: list[tuple[str, str]] = []

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert task_types == ("delegation",)
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": "pending",
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

    async def fake_resolve_target_runtime(_child_agent_id, *, tenant_id):  # pragma: no cover - must not run
        raise AssertionError("non replay-safe delegation must not resolve/replay the child runtime")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id):
        notifications.append((reason, str(runtime_task_id)))

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == [task_id]
        assert task_id not in _async_tasks
        assert updates == []
        assert notifications == [("delegation_restart_pending", str(UUID(task_id)))]
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
    notifications: list[tuple[str, str]] = []

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert task_types == ("delegation",)
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": "running",
                "claimed_by": "dead-worker",
                "claim_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
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

    async def fake_resolve_target_runtime(_child_agent_id, *, tenant_id):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation must not resolve/replay the child runtime")

    async def fake_invoke(_invocation):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation must not invoke from spawn intent")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id):
        notifications.append((reason, str(runtime_task_id)))

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == [task_id]
        assert task_id not in _async_tasks
        assert updates == []
        assert notifications == [("delegation_lease_reclaimable", str(UUID(task_id)))]
    finally:
        _async_tasks.clear()


@pytest.mark.asyncio
async def test_resume_persisted_async_delegations_refuses_mutating_contract_without_replay_journal(monkeypatch):
    from app.agents.orchestrator import _async_tasks, resume_persisted_async_delegations

    task_id = uuid4().hex
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    updates: list[tuple[str, dict]] = []
    notifications: list[tuple[str, str]] = []

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert task_types == ("delegation",)
        return [
            {
                "task_id": task_id,
                "task_type": "delegation",
                "status": "pending",
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

    async def fake_resolve_target_runtime(_child_agent_id, *, tenant_id):  # pragma: no cover - must not run
        raise AssertionError("mutating delegation without replay journal must not be replayed")

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    async def fake_notify_runtime_task_worker(*, reason, runtime_task_id):
        notifications.append((reason, str(runtime_task_id)))

    monkeypatch.setattr(
        "app.agents.orchestrator.list_active_runtime_task_records", fake_list_active_runtime_task_records
    )
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(
        "app.services.runtime_task_worker.notify_runtime_task_worker",
        fake_notify_runtime_task_worker,
    )

    _async_tasks.clear()
    try:
        resumed = await resume_persisted_async_delegations()
        assert resumed == [task_id]
        assert task_id not in _async_tasks
        assert updates == []
        assert notifications == [("delegation_restart_pending", str(UUID(task_id)))]
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
    owner_id = uuid4()
    parent_agent_id = uuid4()

    pending = asyncio.create_task(
        delegate_async(
            target=target,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "do research"}],
            owner_id=owner_id,
            session_id="sess-wait-log",
            **_a2a_authority_kwargs(
                target=target,
                owner_id=owner_id,
                session_id="sess-wait-log",
                parent_agent_id=parent_agent_id,
            ),
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

    async def fake_get_runtime_task_record(task_id):
        assert task_id == handle.task_id
        return {
            "task_id": task_id,
            "status": "pending",
            "parent_agent_id": str(owner_id),
            "child_session_id": "sess-kill",
            "parent_session_id": None,
        }

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)
    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)

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

    async def fake_create_task(**kwargs):
        return kwargs["task_id"]

    async def fake_update_task(*args, **kwargs):
        return True

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.create_runtime_task_record", fake_create_task)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)

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

    async def fake_get_runtime_task_record(task_id):
        assert task_id == handle.task_id
        return {
            "task_id": task_id,
            "status": "pending",
            "parent_agent_id": str(owner_a),
            "child_session_id": "sess-kill-owner",
        }

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)

    status = await cancel_async_delegation(handle.task_id, parent_agent_id=uuid4())
    assert status["status"] == "forbidden"

    never_finish.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["skipped", "needs_reconciliation"])
async def test_db_only_cancel_returns_all_canonical_terminal_statuses(monkeypatch, terminal_status):
    from app.agents.orchestrator import cancel_async_delegation

    task_id = uuid4().hex
    parent_agent_id = uuid4()

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return {
            "task_id": task_id,
            "status": terminal_status,
            "result": f"durable {terminal_status}",
            "parent_agent_id": str(parent_agent_id),
            "child_session_id": str(uuid4()),
            "metadata": {"execution_receipt": {"status": terminal_status}},
        }

    async def forbidden_update(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("canonical terminal RuntimeTask state must not be cancelled again")

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", forbidden_update)

    result = await cancel_async_delegation(task_id, parent_agent_id=parent_agent_id, force=True)

    assert result["status"] == terminal_status
    assert result["result"] == f"durable {terminal_status}"
    assert result["receipt"] == {"status": terminal_status}


@pytest.mark.asyncio
async def test_db_only_cancel_lost_cas_returns_canonical_terminal_state(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    reads = 0
    published: list[dict] = []
    settled: list[tuple[str, str]] = []

    async def fake_get_runtime_task_record(_task_id):
        nonlocal reads
        assert _task_id == task_id
        reads += 1
        if reads == 1:
            return {
                "task_id": task_id,
                "status": "running",
                "parent_agent_id": str(parent_agent_id),
                "child_session_id": "child-cancel-race",
                "metadata": {"execution_receipt": {"status": "pending"}},
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "result": "durable completed result",
            "parent_agent_id": str(parent_agent_id),
            "child_session_id": "child-cancel-race",
            "metadata": {"execution_receipt": {"status": "completed"}},
        }

    async def fake_update(*_args, **_kwargs):
        return False

    async def fake_publish(**kwargs):
        published.append(kwargs)

    async def fake_settle(record, *, task_id, status):
        settled.append((task_id, status))

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.services.runtime_control_bus.publish_delegation_cancel", fake_publish)
    monkeypatch.setattr(
        "app.agents.orchestrator._settle_persisted_delegation_budget",
        fake_settle,
        raising=False,
    )

    result = await cancel_async_delegation(task_id, parent_agent_id=parent_agent_id, force=True)

    assert result["status"] == "completed"
    assert result["result"] == "durable completed result"
    assert result["receipt"] == {"status": "completed"}
    assert published == []
    assert settled == []


@pytest.mark.asyncio
async def test_db_only_cancel_committed_cas_settles_budget_once(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    persisted = {
        "task_id": task_id,
        "status": "running",
        "parent_agent_id": str(parent_agent_id),
        "child_agent_id": str(uuid4()),
        "child_agent_name": "Remote Worker",
        "child_session_id": "child-db-only-cancel",
        "budget_run_id": str(uuid4()),
        "metadata": {"execution_receipt": {"status": "pending"}},
    }
    published: list[dict] = []
    settled: list[tuple[str, str]] = []
    updates: list[dict] = []

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return persisted

    async def fake_update(*_args, **kwargs):
        updates.append(kwargs)
        return True

    async def fake_publish(**kwargs):
        published.append(kwargs)

    async def fake_settle(record, *, task_id, status):
        assert record is persisted
        settled.append((task_id, status))

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.services.runtime_control_bus.publish_delegation_cancel", fake_publish)
    monkeypatch.setattr(
        "app.agents.orchestrator._settle_persisted_delegation_budget",
        fake_settle,
        raising=False,
    )

    result = await cancel_async_delegation(task_id, parent_agent_id=parent_agent_id, force=True)

    assert result["status"] == "killed"
    assert result["receipt"] is None
    assert updates[0]["metadata_json"]["execution_receipt"] is None
    assert updates[0]["metadata_json"]["execution_receipt_error"] == "terminal_receipt_unavailable"
    assert settled == [(task_id, "killed")]
    assert published == [{"task_id": task_id, "parent_agent_id": parent_agent_id}]


@pytest.mark.asyncio
async def test_db_only_cancel_commits_and_returns_killed_execution_receipt(monkeypatch):
    from app.agents.orchestrator import cancel_async_delegation

    task_id = uuid4().hex
    parent_agent_id = uuid4()
    owner_id = uuid4()
    child_session_id = str(uuid4())
    target = SimpleNamespace(id=uuid4(), name="Receipt Worker", role_description="")
    target_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    authority_metadata = _persisted_a2a_authority_metadata(
        task_id=task_id,
        trace_id="trace-db-only-cancel-receipt",
        target=target,
        target_model=target_model,
        owner_id=owner_id,
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session-db-only-cancel",
        child_session_id=child_session_id,
        conversation_messages=[{"role": "user", "content": "cancel this delegated task"}],
        tool_profile="review_readonly",
    )
    pending_receipt = authority_metadata["execution_receipt"]
    persisted = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "running",
        "trace_id": "trace-db-only-cancel-receipt",
        "tenant_id": authority_metadata["tenant_id"],
        "parent_agent_id": str(parent_agent_id),
        "child_agent_id": str(target.id),
        "child_agent_name": target.name,
        "parent_session_id": "parent-session-db-only-cancel",
        "child_session_id": child_session_id,
        "depth": 1,
        "metadata": {
            "owner_id": str(owner_id),
            "target_agent_id": str(target.id),
            "conversation_messages": [{"role": "user", "content": "cancel this delegated task"}],
            "tool_profile": "review_readonly",
            **authority_metadata,
        },
    }

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return persisted

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        assert _task_id == task_id
        persisted["status"] = kwargs["status"]
        persisted["result"] = kwargs["result_summary"]
        persisted["metadata"] = {**persisted["metadata"], **kwargs["metadata_json"]}
        return True

    async def fake_settle(*_args, **_kwargs):
        return None

    async def fake_publish(**_kwargs):
        return None

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._settle_persisted_delegation_budget", fake_settle)
    monkeypatch.setattr("app.services.runtime_control_bus.publish_delegation_cancel", fake_publish)

    result = await cancel_async_delegation(task_id, parent_agent_id=parent_agent_id, force=True)

    durable_receipt = persisted["metadata"]["execution_receipt"]
    assert durable_receipt["status"] == "killed"
    assert durable_receipt["request_hash"] == pending_receipt["request_hash"]
    assert durable_receipt["capability_snapshot_hash"] == pending_receipt["capability_snapshot_hash"]
    assert result["receipt"] == durable_receipt


@pytest.mark.asyncio
async def test_persisted_delegation_budget_settlement_rehydrates_required_identity(monkeypatch):
    from app.agents.orchestrator import _settle_persisted_delegation_budget

    task_id = uuid4().hex
    budget_run_id = uuid4()
    child_agent_id = uuid4()
    captured: list[dict] = []

    async def fake_settle(*, request, task_id, status):
        captured.append(
            {
                "budget_run_id": request.budget_run_id,
                "target_id": request.target.id,
                "target_name": request.target.name,
                "task_id": task_id,
                "status": status,
            }
        )

    monkeypatch.setattr("app.agents.orchestrator._settle_delegation_budget", fake_settle)

    await _settle_persisted_delegation_budget(
        {
            "budget_run_id": str(budget_run_id),
            "child_agent_id": str(child_agent_id),
            "child_agent_name": "Remote Worker",
            "metadata": {},
        },
        task_id=task_id,
        status="killed",
    )

    assert captured == [
        {
            "budget_run_id": str(budget_run_id),
            "target_id": str(child_agent_id),
            "target_name": "Remote Worker",
            "task_id": task_id,
            "status": "killed",
        }
    ]


@pytest.mark.asyncio
async def test_local_cancel_after_transcript_commit_publishes_only_killed_terminal_evidence(monkeypatch):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        _async_tasks,
        _spawn_async_delegation_task,
        cancel_async_delegation,
    )

    task_id = uuid4().hex
    session_id = uuid4().hex
    parent_agent_id = uuid4()
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Race Worker", role_description="", tenant_id=uuid4())
    transcript_committed = asyncio.Event()
    durable = {
        "task_id": task_id,
        "status": "running",
        "result": None,
        "parent_agent_id": str(parent_agent_id),
        "child_agent_id": str(target.id),
        "child_agent_name": target.name,
        "child_session_id": session_id,
        "metadata": {"execution_receipt": {"status": "pending"}},
    }
    evidence: list[str] = []
    settlements: list[str] = []

    async def fake_delegate(_request):
        transcript_committed.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled provider output must not return")

    async def fake_update(_task_id, **fields):
        assert _task_id == task_id
        if durable["status"] != "running":
            return False
        durable["status"] = fields["status"]
        durable["result"] = fields.get("result_summary")
        durable["metadata"] = dict(fields.get("metadata_json") or {})
        return True

    async def fake_get(_task_id):
        assert _task_id == task_id
        return dict(durable)

    async def fake_terminal_evidence(**kwargs):
        evidence.append(kwargs["status"])
        return {"status": kwargs["status"]}

    async def fake_settle(*_args, status, **_kwargs):
        settlements.append(status)

    async def noop_async(*_args, **_kwargs):
        return None

    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "work"}],
        owner_id=owner_id,
        session_id=session_id,
        runtime_task_id=task_id,
        trace_id="trace-local-cancel",
        tenant_id=target.tenant_id,
        **_a2a_authority_kwargs(
            target=target,
            owner_id=owner_id,
            session_id=session_id,
            parent_agent_id=parent_agent_id,
        ),
    )
    monkeypatch.setattr("app.agents.orchestrator._delegate", fake_delegate)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_terminal_evidence", fake_terminal_evidence)
    monkeypatch.setattr("app.agents.orchestrator._settle_delegation_budget", fake_settle)
    monkeypatch.setattr("app.agents.orchestrator._persist_delegation_event", noop_async)
    monkeypatch.setattr("app.agents.orchestrator._release_delegation_coordination_lease", noop_async)

    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-local-cancel")
    await asyncio.wait_for(transcript_committed.wait(), timeout=1)
    result = await cancel_async_delegation(task_id, parent_agent_id=parent_agent_id, force=True)

    assert result["status"] == "killed"
    assert result["result"] == "Task cancelled by parent agent"
    assert durable["status"] == "killed"
    assert evidence == ["killed"]
    assert settlements == ["killed"]
    assert task_id not in _async_tasks


@pytest.mark.asyncio
async def test_check_async_delegation_not_found():
    from app.agents.orchestrator import check_async_delegation

    status = await check_async_delegation("nonexistent-id")
    assert status["status"] == "not_found"


@pytest.mark.asyncio
async def test_delegate_async_handles_failure(monkeypatch):
    from app.agents.orchestrator import _async_tasks, dispatch_persisted_async_delegation

    async def fake_invoke(invocation):
        raise RuntimeError("LLM exploded")

    task_id = uuid4().hex
    owner_id = uuid4()
    parent_agent_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Crasher", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    updates: list[tuple[str, dict]] = []
    authority_metadata = _persisted_a2a_authority_metadata(
        task_id=task_id,
        trace_id="trace-failure",
        target=target,
        target_model=model,
        owner_id=owner_id,
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session-failure",
        child_session_id="sess-2",
        conversation_messages=[{"role": "user", "content": "crash"}],
        tool_profile="review_readonly",
    )

    async def fake_get_runtime_task_record(task_id_arg):
        assert task_id_arg == task_id
        return {
            "task_id": task_id,
            "task_type": "delegation",
            "status": "running",
            "trace_id": "trace-failure",
            "parent_agent_id": str(parent_agent_id),
            "child_agent_id": str(target.id),
            "parent_session_id": "parent-session-failure",
            "child_session_id": "sess-2",
            "depth": 1,
            "metadata": {
                "owner_id": str(owner_id),
                "target_agent_id": str(target.id),
                "conversation_messages": [{"role": "user", "content": "crash"}],
                "tool_profile": "review_readonly",
                "coordination_publish_state": "published",
                **authority_metadata,
            },
        }

    async def fake_resolve_target_runtime(child_agent_id, *, tenant_id):
        assert child_agent_id == target.id
        assert tenant_id == target.tenant_id
        return target, model

    async def fake_update_runtime_task_record(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)

    assert await dispatch_persisted_async_delegation(task_id) is True
    await asyncio.wait_for(_async_tasks[task_id].task, timeout=2.0)

    assert any(task_id_arg == task_id and payload.get("status") == "failed" for task_id_arg, payload in updates)
    _async_tasks.clear()


@pytest.mark.asyncio
async def test_list_async_delegations(monkeypatch):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        _async_tasks,
        _spawn_async_delegation_task,
        list_async_delegations,
    )

    never_finish = asyncio.Event()

    async def fake_invoke(invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="Lister", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    task_id = uuid4().hex
    _spawn_async_delegation_task(
        task_id=task_id,
        trace_id="trace-list",
        request=AgentDelegationRequest(
            target=target,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "list test"}],
            owner_id=uuid4(),
            session_id="sess-3",
            parent_agent_id=uuid4(),
            trace_id="trace-list",
        ),
    )

    tasks = list_async_delegations()
    assert any(t["task_id"] == task_id for t in tasks)
    assert any(t["status"] == "running" for t in tasks)

    # Cleanup
    never_finish.set()
    await asyncio.sleep(0.05)
    _async_tasks.clear()


@pytest.mark.asyncio
async def test_list_async_delegations_filters_by_parent_agent(monkeypatch):
    from app.agents.orchestrator import (
        AgentDelegationRequest,
        _async_tasks,
        _spawn_async_delegation_task,
        list_async_delegations,
    )

    never_finish = asyncio.Event()

    async def fake_invoke(_invocation):
        await never_finish.wait()
        return SimpleNamespace(content="done")

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)

    target = SimpleNamespace(id=uuid4(), name="ScopedWorker", role_description="")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    owner_a = uuid4()
    owner_b = uuid4()
    task_a = uuid4().hex
    task_b = uuid4().hex
    _spawn_async_delegation_task(
        task_id=task_a,
        trace_id="trace-scope-a",
        request=AgentDelegationRequest(
            target=target,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "task-a"}],
            owner_id=uuid4(),
            session_id="sess-scope-a",
            parent_agent_id=owner_a,
            trace_id="trace-scope-a",
        ),
    )
    _spawn_async_delegation_task(
        task_id=task_b,
        trace_id="trace-scope-b",
        request=AgentDelegationRequest(
            target=target,
            target_model=model,
            conversation_messages=[{"role": "user", "content": "task-b"}],
            owner_id=uuid4(),
            session_id="sess-scope-b",
            parent_agent_id=owner_b,
            trace_id="trace-scope-b",
        ),
    )

    tasks = list_async_delegations(parent_agent_id=owner_a)
    task_ids = {task["task_id"] for task in tasks}
    assert task_a in task_ids
    assert task_b not in task_ids

    never_finish.set()
    await asyncio.sleep(0.05)
    _async_tasks.clear()


@pytest.mark.asyncio
async def test_delegate_async_persists_runtime_task_lifecycle(monkeypatch):
    from app.agents.orchestrator import _async_tasks, dispatch_persisted_async_delegation

    task_id = uuid4().hex
    owner_id = uuid4()
    parent_agent_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Worker", role_description="helper")
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    updates: list[tuple[str, dict]] = []
    spans: list[dict] = []
    authority_metadata = _persisted_a2a_authority_metadata(
        task_id=task_id,
        trace_id="trace-runtime",
        target=target,
        target_model=model,
        owner_id=owner_id,
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session-runtime",
        child_session_id="sess-runtime",
        conversation_messages=[{"role": "user", "content": "do research"}],
        tool_profile="review_readonly",
    )

    async def fake_get_runtime_task_record(task_id_arg):
        assert task_id_arg == task_id
        return {
            "task_id": task_id,
            "task_type": "delegation",
            "status": "running",
            "trace_id": "trace-runtime",
            "parent_agent_id": str(parent_agent_id),
            "child_agent_id": str(target.id),
            "parent_session_id": "parent-session-runtime",
            "child_session_id": "sess-runtime",
            "depth": 1,
            "metadata": {
                "owner_id": str(owner_id),
                "target_agent_id": str(target.id),
                "conversation_messages": [{"role": "user", "content": "do research"}],
                "tool_profile": "review_readonly",
                "coordination_publish_state": "published",
                **authority_metadata,
            },
        }

    async def fake_resolve_target_runtime(child_agent_id, *, tenant_id):
        assert child_agent_id == target.id
        assert tenant_id == target.tenant_id
        return target, model

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="async result")

    async def fake_update_task(task_id_arg, **kwargs):
        updates.append((task_id_arg, kwargs))
        return True

    async def fake_persist_span(**kwargs):
        spans.append(kwargs)

    monkeypatch.setattr("app.agents.orchestrator.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._resolve_resumable_target_runtime", fake_resolve_target_runtime)
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_task)
    monkeypatch.setattr("app.agents.orchestrator.persist_invocation_span", fake_persist_span)

    assert await dispatch_persisted_async_delegation(task_id) is True
    await asyncio.sleep(0.05)

    assert any(task_id_arg == task_id and payload.get("status") == "running" for task_id_arg, payload in updates)
    assert any(task_id_arg == task_id and payload.get("status") == "completed" for task_id_arg, payload in updates)
    terminal_update = next(payload for _, payload in updates if payload.get("status") == "completed")
    receipt = terminal_update["metadata_json"]["execution_receipt"]
    assert receipt["status"] == "completed"
    assert receipt["result_refs"] == [f"runtime-task://{task_id}", "session://sess-runtime"]
    assert spans == [
        {
            "db": None,
            "tenant_id": target.tenant_id,
            "trace_id": "trace-runtime",
            "span_id": f"remote-action:{task_id}",
            "parent_span_id": None,
            "parent_trace_id": None,
            "span_type": "remote_action",
            "name": "a2a.delegate",
            "status": "ok",
            "duration_ms": pytest.approx(0.0, abs=1000.0),
            "agent_id": parent_agent_id,
            "user_id": owner_id,
            "runtime_task_id": task_id,
            "session_id": "sess-runtime",
            "request_id": None,
            "execution_identity_type": None,
            "execution_identity_id": None,
            "execution_identity_label": None,
            "metadata": {
                "decision_id": f"a2a:{task_id}",
                "input_hash": receipt["request_hash"],
                "idempotency_key": f"delegation:{task_id}",
                "side_effect_refs": receipt["result_refs"],
                "truth_evidence": [receipt],
                "execution_receipt": receipt,
                "authority_frame_schema": receipt["authority_frame_schema"],
                "authority_snapshot_hash": receipt["capability_snapshot_hash"],
                "policy_snapshot_hash": receipt["policy_snapshot_hash"],
                "execution_principal": receipt["execution_principal"],
                "source": "a2a_delegation",
            },
            "usage": None,
            "error": None,
        }
    ]
    _async_tasks.clear()


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
        **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="cycle-session"),
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
async def test_delegate_marks_non_turn_stop_terminal_reason_as_failed(monkeypatch):
    from app.agents.orchestrator import _delegate, _visited_agents_by_trace
    from app.kernel.contracts import TerminalReason

    _visited_agents_by_trace.clear()
    target = SimpleNamespace(id=uuid4(), name="ProviderFailure", role_description="x")

    async def _provider_failure(_request):
        return SimpleNamespace(
            content="[LLM Error] AI 模型额度或余额不足。",
            parts=[],
            tokens_used=0,
            final_tools=None,
            terminal_reason=TerminalReason.PROVIDER_ERROR,
        )

    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _provider_failure)

    result = await _delegate(_delegation_request(target=target, owner_id=uuid4()))

    assert result.failed is True
    assert result.terminal_reason == "provider_error"


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

        owner_id = uuid4()
        return await _delegate(
            AgentDelegationRequest(
                target=shared_target,
                target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
                conversation_messages=[{"role": "user", "content": "x"}],
                owner_id=owner_id,
                session_id=session_id,
                trace_id=trace_id,
                policy=OrchestrationPolicy(max_depth=5),
                **_a2a_authority_kwargs(
                    target=shared_target,
                    owner_id=owner_id,
                    session_id=session_id,
                ),
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

        assert payload["status"] == "completed"
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
async def test_delegation_parent_completion_uses_durable_outbox(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _wake_parent_session_from_delegation_completion

    captured = {}

    async def fake_enqueue(actual_db, notification):
        captured["db"] = actual_db
        captured["notification"] = notification
        return uuid4()

    monkeypatch.setattr("app.agents.orchestrator.enqueue_completion_notification", fake_enqueue, raising=False)
    db = object()
    tenant_id = uuid4()
    parent_agent_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    owner_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Researcher", tenant_id=tenant_id)
    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "go"}],
        owner_id=owner_id,
        session_id=str(child_session_id),
        parent_agent_id=parent_agent_id,
        parent_session_id=str(parent_session_id),
        trace_id="trace-outbox",
        depth=1,
        tenant_id=tenant_id,
        runtime_task_id="task-1",
    )

    await _wake_parent_session_from_delegation_completion(
        db=db,
        request=request,
        task_id="task-1",
        status="completed",
        summary="done",
        artifacts=[{"type": "artifact", "path": "workspace/report.md"}],
    )

    notification = captured["notification"]
    assert captured["db"] is db
    assert notification.source_kind == "a2a_delegation"
    assert notification.source_run_id == "task-1"
    assert notification.parent_session_id == parent_session_id
    assert notification.parent_agent_id == parent_agent_id
    assert notification.parent_user_id == owner_id
    assert notification.child_session_id == child_session_id
    assert notification.artifacts == [{"type": "artifact", "path": "workspace/report.md"}]


@pytest.mark.asyncio
async def test_delegation_completion_projects_child_session_event_to_parent(monkeypatch):
    """Async delegation completion must append a child_session event to the parent
    session timeline (parity with subagent_run_service), not only update RuntimeTask.

    Revert-sensitive: removing the parent projection in
    `_project_delegation_completion_to_parent` drops the captured event and fails this.
    """
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)
    wakeups: list[dict] = []

    async def fake_wake_parent_session_from_delegation_completion(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr(
        "app.agents.orchestrator._wake_parent_session_from_delegation_completion",
        fake_wake_parent_session_from_delegation_completion,
        raising=False,
    )

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

    assert len(captured) == 2, "expected runtime completion feedback plus child-session projection"
    runtime_event = captured[0]
    assert runtime_event["event_type"] == "runtime_action_completed"
    assert runtime_event["metadata"]["status"] == "completed"
    assert runtime_event["metadata"]["action_kind"] == "a2a_delegation"
    assert runtime_event["metadata"]["child_session_id"]
    event = captured[1]
    assert _uuid.UUID(str(event["session_id"])) == _uuid.UUID(parent_session_id)
    assert event["event_type"] == "child_session"
    assert _uuid.UUID(event["metadata"]["child_session_id"]) == _uuid.UUID(child_session_id)
    assert event["metadata"]["status"] == "completed"
    assert event["metadata"]["reason"] == "delegation_completed"
    assert event["content"] == "Found the answer."
    assert event["listed_surface"] == "chat"
    assert len(wakeups) == 1, "expected delegation completion to wake the parent Agent like CC task-notification"
    assert wakeups[0]["request"] is request
    assert wakeups[0]["task_id"] == "task-1"
    assert wakeups[0]["status"] == "completed"
    assert wakeups[0]["summary"] == "Found the answer."


@pytest.mark.asyncio
async def test_delegation_completion_projects_a2a_artifact_refs_to_parent(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)
    wakeups: list[dict] = []
    artifact_parts = [
        {
            "type": "artifact",
            "artifact_id": "artifact-1",
            "path": "workspace/web3-report.md",
            "name": "web3-report.md",
            "preview_kind": "markdown",
            "source": "a2a_workspace_write",
            "owner_agent_id": "agent-b",
            "source_agent_id": "agent-b",
            "download_agent_id": "agent-b",
        }
    ]

    async def fake_collect_delegation_child_artifact_parts(**kwargs):
        return artifact_parts

    async def fake_wake_parent_session_from_delegation_completion(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr(
        "app.agents.orchestrator._collect_delegation_child_artifact_parts",
        fake_collect_delegation_child_artifact_parts,
        raising=False,
    )
    monkeypatch.setattr(
        "app.agents.orchestrator._wake_parent_session_from_delegation_completion",
        fake_wake_parent_session_from_delegation_completion,
        raising=False,
    )

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
        trace_id="trace-artifacts",
        depth=1,
        tenant_id=tenant_id,
        runtime_task_id="task-1",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-1",
        status="completed",
        summary="Report is ready.",
    )

    runtime_event = captured[0]
    parent_event = captured[1]
    assert runtime_event["event_type"] == "runtime_action_completed"
    assert runtime_event["metadata"]["artifact_paths"] == ["workspace/web3-report.md"]
    assert parent_event["parts"][0]["type"] == "event"
    assert parent_event["parts"][1:] == artifact_parts
    assert parent_event["metadata"]["artifacts"] == artifact_parts
    assert parent_event["metadata"]["artifact_paths"] == ["workspace/web3-report.md"]
    assert wakeups[0]["artifacts"] == artifact_parts


@pytest.mark.asyncio
async def test_modify_existing_delegation_blocks_when_target_artifact_path_not_delivered(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)
    wakeups: list[dict] = []

    async def fake_collect_delegation_child_artifact_parts(**kwargs):
        return [
            {
                "type": "artifact",
                "artifact_id": "artifact-new",
                "path": "workspace/new-report.md",
                "name": "new-report.md",
                "owner_agent_id": "agent-b",
                "source_agent_id": "agent-b",
                "download_agent_id": "agent-b",
            }
        ]

    async def fake_wake_parent_session_from_delegation_completion(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr(
        "app.agents.orchestrator._collect_delegation_child_artifact_parts",
        fake_collect_delegation_child_artifact_parts,
        raising=False,
    )
    monkeypatch.setattr(
        "app.agents.orchestrator._wake_parent_session_from_delegation_completion",
        fake_wake_parent_session_from_delegation_completion,
        raising=False,
    )

    parent_session_id = uuid4().hex
    child_session_id = uuid4().hex
    parent_agent_id = uuid4()
    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Researcher", tenant_id=tenant_id)
    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "Update the current report"}],
        owner_id=uuid4(),
        session_id=child_session_id,
        parent_agent_id=parent_agent_id,
        parent_session_id=parent_session_id,
        trace_id="trace-artifacts",
        depth=1,
        tenant_id=tenant_id,
        runtime_task_id="task-1",
        target_artifact_path="workspace/current-report.md",
        edit_mode="modify_existing",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-1",
        status="completed",
        summary="Report is ready.",
    )

    runtime_event = captured[0]
    parent_event = captured[1]
    assert runtime_event["event_type"] == "runtime_action_blocked"
    assert runtime_event["metadata"]["status"] == "blocked"
    assert runtime_event["metadata"]["reason"] == "delegation_artifact_contract_mismatch"
    assert runtime_event["metadata"]["target_artifact_path"] == "workspace/current-report.md"
    assert parent_event["metadata"]["status"] == "blocked"
    assert parent_event["metadata"]["reason"] == "delegation_artifact_contract_mismatch"
    assert parent_event["metadata"]["target_artifact_path"] == "workspace/current-report.md"
    assert wakeups[0]["status"] == "blocked"
    assert "workspace/current-report.md" in wakeups[0]["summary"]


@pytest.mark.asyncio
async def test_modify_existing_delegation_blocks_when_any_cross_workspace_target_artifact_missing(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _project_delegation_completion_to_parent

    captured = _capture_parent_session_events(monkeypatch)
    wakeups: list[dict] = []

    async def fake_collect_delegation_child_artifact_parts(**kwargs):
        return [
            {
                "type": "artifact",
                "artifact_id": "artifact-deck",
                "path": "workspace/board-review.pptx",
                "name": "board-review.pptx",
                "owner_agent_id": "agent-b",
                "source_agent_id": "agent-b",
                "download_agent_id": "agent-b",
            }
        ]

    async def fake_wake_parent_session_from_delegation_completion(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr(
        "app.agents.orchestrator._collect_delegation_child_artifact_parts",
        fake_collect_delegation_child_artifact_parts,
        raising=False,
    )
    monkeypatch.setattr(
        "app.agents.orchestrator._wake_parent_session_from_delegation_completion",
        fake_wake_parent_session_from_delegation_completion,
        raising=False,
    )

    parent_session_id = uuid4().hex
    child_session_id = uuid4().hex
    parent_agent_id = uuid4()
    tenant_id = uuid4()
    target = SimpleNamespace(id=uuid4(), name="Builder", tenant_id=tenant_id)
    request = AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(),
        conversation_messages=[{"role": "user", "content": "Update the deck and source file"}],
        owner_id=uuid4(),
        session_id=child_session_id,
        parent_agent_id=parent_agent_id,
        parent_session_id=parent_session_id,
        trace_id="trace-artifacts",
        depth=1,
        tenant_id=tenant_id,
        runtime_task_id="task-1",
        target_artifacts=[
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
        ],
        edit_mode="modify_existing",
    )

    await _project_delegation_completion_to_parent(
        request=request,
        task_id="task-1",
        status="completed",
        summary="Deck is ready.",
    )

    runtime_event = captured[0]
    parent_event = captured[1]
    assert runtime_event["event_type"] == "runtime_action_blocked"
    assert runtime_event["metadata"]["reason"] == "delegation_artifact_contract_mismatch"
    assert runtime_event["metadata"]["target_artifact_paths"] == [
        "workspace/board-review.pptx",
        "workspace/src/forecast.py",
    ]
    assert parent_event["metadata"]["target_artifact_paths"] == [
        "workspace/board-review.pptx",
        "workspace/src/forecast.py",
    ]
    assert "workspace/src/forecast.py" in wakeups[0]["summary"]


def test_delegation_child_session_is_listed_as_readable_a2a_chat() -> None:
    source_path = Path(__file__).resolve().parents[2] / "app" / "agents" / "orchestrator.py"
    source = source_path.read_text(encoding="utf-8")
    marker = 'session_kind="delegation_run"'
    assert marker in source
    marker_index = source.index(marker)
    block_start = source.rfind("ChatSession(", 0, marker_index)
    delegation_session_block = source[block_start : marker_index + 1200]
    assert 'source_channel="agent"' in delegation_session_block
    assert 'listed_surface="chat"' in delegation_session_block
    assert 'listed_surface="task_updates"' not in delegation_session_block


def test_delegation_child_tool_results_bind_workspace_artifacts() -> None:
    source_path = Path(__file__).resolve().parents[2] / "app" / "agents" / "orchestrator.py"
    source = source_path.read_text(encoding="utf-8")
    marker = "async def _on_delegation_tool_call"
    assert marker in source
    marker_index = source.index(marker)
    block = source[marker_index : source.index("invocation = AgentInvocationRequest", marker_index)]
    assert "tool_session_write_paths" in block
    assert "artifact_paths" in block
    assert (
        "create_chat_artifacts_for_message"
        in source[source.index("async def _append_child_transcript_event") : marker_index]
    )


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

    assert len(captured) == 2
    runtime_event = captured[0]
    parent_event = captured[1]
    assert runtime_event["event_type"] == "runtime_action_failed"
    assert runtime_event["metadata"]["status"] == "failed"
    assert runtime_event["metadata"]["reason"] == "delegation_failed"
    assert parent_event["metadata"]["status"] == "failed"
    assert parent_event["metadata"]["reason"] == "delegation_failed"


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
async def test_spawn_async_delegation_task_defers_parent_projection_to_terminal_outbox(monkeypatch):
    """The producer seals RuntimeTask/outbox state; its consumer owns parent projection."""
    from app.agents.orchestrator import (
        AgentDelegationResult,
        _async_tasks,
        _spawn_async_delegation_task,
    )

    projected: list[dict] = []
    released: list[dict] = []

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

    async def fake_release(**kwargs):
        released.append(kwargs)
        return True

    monkeypatch.setattr("app.agents.orchestrator._delegate", fake_delegate)
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.agents.orchestrator._project_delegation_completion_to_parent", fake_project)
    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", fake_plan_gate)
    monkeypatch.setattr("app.agents.orchestrator._release_delegation_coordination_lease", fake_release)

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
        coordination_task_key="delegate:parent:worker:instruction",
        coordination_lease_id="lease-wire-1",
    )

    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id="trace-wire")
    state = _async_tasks.get(task_id)
    assert state is not None
    await state.task

    assert projected == []
    assert released == [
        {
            "task_id": task_id,
            "tenant_id": request.tenant_id,
            "task_key": request.coordination_task_key,
            "lease_id": request.coordination_lease_id,
            "reason": "delegation_terminal",
            "coordination_gateway": None,
        }
    ]
