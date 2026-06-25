"""CCPlus V1 §7 acceptance — turn_state (TerminalReason on real terminal paths).

These are GENUINE acceptance tests for the §7 ``turn_state`` selector. They
assert the integrated behavior that already exists in the tree:

  1. ``AgentKernel.handle`` stamps a *specific* ``TerminalReason`` on every REAL
     terminal exit — not just the standalone helper functions. We drive the
     full kernel loop and read ``result.terminal_reason`` for the
     QUOTA_DENIED, TENANT_RESOLUTION_ERROR, USER_CANCEL, TOOL_BUDGET, and
     CLARIFICATION_REQUIRED paths.
  2. ``session_control_plane`` projects a ``TurnStateV1``-derived view: the live
     workbench ``agent_session.active_turn`` surfaces the persisted
     ``terminal_reason`` and a ``TurnStatus``-coerced ``status`` under the
     ``hive.ccplus.turn_state.v1`` schema.

Revert-sensitive: if any of these terminal exits stopped stamping its specific
TerminalReason (reverting to the ``TURN_STOP`` default on ``InvocationResult``),
or if the projection stopped threading the TurnStateV1 ``terminal_reason``
through ``agent_session.active_turn``, these assertions fail.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.contracts import InvocationRequest, RuntimeConfig, TerminalReason


class _ScriptedClient:
    """Minimal LLM client double: replays queued responses, optional delay."""

    def __init__(self, responses=None, *, delay: float = 0.0) -> None:
        self._responses = list(responses or [])
        self._delay = delay
        self.calls: list[dict] = []
        self.closed = False

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            raise AssertionError("No scripted response left")
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def _kernel_with(runtime_config, *, client=None, execute_tool=None, get_tools=None):
    from app.kernel.engine import AgentKernel, KernelDependencies

    client = client or _ScriptedClient()
    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: runtime_config,
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            resolve_retrieval_context=lambda *_a, **_k: "",
            get_tools=get_tools or (lambda *_a, **_k: []),
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda _model: client,
            execute_tool=execute_tool or (lambda *_a, **_k: "unused"),
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: (usage or {}).get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )


def _request(**overrides) -> InvocationRequest:
    base = {
        "model": _model(),
        "messages": [{"role": "user", "content": "hello"}],
        "agent_name": "Agent",
        "role_description": "desc",
        "agent_id": uuid4(),
        "user_id": uuid4(),
    }
    base.update(overrides)
    return InvocationRequest(**base)


@pytest.mark.asyncio
async def test_turn_state_quota_denied_terminal_reason_on_real_handle():
    """Real ``handle`` quota gate stamps QUOTA_DENIED (not the TURN_STOP default)."""
    config = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message="额度已耗尽")
    kernel = _kernel_with(config)

    result = await kernel.handle(_request())

    assert result.terminal_reason == TerminalReason.QUOTA_DENIED
    assert result.content == "额度已耗尽"


@pytest.mark.asyncio
async def test_turn_state_tenant_resolution_error_terminal_reason_on_real_handle():
    """Real ``handle`` tenant-resolution abort stamps TENANT_RESOLUTION_ERROR."""
    config = RuntimeConfig(
        tenant_id=None,
        max_tool_rounds=5,
        tenant_resolution_error="agent not found",
    )
    kernel = _kernel_with(config)

    result = await kernel.handle(_request())

    assert result.terminal_reason == TerminalReason.TENANT_RESOLUTION_ERROR
    # The error path must abort BEFORE the LLM is ever called.
    assert "tenant resolution failed" in result.content


@pytest.mark.asyncio
async def test_turn_state_user_cancel_terminal_reason_on_real_handle():
    """Real ``handle`` cancel path stamps USER_CANCEL when the cancel_event fires."""
    config = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5)
    client = _ScriptedClient(delay=0.2)
    kernel = _kernel_with(config, client=client)
    cancel_event = asyncio.Event()

    task = asyncio.create_task(kernel.handle(_request(cancel_event=cancel_event)))
    await asyncio.sleep(0.05)
    cancel_event.set()
    result = await task

    assert result.terminal_reason == TerminalReason.USER_CANCEL
    assert result.content == "*[Generation stopped]*"


@pytest.mark.asyncio
async def test_turn_state_tool_budget_terminal_reason_on_real_handle():
    """Real ``handle`` exhausting the tool-round budget stamps TOOL_BUDGET.

    The model keeps emitting a tool call every round; once the round budget is
    exhausted the turn ends with a TOOL_BUDGET terminal reason (the round-limit
    backstop) rather than a generic stop.
    """

    def tool_call_response():
        return SimpleNamespace(
            content="",
            tool_calls=[{"id": f"call_{uuid4().hex[:6]}", "function": {"name": "read_file", "arguments": "{}"}}],
            reasoning_content=None,
            usage={"total_tokens": 3},
        )

    # More scripted responses than the round budget so the budget — not an
    # empty queue — is what ends the turn.
    config = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2)
    client = _ScriptedClient(responses=[tool_call_response() for _ in range(8)])
    kernel = _kernel_with(
        config,
        client=client,
        execute_tool=lambda *_a, **_k: "ok",
        get_tools=lambda *_a, **_k: [
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
        ],
    )

    result = await kernel.handle(_request())

    assert result.terminal_reason == TerminalReason.TOOL_BUDGET
    # The budget exit explains the round limit and preserves state for resume.
    assert "tool-round limit" in result.content


@pytest.mark.asyncio
async def test_turn_state_clarification_required_terminal_reason_on_real_handle():
    """Real ``handle`` pauses with CLARIFICATION_REQUIRED on a blocking ask card.

    A tool result whose JSON status is ``awaiting_user_clarification`` makes the
    kernel pause the turn for the user; that terminal exit carries the
    CLARIFICATION_REQUIRED reason and an empty content (the question already
    streamed as the tool's card).
    """
    clarification_payload = json.dumps(
        {"status": "awaiting_user_clarification", "blocking": True, "question": "Which file?"}
    )

    config = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5)
    client = _ScriptedClient(
        responses=[
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_ask", "function": {"name": "ask_user_question", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 4},
            )
        ]
    )
    kernel = _kernel_with(
        config,
        client=client,
        execute_tool=lambda *_a, **_k: clarification_payload,
        get_tools=lambda *_a, **_k: [
            {"type": "function", "function": {"name": "ask_user_question", "description": "", "parameters": {}}}
        ],
    )

    result = await kernel.handle(_request())

    assert result.terminal_reason == TerminalReason.CLARIFICATION_REQUIRED
    assert result.content == ""


@pytest.mark.asyncio
async def test_turn_state_projection_surfaces_terminal_reason_via_agent_session(monkeypatch):
    """The workbench projects a TurnStateV1-derived active turn with terminal_reason.

    ``session_control_plane.build_session_workbench`` derives a ``TurnStateV1``
    from the REAL active run + its metadata and surfaces it under
    ``agent_session.active_turn`` (schema ``hive.ccplus.turn_state.v1``). A
    persisted ``terminal_reason`` on the run metadata must flow through onto that
    contract-shaped view, and the raw status must be coerced onto the
    ``TurnStatus`` enum (a clean-mapping value passes through; an unknown value
    falls back to RUNNING — proving the coercion actually runs).
    """
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        user_id=uuid4(),
        title="Failed run",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=None,
        root_session_id=session_id,
        runtime_task_id=None,
        created_at=now,
        last_message_at=now,
    )

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        # A live run that ended in a provider error, with the active tool-call
        # ids tracked on the run metadata exactly as the runtime persists them.
        # "failed" is a TurnStatus enum value, so it must pass through coercion.
        return {
            "id": "run-failed",
            "status": "failed",
            "metadata": {
                "turn_id": "turn-9",
                "terminal_reason": TerminalReason.PROVIDER_ERROR.value,
                "active_tool_call_ids": ["toolu_a", "toolu_b"],
            },
        }

    async def _empty(*_a, **_k):
        return []

    async def fake_session_index(*_a, **_k):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", _empty)
    monkeypatch.setattr(service, "_list_goals", _empty)
    monkeypatch.setattr(service, "_list_teams", _empty)
    monkeypatch.setattr(service, "_list_pending_approvals", _empty)
    monkeypatch.setattr(service, "_list_branches", _empty)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    active_turn = result["agent_session"]["active_turn"]
    assert active_turn["schema"] == "hive.ccplus.turn_state.v1"
    # The persisted terminal_reason is threaded onto the TurnStateV1-derived view.
    assert active_turn["terminal_reason"] == TerminalReason.PROVIDER_ERROR.value
    # The raw "failed" status mapped cleanly onto the TurnStatus enum value.
    assert active_turn["status"] == "failed"
    # The active tool-call ids ride along on the contract surface.
    assert active_turn["active_tool_call_ids"] == ["toolu_a", "toolu_b"]
    assert active_turn["session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_turn_state_projection_coerces_unknown_run_status_onto_enum(monkeypatch):
    """An unknown raw run status (e.g. ``killed``) is coerced to RUNNING, not echoed.

    The runtime persists run statuses like ``killed`` that are NOT TurnStatus
    enum members. ``_coerce_turn_status`` maps unknowns onto ``RUNNING`` so the
    projection only ever surfaces enum-valid statuses. Revert-sensitive: if the
    coercion were dropped and the raw status passed through, ``killed`` would
    leak verbatim onto the contract surface. The terminal_reason still rides
    through faithfully.
    """
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        user_id=uuid4(),
        title="Killed run",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=None,
        root_session_id=session_id,
        runtime_task_id=None,
        created_at=now,
        last_message_at=now,
    )

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return {
            "id": "run-killed",
            "status": "killed",  # not a TurnStatus member
            "metadata": {"terminal_reason": TerminalReason.USER_CANCEL.value},
        }

    async def _empty(*_a, **_k):
        return []

    async def fake_session_index(*_a, **_k):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", _empty)
    monkeypatch.setattr(service, "_list_goals", _empty)
    monkeypatch.setattr(service, "_list_teams", _empty)
    monkeypatch.setattr(service, "_list_pending_approvals", _empty)
    monkeypatch.setattr(service, "_list_branches", _empty)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    active_turn = result["agent_session"]["active_turn"]
    # Unknown status coerced to the enum fallback — NOT echoed as "killed".
    assert active_turn["status"] == "running"
    assert active_turn["status"] != "killed"
    # terminal_reason still carries the real cancellation cause.
    assert active_turn["terminal_reason"] == TerminalReason.USER_CANCEL.value


@pytest.mark.asyncio
async def test_turn_state_projection_omits_terminal_reason_when_idle(monkeypatch):
    """No active run → no active_turn TurnStateV1 view at all (None, not a stub)."""
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        user_id=uuid4(),
        title="Idle",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=None,
        root_session_id=session_id,
        runtime_task_id=None,
        created_at=now,
        last_message_at=now,
    )

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return None

    async def _empty(*_a, **_k):
        return []

    async def fake_session_index(*_a, **_k):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", _empty)
    monkeypatch.setattr(service, "_list_goals", _empty)
    monkeypatch.setattr(service, "_list_teams", _empty)
    monkeypatch.setattr(service, "_list_pending_approvals", _empty)
    monkeypatch.setattr(service, "_list_branches", _empty)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    assert result["agent_session"]["active_turn"] is None
    assert result["active_turn"] is None
