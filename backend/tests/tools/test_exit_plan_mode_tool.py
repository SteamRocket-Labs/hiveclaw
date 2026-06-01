from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _request(arguments: dict):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="exit_plan_mode",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,
            workspace="/tmp",
            session_id="session-plan",
        ),
    )


class _PlanService:
    def __init__(self):
        self.calls = []

    async def ensure_awaiting_plan_from_fill(self, **kwargs):
        from app.services import plan_mode_core

        self.calls.append(kwargs)
        plan_json = {
            **kwargs["fill"],
            "schema": plan_mode_core.PLAN_SCHEMA,
            "intent_type": kwargs["intent_type"],
        }
        return SimpleNamespace(
            id=uuid4(),
            status="awaiting_confirmation",
            plan_version=1,
            plan_hash=plan_mode_core.compute_plan_hash(plan_json),
            plan_json=plan_json,
            metadata_json=kwargs.get("metadata_json") or {},
        )


@pytest.mark.asyncio
async def test_exit_plan_mode_requires_active_interactive_plan_mode():
    from app.tools.handlers.plan_mode import exit_plan_mode

    result = json.loads(
        await exit_plan_mode(
            _request(
                {
                    "title": "Plan",
                    "objective": "Do the work later",
                    "plan_markdown": "## Plan\nDo it later.",
                    "steps": ["Inspect", "Implement"],
                    "success_criteria": ["User can confirm it"],
                    "stop_conditions": ["User rejects"],
                }
            )
        )
    )

    assert result["status"] == "error"
    assert result["error_code"] == "not_in_plan_mode"


@pytest.mark.asyncio
async def test_exit_plan_mode_creates_needs_plan_payload_from_active_context(monkeypatch):
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode(
        {
            "original_request": "使用 deepresearch做一个web3的全景报告",
            "intent_type": "long_task",
            "handoff_target": "deep_research",
            "deep_research": True,
            "deep_research_args": {"question": "使用 deepresearch做一个web3的全景报告", "depth": "full"},
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Web3 全景研究计划",
                        "objective": "生成一份中文 Web3 全景研究报告。",
                        "plan_markdown": "## Context\n需要先确认研究范围。\n\n## Plan\n1. 分赛道研究。\n2. 综合报告。",
                        "steps": [
                            {"description": "确认研究范围和证据标准。"},
                            "分赛道收集来源并综合。",
                        ],
                        "success_criteria": ["输出 report.md", "关键判断有来源支撑"],
                        "stop_conditions": ["用户拒绝计划", "来源质量不足"],
                        "risk_assessment": {"level": "high", "reasons": ["投资决策用途"]},
                        "estimated_cost": {"tokens_per_run": "high", "expected_duration": "15-25 minutes"},
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert result["plan_id"]
    assert result["plan_json"]["title"] == "Web3 全景研究计划"
    assert result["plan_json"]["steps"][0]["order"] == 1
    assert result["plan_json"]["handoff"]["target"] == "deep_research"
    assert result["plan_json"]["handoff"]["payload"]["question"] == "使用 deepresearch做一个web3的全景报告"
    assert service.calls
    call = service.calls[0]
    assert call["intent_type"] == "long_task"
    assert str(call["requested_by_user_id"]) == result["requested_by_user_id"]
    assert call["metadata_json"]["interactive_plan_mode"] is True
