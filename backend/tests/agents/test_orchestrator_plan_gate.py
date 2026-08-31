from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _request(*, execution_identity=None):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy

    return AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="飞书知识库助手"),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "查一下飞翼艇报告"}],
        owner_id=uuid4(),
        session_id="wechat-session",
        parent_agent_id=uuid4(),
        parent_session_id="wechat-parent-session",
        policy=OrchestrationPolicy(tool_profile="worker_safe"),
        execution_identity=execution_identity,
        tenant_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_unattended_delegation_verifies_consumed_plan_evidence_without_reconsuming(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegation_plan_gate_allows

    tenant_id = uuid4()
    parent_agent_id = uuid4()
    plan_id = uuid4()
    requester_id = uuid4()
    evidence = {
        "schema": "hive.plan_authorization_evidence.v1",
        "lease_id": str(uuid4()),
        "canonical_args_hash": "args-hash",
        "target_ref": f"plan:{plan_id}:handoff:delegation",
        "requester_user_id": str(requester_id),
        "session_id": "wechat-parent-session",
        "runtime_task_id": None,
        "evidence_id": f"plan-handoff:{plan_id}:delegation",
    }
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=uuid4(), name="投研助理"),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "分析融资动态"}],
        owner_id=requester_id,
        session_id="child-session",
        parent_agent_id=parent_agent_id,
        parent_session_id="wechat-parent-session",
        policy=OrchestrationPolicy(tool_profile="worker_safe"),
        execution_identity=None,
        tenant_id=tenant_id,
        confirmed_plan_id=plan_id,
        confirmed_plan_version=2,
        confirmed_plan_hash="sha256:plan",
        confirmed_plan_session_id="wechat-parent-session",
        plan_authorization=evidence,
    )
    db = object()
    calls = []

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_verify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(lease_id=evidence["lease_id"])

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _SessionContext())
    monkeypatch.setattr("app.services.plan_authorization_lease.verify_consumed_plan_authorization_lease", fake_verify)
    monkeypatch.setattr(
        "app.services.plan_mode_gate.get_plan_mode_gate",
        lambda: (_ for _ in ()).throw(AssertionError("must verify consumed evidence, not consume another lease")),
    )

    allowed, reason = await _delegation_plan_gate_allows(request)

    assert allowed is True
    assert reason == "confirmed_plan_lease_verified"
    assert calls == [
        {
            "db": db,
            "tenant_id": tenant_id,
            "agent_id": parent_agent_id,
            "plan_id": plan_id,
            "evidence": evidence,
        }
    ]


@pytest.mark.asyncio
async def test_unattended_direct_delegation_consumes_and_stamps_exact_action(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegation_plan_gate_allows

    tenant_id = uuid4()
    parent_agent_id = uuid4()
    target_id = uuid4()
    plan_id = uuid4()
    owner_id = uuid4()
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=target_id, name="投研助理"),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": "分析融资动态"}],
        owner_id=owner_id,
        session_id="child-session",
        parent_agent_id=parent_agent_id,
        parent_session_id="parent-session",
        policy=OrchestrationPolicy(tool_profile="research_readonly"),
        execution_identity=None,
        tenant_id=tenant_id,
        confirmed_plan_id=plan_id,
        confirmed_plan_version=3,
        confirmed_plan_hash="sha256:plan",
        confirmed_plan_session_id="parent-session",
        runtime_task_id="runtime-1",
    )

    class _DB:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    db = _DB()
    gate_calls = []

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Gate:
        async def check(self, _db, **kwargs):
            gate_calls.append(kwargs)
            return SimpleNamespace(
                allowed=True,
                reason="confirmed_plan_lease_consumed",
                authorization_lease_id="lease-1",
                canonical_args_hash="args-hash",
                target_ref=kwargs["target_ref"],
                canonical_plan_id=str(plan_id),
                canonical_plan_version=3,
                canonical_plan_hash="sha256:server-plan",
            )

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _SessionContext())
    monkeypatch.setattr("app.services.plan_mode_gate.get_plan_mode_gate", lambda: _Gate())

    allowed, reason = await _delegation_plan_gate_allows(request)

    assert allowed is True
    assert reason == "confirmed_plan_lease_consumed"
    assert db.commits == 1
    assert gate_calls[0]["requester_user_id"] == owner_id
    assert gate_calls[0]["session_id"] == "parent-session"
    assert gate_calls[0]["target_ref"] == f"agent:{target_id}:delegation"
    assert gate_calls[0]["action_artifact"] == {
        "target_agent_id": str(target_id),
        "message": "分析融资动态",
        "tool_profile": "research_readonly",
        "target_artifact_path": None,
        "target_artifacts": [],
        "edit_mode": None,
    }
    assert request.plan_authorization == {
        "schema": "hive.plan_authorization_evidence.v1",
        "lease_id": "lease-1",
        "canonical_args_hash": "args-hash",
        "target_ref": f"agent:{target_id}:delegation",
        "requester_user_id": str(owner_id),
        "session_id": "parent-session",
        "runtime_task_id": None,
        "evidence_id": "delegation-start:runtime-1",
        "plan_id": str(plan_id),
        "plan_version": 3,
        "plan_hash": "sha256:server-plan",
    }


@pytest.mark.asyncio
async def test_user_initiated_delegation_bypasses_plan_gate_db_lookup(monkeypatch):
    from app.agents.orchestrator import _delegation_plan_gate_allows
    from app.kernel.contracts import ExecutionIdentityRef

    def fail_tenant_scoped_session(*_args, **_kwargs):
        raise AssertionError("user-bound delegation should not call PlanModeGate")

    monkeypatch.setattr("app.database.tenant_scoped_session", fail_tenant_scoped_session)

    allowed, reason = await _delegation_plan_gate_allows(
        _request(
            execution_identity=ExecutionIdentityRef(
                identity_type="delegated_user",
                identity_id=uuid4(),
                label="Example Owner via wechat_personal",
            )
        )
    )

    assert allowed is True
    assert reason == "user_initiated_delegation"


@pytest.mark.asyncio
async def test_unattended_delegation_without_user_identity_still_uses_plan_gate(monkeypatch):
    from app.agents.orchestrator import _delegation_plan_gate_allows

    captured = {}

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Gate:
        async def check(self, db, **kwargs):
            captured["db"] = db
            captured["kwargs"] = kwargs
            return SimpleNamespace(allowed=False, reason="no_confirmed_plan")

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: _SessionContext())
    monkeypatch.setattr("app.services.plan_mode_gate.get_plan_mode_gate", lambda: _Gate())

    allowed, reason = await _delegation_plan_gate_allows(_request(execution_identity=None))

    assert allowed is False
    assert reason == "no_confirmed_plan"
    assert captured["kwargs"]["action_kind"] == "start_delegation"
