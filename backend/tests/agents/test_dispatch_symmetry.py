"""F-1 dispatch symmetry tests.

Crown-jewel invariant: the instruction the parent agent writes reaches the
child session VERBATIM — no ``## Delegated Task Brief`` envelope, no
``[Delegated by X]`` prefix, no forced ``Completed/Evidence/Blockers``
return template baked into the user message.

spawn_subagent already does this (task → messages[0]["content"]).
delegate_to_agent must match it.  Identity differences are preserved: the
worker framing still names the parent, and a delegation_token is still issued.

Capture pattern mirrors test_subagent_spawn_tool.py: monkeypatch invoke_agent,
capture the AgentInvocationRequest, assert on .messages.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

INSTRUCTION = "Audit auth/*.py and list token bugs"


def _target():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="SecurityReviewer",
        role_description="Security specialist",
    )


def _model():
    return SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )


def _fake_invoke_capturing(store: dict, content: str = "result"):
    async def fake_invoke(request):
        store["request"] = request
        return SimpleNamespace(content=content)

    return fake_invoke


def _authority_kwargs(*, target, owner_id: uuid.UUID, session_id: str) -> dict:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = getattr(target, "tenant_id", None) or uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    target.tenant_id = tenant_id
    return {
        "parent_agent_id": parent_agent_id,
        "parent_session_id": session_id,
        "execution_principal": ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=parent_agent_id,
            requester_user_id=owner_id,
            root_session_id=session_id,
            delegation_chain=(f"agent:{parent_agent_id}",),
        ).to_evidence(),
    }


# ---------------------------------------------------------------------------
# test_delegate_passes_instruction_verbatim
# RED until _build_delegation_brief is replaced with _delegation_user_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_passes_instruction_verbatim(monkeypatch):
    """The first user message in the child session must equal the raw instruction."""
    from app.agents.orchestrator import delegate_to_agent

    captured: dict = {}
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _fake_invoke_capturing(captured))
    monkeypatch.setattr(
        "app.agents.orchestrator._delegation_plan_gate_allows",
        lambda _r: (True, None),
    )

    async def _fake_plan_gate(_r):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", _fake_plan_gate)

    target = _target()
    owner_id = uuid.uuid4()
    await delegate_to_agent(
        target=target,
        target_model=_model(),
        conversation_messages=[{"role": "user", "content": INSTRUCTION}],
        owner_id=owner_id,
        session_id="sess-symm-1",
        **_authority_kwargs(target=target, owner_id=owner_id, session_id="sess-symm-1"),
    )

    req = captured["request"]
    assert len(req.messages) == 1
    # Must be verbatim — no envelope header, no prefix
    assert req.messages[0]["content"] == INSTRUCTION


# ---------------------------------------------------------------------------
# test_delegate_no_three_section_envelope
# RED: currently the content IS wrapped in ## Delegated Task Brief and the
# suffix DOES contain Completed:/Evidence:/Blockers:
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_no_three_section_envelope(monkeypatch):
    """Child session must contain no mechanical 3-section template."""
    from app.agents.orchestrator import delegate_to_agent

    captured: dict = {}
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _fake_invoke_capturing(captured))

    async def _fake_plan_gate(_r):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", _fake_plan_gate)

    target = _target()
    owner_id = uuid.uuid4()
    await delegate_to_agent(
        target=target,
        target_model=_model(),
        conversation_messages=[{"role": "user", "content": INSTRUCTION}],
        owner_id=owner_id,
        session_id="sess-symm-2",
        **_authority_kwargs(target=target, owner_id=owner_id, session_id="sess-symm-2"),
    )

    req = captured["request"]
    user_content = req.messages[0]["content"]
    combined_suffix = req.system_prompt_suffix or ""

    # No envelope in user message
    assert "## Delegated Task Brief" not in user_content
    assert "[Delegated by" not in user_content

    # No forced return template in prompt suffix
    assert "Completed:" not in combined_suffix
    assert "Evidence:" not in combined_suffix
    assert "Blockers:" not in combined_suffix


# ---------------------------------------------------------------------------
# test_delegate_keeps_source_as_metadata
# RED: currently "[Delegated by X]" is prefixed into the content string.
# After fix: parent_agent_name should appear in the framing suffix, NOT
# the user message content.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_keeps_source_as_metadata(monkeypatch):
    """Parent name reaches worker framing (suffix), NOT the instruction text."""
    from app.agents.orchestrator import delegate_to_agent

    target = _target()
    parent_name = "CoordinatorAgent"
    owner_id = uuid.uuid4()

    captured: dict = {}
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _fake_invoke_capturing(captured))

    async def _fake_plan_gate(_r):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", _fake_plan_gate)

    await delegate_to_agent(
        target=target,
        target_model=_model(),
        conversation_messages=[{"role": "user", "content": INSTRUCTION}],
        owner_id=owner_id,
        session_id="sess-symm-3",
        parent_agent_name=parent_name,
        **_authority_kwargs(target=target, owner_id=owner_id, session_id="sess-symm-3"),
    )

    req = captured["request"]
    user_content = req.messages[0]["content"]
    combined_suffix = req.system_prompt_suffix or ""

    # Source must NOT contaminate the instruction
    assert parent_name not in user_content

    # Source MUST appear in framing context
    assert parent_name in combined_suffix


# ---------------------------------------------------------------------------
# test_delegate_preserves_identity_semantics
# Should pass even before fix (token + soul usage), confirms symmetry ≠
# erasing identity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_preserves_identity_semantics(monkeypatch):
    """delegate still issues delegation_token and uses the target agent's soul."""
    from app.agents.orchestrator import delegate_to_agent

    target = _target()
    owner_id = uuid.uuid4()

    captured: dict = {}
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", _fake_invoke_capturing(captured))

    async def _fake_plan_gate(_r):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", _fake_plan_gate)

    await delegate_to_agent(
        target=target,
        target_model=_model(),
        conversation_messages=[{"role": "user", "content": INSTRUCTION}],
        owner_id=owner_id,
        session_id="sess-symm-4",
        **_authority_kwargs(target=target, owner_id=owner_id, session_id="sess-symm-4"),
    )

    req = captured["request"]

    # delegation_token is issued (identity invariant)
    assert req.delegation_token is not None
    assert req.delegation_token.child_agent_id == target.id

    # kernel receives the target agent's identity (soul bound via agent_id)
    assert req.agent_id == target.id
    assert req.agent_name == target.name


