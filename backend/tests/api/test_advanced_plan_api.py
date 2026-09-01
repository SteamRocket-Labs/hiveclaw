from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_advanced_plan_api_starts_advanced_plan_runtime(monkeypatch):
    import app.api.advanced_plan as advanced_plan_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Agent")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=current_user.id)
    db = object()
    captured = {}

    async def fake_authorize(db_arg, user_arg, **kwargs):
        assert db_arg is db
        assert user_arg is current_user
        assert kwargs == {
            "agent_id": agent_id,
            "session_id": session_id,
            "action": "advanced_plan:start",
            "require_writable": True,
        }
        return SimpleNamespace(agent=agent, session=session)

    async def fake_start_web_chat_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": "plan-run-1", "status": "running"}

    monkeypatch.setattr(advanced_plan_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(advanced_plan_api, "start_web_chat_run", fake_start_web_chat_run)

    result = await advanced_plan_api.start_advanced_plan(
        agent_id=agent_id,
        session_id=session_id,
        body=advanced_plan_api.StartAdvancedPlanIn(
            objective="Design the parity rollout.",
            context={"source": "freecode-parity"},
        ),
        current_user=current_user,
        db=db,
    )

    assert result["run_id"] == "plan-run-1"
    assert captured["agent"] is agent
    assert captured["user"] is current_user
    assert captured["session"] is session
    assert captured["runtime_task_type"] == "advanced_plan"
    assert captured["append_user_message"] is False
    assert captured["extra_metadata"]["advanced_plan"] is True
    assert captured["extra_metadata"]["context"] == {"source": "freecode-parity"}


@pytest.mark.asyncio
async def test_advanced_plan_api_rejects_cross_user_session_before_start(monkeypatch):
    import app.api.advanced_plan as advanced_plan_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = object()
    start_calls = 0

    async def fake_authorize(db_arg, user_arg, **kwargs):
        assert db_arg is db
        assert user_arg is current_user
        assert kwargs == {
            "agent_id": agent_id,
            "session_id": session_id,
            "action": "advanced_plan:start",
            "require_writable": True,
        }
        raise HTTPException(status_code=403, detail="This session belongs to a different user")

    async def fake_start_web_chat_run(**_kwargs):
        nonlocal start_calls
        start_calls += 1

    monkeypatch.setattr(advanced_plan_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(advanced_plan_api, "start_web_chat_run", fake_start_web_chat_run)

    with pytest.raises(HTTPException) as exc_info:
        await advanced_plan_api.start_advanced_plan(
            agent_id=agent_id,
            session_id=session_id,
            body=advanced_plan_api.StartAdvancedPlanIn(objective="Read another user's session."),
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert start_calls == 0
