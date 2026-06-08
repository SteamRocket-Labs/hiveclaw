"""Plan Mode current-session continuation handoff (CC-align §4.2).

The production "用不了" chain was: live chat Plan Mode -> confirm -> target
``long_task`` -> NO handler -> ``handoff_status="skipped"`` -> no RuntimeTask ->
agent says "not confirmed". These tests pin the fix: a confirmed continuation
plan starts a real ``web_chat_turn`` run carrying the approved plan, returns its
id, queues (not errors) when a run is already active, and fails closed (visible
reason, not silent skipped) without a live session.

The handler loads entities via module-level ``_load_*`` seams (stubbed here, same
style as ``plan_mode_handoff._load_agent``) and starts the run via
``start_web_chat_run`` (stubbed), so no DB engine / LLM is involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.plan_mode_session_handoff as mod


def _confirmed_plan(*, session_id="sess-1", user_id="user-1", plan_markdown="## 思路\n聚焦三条赛道。"):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        status="confirmed",
        session_id=session_id,
        requested_by_user_id=user_id,
        plan_version=1,
        plan_hash="sha256:abc",
        original_request="做 RWA 周报",
        plan_json={"objective": "出 RWA 周报", "plan_markdown": plan_markdown},
    )


def _stub_entities(monkeypatch, *, expired=False):
    async def _agent(db, _id):
        return SimpleNamespace(id=_id, name="A")

    async def _user(db, _id):
        return SimpleNamespace(id=_id)

    async def _session(db, _id):
        return SimpleNamespace(id=_id)

    monkeypatch.setattr(mod, "_load_agent", _agent)
    monkeypatch.setattr(mod, "_load_user", _user)
    monkeypatch.setattr(mod, "_load_session", _session)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _agent: expired)


@pytest.mark.asyncio
async def test_continuation_starts_current_session_run_with_plan_in_prompt(monkeypatch):
    captured = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"id": "run-123", "status": "running"}

    _stub_entities(monkeypatch)
    monkeypatch.setattr("app.services.web_chat_runtime.start_web_chat_run", fake_start)

    plan = _confirmed_plan()
    result = await mod.continue_current_session_handoff(db=None, plan=plan)

    assert result["runtime_task_id"] == "run-123"
    assert result["session_id"] == "sess-1"
    assert result["execution"] == "current_session"
    # Amendment ④: the approved plan is injected into the run's PROMPT (content),
    # not just metadata — the agent's marching orders.
    assert "聚焦三条赛道" in captured["content"]
    assert str(plan.id) in captured["content"]
    # Clean UX message is what the chat shows as the turn.
    assert captured["display_content"] == "✅ 计划已确认，开始执行"
    # Audit provenance stamped on the run metadata.
    assert captured["extra_metadata"]["approved_plan_id"] == str(plan.id)
    assert captured["extra_metadata"]["source"] == "plan_mode_handoff"


@pytest.mark.asyncio
async def test_continuation_queues_when_a_run_is_already_active(monkeypatch):
    from app.services.web_chat_runtime import ActiveWebChatRunExists

    async def fake_start(**kwargs):
        raise ActiveWebChatRunExists({"id": "active-9"})

    _stub_entities(monkeypatch)
    monkeypatch.setattr("app.services.web_chat_runtime.start_web_chat_run", fake_start)

    result = await mod.continue_current_session_handoff(db=None, plan=_confirmed_plan())

    # Not an error, not lost: queued behind the active run (UI shows "waiting").
    assert result["handoff_status"] == "queued"
    assert result["reason"] == "active_run_exists"
    assert result["active_run_id"] == "active-9"


@pytest.mark.asyncio
async def test_continuation_fails_closed_without_a_live_session(monkeypatch):
    _stub_entities(monkeypatch)
    plan = _confirmed_plan(session_id=None)
    with pytest.raises(mod.SessionHandoffError):
        await mod.continue_current_session_handoff(db=None, plan=plan)


@pytest.mark.asyncio
async def test_continuation_requires_confirmed_status(monkeypatch):
    _stub_entities(monkeypatch)
    plan = _confirmed_plan()
    plan.status = "awaiting_confirmation"
    with pytest.raises(mod.SessionHandoffError):
        await mod.continue_current_session_handoff(db=None, plan=plan)


@pytest.mark.asyncio
async def test_continuation_fails_closed_when_agent_expired(monkeypatch):
    _stub_entities(monkeypatch, expired=True)
    monkeypatch.setattr(
        "app.services.web_chat_runtime.start_web_chat_run",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not start a run for an expired agent")),
    )
    with pytest.raises(mod.SessionHandoffError):
        await mod.continue_current_session_handoff(db=None, plan=_confirmed_plan())


@pytest.mark.asyncio
async def test_run_handoff_honors_handler_queued_status():
    # The service's _run_handoff must honor a handler-signaled non-terminal status
    # (queued) instead of always stamping "completed".
    from app.services.plan_mode_service import PlanModeService

    service = PlanModeService()

    async def fake_handler(db, plan):
        return {"handoff_status": "queued", "reason": "active_run_exists", "session_id": "s1"}

    service.register_handoff_handler("continue_current_session", fake_handler)
    plan = SimpleNamespace(
        id=uuid4(),
        plan_json={"handoff": {"target": "continue_current_session"}},
        handoff_status=None,
        handoff_payload=None,
    )
    await service._run_handoff(db=None, plan=plan)

    assert plan.handoff_status == "queued"
    assert plan.handoff_payload["reason"] == "active_run_exists"
    assert plan.handoff_payload["target"] == "continue_current_session"
    assert "updated_at" in plan.handoff_payload
    assert "completed_at" not in plan.handoff_payload


@pytest.mark.asyncio
async def test_detached_target_fails_closed_with_visible_reason():
    # Amendment ②: detached background is a fail-closed stub, NOT silent skipped.
    from app.services.plan_mode_detached_handoff import (
        DetachedRuntimeNotAvailable,
        detached_runtime_task_handoff,
    )

    plan = SimpleNamespace(id=uuid4())
    with pytest.raises(DetachedRuntimeNotAvailable):
        await detached_runtime_task_handoff(db=None, plan=plan)
