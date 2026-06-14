"""request_plan_mode — the agent's CC-style EnterPlanMode request tool.

CC's EnterPlanMode lets the model *request* planning; the user approves before
plan mode actually starts. Hive mirrors this two-step async shape: the agent calls
request_plan_mode(reason) → the handler returns a ``plan_mode_entry_requested``
signal and the agent ENDs its turn → the frontend renders an approval card → on
approval the existing ``plan_mode_requested`` entry path activates Plan Mode (zero
change to the core activation logic). The user is the gate: nothing flips into
Plan Mode from the tool result alone.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _request(tmp_path: Path, arguments: dict, *, user_id: uuid.UUID | None = None):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="request_plan_mode",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=user_id or uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path,
            session_id="s-1",
        ),
    )


# ── Wiring contracts (the green-tests-don't-mean-done guards) ──


def test_request_plan_mode_is_turn_1_core():
    # Real wiring: it must be in the turn-1 core surface (like ask_user_question /
    # exit_plan_mode) so the agent can actually SEE and call it in normal live chat
    # *before* Plan Mode is active. Otherwise the model never reaches it.
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert "request_plan_mode" in CORE_TOOL_NAMES


def test_request_plan_mode_is_capability_gate_exempt():
    # Read-only signal to the current user, no external side effect — exempt like
    # ask_user_question, so it works without a per-tenant capability policy.
    from app.services.capability_gate import _CAPABILITY_GATE_EXEMPT_TOOLS

    assert "request_plan_mode" in _CAPABILITY_GATE_EXEMPT_TOOLS


def test_request_plan_mode_is_registered_in_capability_map():
    # Iron law ②: must be in CAPABILITY_MAP or STRICT_CAPABILITY_MAPPING fail-closed
    # denies it. The startup audit must not report it as unmapped.
    from app.services.capability_gate import CAPABILITY_MAP, audit_capability_mapping

    assert CAPABILITY_MAP.get("request_plan_mode") == "agent.plan.request"
    audit = audit_capability_mapping()
    assert "request_plan_mode" not in audit["unmapped"]


def test_request_plan_mode_excluded_from_subagents():
    # A spawned worker has no user to approve — it must return to its parent, not
    # request a plan-mode entry it can never get approved.
    from app.agents.subagent import _SUBAGENT_BASE_EXCLUDED_TOOLS

    assert "request_plan_mode" in _SUBAGENT_BASE_EXCLUDED_TOOLS


def test_request_plan_mode_is_core_plan_mode_pack_retired():
    # Step 0: plan_mode_pack was a CORE-only catalog anchor (request_plan_mode /
    # exit_plan_mode / ask_user_question are all CORE, turn-1 visible). The
    # zero-effect pack entry is retired; the tool stays CORE.
    from app.services.agent_tools import CORE_TOOL_NAMES
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    assert runtime_tool_group_for_name("plan_mode_pack") is None
    assert "request_plan_mode" in CORE_TOOL_NAMES


def test_request_plan_mode_ends_turn_like_clarification():
    # The kernel must stop after this tool result (the approval card is the
    # terminal output for the turn), exactly as it does for ask_user_question.
    from app.kernel.engine import _tool_result_requests_user_clarification

    payload = json.dumps({"status": "plan_mode_entry_requested", "reason": "multi-step refactor"})
    assert _tool_result_requests_user_clarification("request_plan_mode", payload) is True


# ── Handler behaviour ──


@pytest.mark.asyncio
async def test_request_plan_mode_happy(tmp_path: Path):
    from app.tools.handlers.plan_mode import request_plan_mode

    user_id = uuid.uuid4()
    result = json.loads(
        await request_plan_mode(
            _request(
                tmp_path,
                {"reason": "This spans several files and external sends — let's confirm the plan first."},
                user_id=user_id,
            )
        )
    )
    assert result["status"] == "plan_mode_entry_requested"
    assert result["reason"] == "This spans several files and external sends — let's confirm the plan first."
    # The agent must END its turn and wait for the user's approval.
    assert "next_action" in result
    assert "end" in result["next_action"].lower()
    assert str(result["requested_by_user_id"]) == str(user_id)


@pytest.mark.asyncio
async def test_request_plan_mode_missing_reason(tmp_path: Path):
    from app.tools.handlers.plan_mode import request_plan_mode

    for arguments in ({}, {"reason": "   "}, {"reason": ""}):
        result = json.loads(await request_plan_mode(_request(tmp_path, arguments)))
        assert result["status"] == "error"
        assert result["error_code"] == "missing_reason"


@pytest.mark.asyncio
async def test_request_plan_mode_rejected_when_already_in_plan_mode(tmp_path: Path):
    # Already inside Plan Mode → requesting entry is meaningless; the agent should
    # use exit_plan_mode/ask_user_question instead. Defend so it doesn't loop.
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers.plan_mode import request_plan_mode

    token = set_interactive_plan_mode({"active": True, "original_request": "x"})
    try:
        result = json.loads(
            await request_plan_mode(_request(tmp_path, {"reason": "plan this work"}))
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "error"
    assert result["error_code"] == "already_in_plan_mode"
