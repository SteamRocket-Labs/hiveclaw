"""Contract tests for the system_plan_run launcher (path-unification §12 / cut ③).

``launch_system_plan_run`` pre-arms the SAME Plan Mode runtime used by live chat
/ unattended tool-intercept (read-only ContextVar + typed PlanModeState) — but
*before* the loop and with the draft's ``plan_id`` already set — then runs the
agent main loop so the agent authors the plan via ``exit_plan_mode`` (which fills
THAT draft, cut ③a). These tests assert the launcher's pre-arm + invocation
contract and its fail-closed guarantee; the actual fill is covered by the
exit_plan_mode dual-state tests.

The DB-touching surface (``_resolve_agent_models``) is exercised with the
project's hand-rolled async-session fake; ``invoke_agent`` is patched on the
runtime invoker module (it is lazily imported inside the launcher).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ResolveSession:
    """Answers the launcher's (agent, model, fallback) lookups in order."""

    def __init__(self, *, agent, model=None, fallback=None):
        self._agent = agent
        self._model = model
        self._fallback = fallback
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _ScalarOneResult(self._agent)
        if self._calls == 2:
            return _ScalarOneResult(self._model)
        return _ScalarOneResult(self._fallback)


def _agent(**over):
    data = {
        "id": uuid4(),
        "name": "Planner Agent",
        "role_description": "Assistant",
        "tenant_id": None,
        "primary_model_id": uuid4(),
        "fallback_model_id": None,
        "owner_user_id": None,
        "creator_id": uuid4(),
    }
    data.update(over)
    return SimpleNamespace(**data)


def _draft_plan(agent, **over):
    data = {
        "id": uuid4(),
        "agent_id": agent.id,
        "tenant_id": agent.tenant_id,
        "session_id": "sess-1",
        "runtime_task_id": None,
        "requested_by_user_id": uuid4(),
        "intent_type": "long_task",
        "original_request": "每天 9 点给我发 RWA 日报",
        "status": "draft",
    }
    data.update(over)
    return SimpleNamespace(**data)


def _patch_resolve(monkeypatch, session):
    from app.services import plan_mode_system_run as mod

    monkeypatch.setattr(mod, "async_session", lambda: session)


@pytest.mark.asyncio
async def test_launch_arms_plan_mode_with_draft_id_then_resets(monkeypatch):
    """The run must see Plan Mode armed (ContextVar) carrying the draft plan_id,
    source=system_plan_run; after the run the ContextVar is reset (no leak)."""
    from app.services import plan_mode_system_run as mod
    from app.services.plan_mode_runtime_context import (
        interactive_plan_mode_active,
        interactive_plan_mode_metadata,
    )

    agent = _agent()
    model = SimpleNamespace(id=agent.primary_model_id, provider="openai", model="x")
    plan = _draft_plan(agent)
    _patch_resolve(monkeypatch, _ResolveSession(agent=agent, model=model))

    captured = {}

    async def fake_invoke_agent(request):
        # The agent loop sees Plan Mode armed with THIS draft's id.
        captured["armed_active"] = interactive_plan_mode_active()
        captured["armed_plan_id"] = interactive_plan_mode_metadata().get("plan_id")
        captured["source"] = request.session_context.source
        captured["plan_mode_active"] = request.session_context.plan_mode.active
        captured["state_plan_id"] = request.session_context.plan_mode.plan_id
        captured["max_tool_rounds"] = request.max_tool_rounds
        captured["agent_id"] = request.agent_id
        return SimpleNamespace(content="planned", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke_agent)

    returned = await mod.launch_system_plan_run(plan)

    assert returned is plan
    assert captured["armed_active"] is True
    assert captured["armed_plan_id"] == str(plan.id)
    assert captured["source"] == mod.SYSTEM_PLAN_RUN_SOURCE
    assert captured["plan_mode_active"] is True
    assert captured["state_plan_id"] == str(plan.id)
    assert captured["max_tool_rounds"] == mod.SYSTEM_PLAN_RUN_MAX_ROUNDS
    assert captured["agent_id"] == plan.agent_id
    # ContextVar reset after the run — must not leak into a later invocation.
    assert interactive_plan_mode_active() is False


@pytest.mark.asyncio
async def test_launch_passes_seed_context_into_prompt(monkeypatch):
    from app.services import plan_mode_system_run as mod

    agent = _agent()
    model = SimpleNamespace(id=agent.primary_model_id, provider="openai", model="x")
    plan = _draft_plan(agent)
    _patch_resolve(monkeypatch, _ResolveSession(agent=agent, model=model))

    captured = {}

    async def fake_invoke_agent(request):
        captured["prompt"] = request.messages[0]["content"]
        return SimpleNamespace(content="planned", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke_agent)

    await mod.launch_system_plan_run(
        plan,
        seed_context={"tool_name": "set_trigger", "action_kind": "create_enabled_trigger"},
    )

    prompt = captured["prompt"]
    assert "Plan Mode" in prompt
    assert "exit_plan_mode" in prompt
    assert plan.original_request in prompt
    assert "set_trigger" in prompt  # seed context surfaced


@pytest.mark.asyncio
async def test_launch_is_fail_closed_when_invoke_raises(monkeypatch):
    """A failed agent run must NOT propagate and must reset the ContextVar — the
    plan is simply left non-confirmable; the launcher never executes the work."""
    from app.services import plan_mode_system_run as mod
    from app.services.plan_mode_runtime_context import interactive_plan_mode_active

    agent = _agent()
    model = SimpleNamespace(id=agent.primary_model_id, provider="openai", model="x")
    plan = _draft_plan(agent)
    _patch_resolve(monkeypatch, _ResolveSession(agent=agent, model=model))

    async def boom_invoke_agent(_request):
        raise RuntimeError("LLM exploded mid-plan")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", boom_invoke_agent)

    returned = await mod.launch_system_plan_run(plan)

    assert returned is plan  # swallowed, returned the (still non-confirmable) plan
    assert interactive_plan_mode_active() is False  # ContextVar reset in finally


@pytest.mark.asyncio
async def test_launch_noop_when_agent_missing(monkeypatch):
    from app.services import plan_mode_system_run as mod

    plan = _draft_plan(_agent())
    _patch_resolve(monkeypatch, _ResolveSession(agent=None))

    invoked = {"n": 0}

    async def fake_invoke_agent(_request):
        invoked["n"] += 1
        return SimpleNamespace(content="x", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke_agent)

    returned = await mod.launch_system_plan_run(plan)
    assert returned is plan
    assert invoked["n"] == 0  # no run without an agent


@pytest.mark.asyncio
async def test_launch_noop_when_model_missing(monkeypatch):
    from app.services import plan_mode_system_run as mod

    agent = _agent()
    plan = _draft_plan(agent)
    # agent resolves but its model row does not.
    _patch_resolve(monkeypatch, _ResolveSession(agent=agent, model=None))

    invoked = {"n": 0}

    async def fake_invoke_agent(_request):
        invoked["n"] += 1
        return SimpleNamespace(content="x", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke_agent)

    returned = await mod.launch_system_plan_run(plan)
    assert returned is plan
    assert invoked["n"] == 0  # no run without a usable model