# ---------------------------------------------------------------------------
# test_spawn_and_delegate_same_user_message_for_same_instruction
# RED: spawn content == instruction, delegate content currently != instruction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_and_delegate_same_user_message_for_same_instruction(monkeypatch):
    """Same instruction string: spawn content and delegate child first-user-message
    are byte-identical after the fix."""
    from app.agents.orchestrator import delegate_to_agent
    from app.agents.subagent import SubagentSpawnContext, explorer_spec, spawn_subagent

    owner_id = uuid.uuid4()

    # --- capture delegate child first user-message ---
    delegate_captured: dict = {}
    monkeypatch.setattr(
        "app.agents.orchestrator.invoke_agent",
        _fake_invoke_capturing(delegate_captured),
    )

    async def _fake_plan_gate(_r):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", _fake_plan_gate)

    target = _target()
    await delegate_to_agent(
        target=target,
        target_model=_model(),
        conversation_messages=[{"role": "user", "content": INSTRUCTION}],
        owner_id=owner_id,
        session_id="sess-symm-5a",
        **_authority_kwargs(target=target, owner_id=owner_id, session_id="sess-symm-5a"),
    )
    delegate_user_msg = delegate_captured["request"].messages[0]["content"]

    # --- capture spawn task ---
    spawn_captured: dict = {}

    async def fake_spawn_invoke(request):
        spawn_captured["request"] = request
        return SimpleNamespace(content="done", tokens_used=3)

    ctx = SubagentSpawnContext(
        parent_agent_id=owner_id,
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="openai", model="m", api_key="k", base_url=None),
    )
    await spawn_subagent(ctx, explorer_spec("worker"), INSTRUCTION, invoke=fake_spawn_invoke)
    spawn_user_msg = spawn_captured["request"].messages[0]["content"]

    # symmetry: both must equal the raw instruction
    assert spawn_user_msg == INSTRUCTION
    assert delegate_user_msg == INSTRUCTION
    assert delegate_user_msg == spawn_user_msg
