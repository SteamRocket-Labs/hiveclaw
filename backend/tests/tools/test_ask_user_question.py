"""ask_user_question — Plan Mode's first-class clarification tool (CC-align Phase B).

CC's plan mode lets the agent ask the user (AskUserQuestion) before committing a
plan; Hive had no such tool, so the agent could only assume defaults. This tool
lets the agent pause for a real answer instead of guessing. It is read-only (asks
the current user, no external side effect) and allowed under Plan Mode policy.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _request(tmp_path: Path, arguments: dict):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="ask_user_question",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path,
            session_id="s-1",
        ),
    )


def test_ask_user_question_is_in_plan_mode_allowlist():
    from app.tools.plan_mode_policy import PLAN_MODE_READONLY_TOOLS

    assert "ask_user_question" in PLAN_MODE_READONLY_TOOLS


def test_ask_user_question_is_capability_gate_exempt():
    # Asks the current user, no external side effect — exempt like read-only
    # context tools, so it works without a per-tenant capability policy.
    from app.services.capability_gate import _CAPABILITY_GATE_EXEMPT_TOOLS

    assert "ask_user_question" in _CAPABILITY_GATE_EXEMPT_TOOLS


def test_ask_user_question_is_turn_1_core():
    # Real wiring: must be in the turn-1 core surface (like exit_plan_mode) so the
    # agent can actually SEE and call it. Registered-but-not-in-surface = the
    # green-tests-don't-mean-done trap (handler works but the agent never reaches it).
    from app.services.agent_tools import CORE_TOOL_NAMES

    assert "ask_user_question" in CORE_TOOL_NAMES


def test_ask_user_question_excluded_from_subagents():
    # A spawned worker has no user-interaction channel — it must return to its
    # parent, not block on ask_user_question.
    from app.agents.subagent import _SUBAGENT_BASE_EXCLUDED_TOOLS

    assert "ask_user_question" in _SUBAGENT_BASE_EXCLUDED_TOOLS


@pytest.mark.asyncio
async def test_ask_user_question_returns_clarification_envelope(tmp_path: Path):
    from app.tools.handlers.plan_mode import ask_user_question

    result = await ask_user_question(
        _request(
            tmp_path,
            {
                "question": "Which asset tracks should the RWA report focus on?",
                "reason": "Track choice changes scope and data sources.",
                "options": [{"label": "US Treasuries", "description": "Ondo/Backed/..."}],
                "blocking": True,
            },
        )
    )
    payload = json.loads(result)
    assert payload["status"] == "awaiting_user_clarification"
    assert payload["question"] == "Which asset tracks should the RWA report focus on?"
    assert payload["options"][0]["label"] == "US Treasuries"
    assert payload["blocking"] is True
    # The agent must present the question and stop — not assume an answer.
    assert "end your turn" in payload["next_action"].lower()
    assert "exit_plan_mode" in payload["next_action"]


@pytest.mark.asyncio
async def test_ask_user_question_rejects_empty_question(tmp_path: Path):
    from app.tools.handlers.plan_mode import ask_user_question

    result = await ask_user_question(_request(tmp_path, {"question": "   "}))
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "missing_question"
