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
                label="Rocky via wechat_personal",
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
