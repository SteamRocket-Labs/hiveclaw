from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_delegation_handoff_starts_async_delegation_with_confirmed_plan(monkeypatch):
    from app.services.plan_mode_delegation_handoff import delegation_handoff_handler

    parent_agent_id = uuid4()
    confirmer_id = uuid4()
    tenant_id = uuid4()
    plan_id = uuid4()
    target_agent = SimpleNamespace(id=uuid4(), name="投研助理")
    target_model = SimpleNamespace(id=uuid4(), model="test-model")
    source_agent = SimpleNamespace(id=parent_agent_id, name="微信协调员", creator_id=uuid4(), tenant_id=tenant_id)
    captured = {}

    async def fake_resolve(from_agent_id, agent_name, *, target_agent_id=None):
        assert from_agent_id == parent_agent_id
        assert agent_name == "投研助理"
        assert target_agent_id is None
        return source_agent, target_agent, target_model, None

    async def fake_delegate_async(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            task_id="runtime-task-1",
            trace_id="trace-1",
            target_name="投研助理",
            status="running",
        )

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", fake_delegate_async)

    plan = SimpleNamespace(
        id=plan_id,
        agent_id=parent_agent_id,
        tenant_id=tenant_id,
        session_id="wechat-session",
        status="confirmed",
        plan_version=2,
        plan_hash="sha256:abc",
        requested_by_user_id=None,
        confirmed_by_user_id=confirmer_id,
        plan_json={
            "objective": "让投研助理分析最近三天 AI Infra 融资动态。",
            "handoff": {
                "target": "delegation",
                "payload": {
                    "agent_name": "投研助理",
                    "message": "分析最近三天 AI Infra 融资动态，返回中文摘要和来源。",
                    "tool_profile": "research_readonly",
                    "max_tool_rounds": 16,
                },
            },
        },
    )

    payload = await delegation_handoff_handler(None, plan)

    assert payload == {
        "runtime_task_id": "runtime-task-1",
        "trace_id": "trace-1",
        "target_agent": "投研助理",
        "status": "running",
    }
    assert captured["target"] is target_agent
    assert captured["target_model"] is target_model
    assert captured["owner_id"] == source_agent.creator_id
    assert captured["parent_agent_id"] == parent_agent_id
    assert captured["parent_session_id"] == "wechat-session"
    assert captured["tenant_id"] == tenant_id
    assert captured["confirmed_plan_id"] == plan_id
    assert captured["confirmed_plan_version"] == 2
    assert captured["confirmed_plan_hash"] == "sha256:abc"
    assert captured["policy"].tool_profile == "research_readonly"
    assert captured["max_tool_rounds"] == 16
    assert captured["conversation_messages"][0]["content"].startswith("[Plan Mode confirmed delegation]")

