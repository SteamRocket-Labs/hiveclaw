from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _request(arguments: dict, *, workspace: str | Path = "/tmp"):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="exit_plan_mode",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=None,
            workspace=Path(workspace),
            session_id="session-plan",
        ),
    )


class _PlanService:
    def __init__(self):
        self.calls = []
        self.generate_calls = []

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

    async def generate_plan(self, *, plan_id, fill):
        from app.services import plan_mode_core

        self.generate_calls.append({"plan_id": plan_id, "fill": fill})
        plan_json = {
            **fill,
            "schema": plan_mode_core.PLAN_SCHEMA,
            "intent_type": "in_session_execution",
        }
        # plan_id stays stable — the draft the launcher pre-created is filled.
        return SimpleNamespace(
            id=plan_id,
            status="awaiting_confirmation",
            plan_version=1,
            plan_hash=plan_mode_core.compute_plan_hash(plan_json),
            plan_json=plan_json,
            metadata_json={},
        )


class _FailingPlanService(_PlanService):
    async def ensure_awaiting_plan_from_fill(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            id=uuid4(),
            status="planning_failed",
            plan_version=1,
            plan_hash=None,
            plan_json={},
            metadata_json={
                "planning_errors": ["user-visible plan leaks internal workflow detail: load_skill"]
            },
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
            "original_request": "做一个web3的全景报告",
            "intent_type": "in_session_execution",
            "handoff_target": "continue_current_session",
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
    assert result["item_type"] == "plan_proposal"
    assert result["plan_id"]
    assert result["plan_json"]["title"] == "Web3 全景研究计划"
    assert result["plan_json"]["steps"][0]["order"] == 1
    assert result["plan_json"]["handoff"]["target"] == "continue_current_session"
    assert "execution_contract" not in result["plan_json"]
    assert service.calls
    call = service.calls[0]
    assert call["intent_type"] == "in_session_execution"
    assert str(call["requested_by_user_id"]) == result["requested_by_user_id"]
    assert call["metadata_json"]["interactive_plan_mode"] is True
    # No pre-armed plan_id → "create new" branch only; generate_plan untouched.
    assert service.generate_calls == []


@pytest.mark.asyncio
async def test_exit_plan_mode_returns_planning_failed_without_needs_plan_success_copy(monkeypatch):
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _FailingPlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "做一个web3的全景报告",
            "intent_type": "in_session_execution",
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Bad plan",
                        "objective": "Expose internals.",
                        "plan_markdown": "## Plan\nCall load_skill directly.",
                        "steps": ["Run load_skill"],
                        "success_criteria": ["Report is generated"],
                        "stop_conditions": ["User rejects"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "planning_failed"
    assert result["item_type"] == "plan_proposal"
    assert "等待用户确认" not in result["summary"]
    assert "confirm" not in result["next_action"].lower()
    assert "load_skill" in "\n".join(result["planning_errors"])


@pytest.mark.asyncio
async def test_exit_plan_mode_preserves_hidden_execution_contract_without_visible_leak(monkeypatch):
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    contract = {
        "type": "workflow",
        "workflow_ref": "custom_research.v1",
        "workflow_definition_hash": "hash-workflow",
        "args": {
            "question": "Web3 landscape",
            "internal_tool_note": "start_workflow writes runtime_artifacts/workflow_runs/run",
        },
    }
    token = set_interactive_plan_mode(
        {"active": True, "original_request": "Plan research", "intent_type": "in_session_execution"}
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Web3 research plan",
                        "objective": "Produce a source-grounded report.",
                        "plan_markdown": "## Plan\nUse approved research lanes and write a report.",
                        "steps": ["Confirm scope", "Collect sources", "Write report"],
                        "success_criteria": ["Report cites sources"],
                        "stop_conditions": ["User rejects"],
                        "execution_contract": contract,
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert result["plan_json"]["execution_contract"] == contract
    assert service.calls[0]["fill"]["execution_contract"] == contract


@pytest.mark.asyncio
async def test_exit_plan_mode_routes_agent_team_contract_to_agent_team_handoff(monkeypatch):
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    contract = {
        "type": "agent_team",
        "name": "Parity Review Team",
        "members": [{"name": "critic", "role": "Review implementation", "prompt": "Review the runtime."}],
    }
    token = set_interactive_plan_mode(
        {"active": True, "original_request": "Review with a team", "intent_type": "in_session_execution"}
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Team review plan",
                        "objective": "Review the parity work with a team.",
                        "plan_markdown": "## Plan\nUse a critic teammate for review.",
                        "steps": ["Create critic teammate", "Run review", "Merge result"],
                        "success_criteria": ["Critic output is visible"],
                        "stop_conditions": ["User rejects"],
                        "execution_contract": contract,
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert result["plan_json"]["handoff"]["target"] == "agent_team"
    assert result["plan_json"]["execution_contract"] == contract


@pytest.mark.asyncio
async def test_exit_plan_mode_preserves_metadata_execution_contract(monkeypatch):
    """A pre-armed model-authored contract in Plan Mode metadata must not be lost.

    The visible tool args may omit execution_contract when the runtime already
    carries the typed PlanModeState mirror; exit_plan_mode must still land that
    contract into plan_json and route the handoff to Agent Team.
    """
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    contract = {
        "type": "agent_team",
        "name": "Metadata Team",
        "members": [{"name": "researcher", "role": "Collect source evidence"}],
    }
    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "Use Agent Team for the report",
            "intent_type": "in_session_execution",
            "execution_contract": contract,
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Metadata team plan",
                        "objective": "Run the report with an Agent Team.",
                        "plan_markdown": "## Plan\nUse the approved team contract and produce the report.",
                        "steps": ["Create team", "Run member research", "Synthesize"],
                        "success_criteria": ["Team member session is visible"],
                        "stop_conditions": ["User rejects"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert result["plan_json"]["handoff"]["target"] == "agent_team"
    assert result["plan_json"]["execution_contract"] == contract
    assert service.calls[0]["fill"]["execution_contract"] == contract


@pytest.mark.asyncio
async def test_exit_plan_mode_rejects_blocking_open_questions(monkeypatch):
    """Open questions are not confirmable choices.

    The agent must use ask_user_question for blocking decisions instead of
    creating a PlanCard whose only user actions are confirm / modify / reject.
    """
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode(
        {"active": True, "original_request": "Use Agent Team for ABS report", "intent_type": "in_session_execution"}
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "ABS report plan",
                        "objective": "Produce the report.",
                        "plan_markdown": "## Plan\nNeed user choices before this is executable.",
                        "steps": ["Ask scope", "Run report"],
                        "success_criteria": ["Report completed"],
                        "stop_conditions": ["User rejects"],
                        "open_questions": [
                            "Which market should be prioritized?",
                            "Should this be Agent Team or single-agent?",
                        ],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "error"
    assert result["error_code"] == "blocking_open_questions"
    assert "ask_user_question" in result["message"]
    assert service.calls == []
    assert service.generate_calls == []


# ── cut ③a: dual-state submission (plan_id armed → fill existing draft) ──


@pytest.mark.asyncio
async def test_exit_plan_mode_fills_existing_draft_when_plan_id_armed(monkeypatch):
    """A system_plan_run launcher pre-arms Plan Mode with the draft's plan_id;
    exit_plan_mode must fill THAT draft via generate_plan(plan_id, fill) — keeping
    the id stable — instead of creating a new awaiting plan."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    armed_plan_id = str(uuid4())
    token = set_interactive_plan_mode(
        {
            "active": True,
            "plan_id": armed_plan_id,
            "original_request": "每天 9 点给我发 RWA 日报",
            "intent_type": "in_session_execution",
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "RWA 日报计划",
                        "objective": "每天生成一份 RWA 日报。",
                        "plan_markdown": "## Plan\n1. 收集来源。\n2. 汇总日报。",
                        "steps": ["收集来源", "汇总日报"],
                        "success_criteria": ["日报含 5-10 条带链接更新"],
                        "stop_conditions": ["用户拒绝计划"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    # The id returned is the pre-armed draft id (stable for the frontend).
    assert result["plan_id"] == armed_plan_id
    # Filled the existing draft, not a new awaiting plan.
    assert service.calls == []
    assert len(service.generate_calls) == 1
    gen = service.generate_calls[0]
    assert str(gen["plan_id"]) == armed_plan_id
    assert gen["fill"]["title"] == "RWA 日报计划"
    assert gen["fill"]["steps"][0]["order"] == 1


@pytest.mark.asyncio
async def test_exit_plan_mode_creates_new_when_plan_id_malformed(monkeypatch):
    """A malformed/empty plan_id degrades to the "create new" branch — it must
    never silently route into generate_plan with a bad id."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode(
        {
            "active": True,
            "plan_id": "not-a-uuid",
            "original_request": "帮我盯住这个仓位",
            "intent_type": "in_session_execution",
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "盯盘计划",
                        "objective": "盯住仓位并预警。",
                        "plan_markdown": "## Plan\n盯盘。",
                        "steps": ["设定阈值", "持续监控"],
                        "success_criteria": ["触发阈值时通知用户"],
                        "stop_conditions": ["用户取消"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    # Fell back to create-new: ensure_awaiting_plan_from_fill, not generate_plan.
    assert service.generate_calls == []
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_exit_plan_mode_lands_action_artifact_into_plan_json(monkeypatch):
    """When Plan Mode was entered from a blocked high-risk ``start_workflow``,
    the armed action_artifact must land in the submitted fill so the confirmed
    plan's ``plan_json["action_artifact"]`` satisfies the gate's binding check
    (P1: otherwise confirmation never unlocks the launch)."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    artifact = {"definition_hash": "wf-hash", "args_hash": "args-hash", "risk_reasons": ["external send"]}
    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "把周报发给客户",
            "intent_type": "in_session_execution",
            "action_kind": "start_workflow",
            "tool_name": "start_workflow",
            "action_artifact": artifact,
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "外发周报",
                        "objective": "审批后把周报发给客户。",
                        "plan_markdown": "## Plan\n1. 起草。\n2. 审批后外发。",
                        "steps": ["起草", "审批后外发"],
                        "success_criteria": ["客户收到周报"],
                        "stop_conditions": ["用户拒绝计划"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert result["plan_json"]["action_artifact"] == artifact
    assert service.calls[0]["fill"]["action_artifact"] == artifact


# ── CC alignment §4.1: plan_markdown is the body, not a discarded field ──


@pytest.mark.asyncio
async def test_exit_plan_mode_captures_plan_markdown_into_fill(monkeypatch):
    """The agent's plan_markdown article must land in the fill/plan_json — not be
    discarded. Canonical 偏离①: the schema required plan_markdown then the handler
    dropped it, so the card re-rendered from structured fields."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode(
        {"active": True, "original_request": "RWA 周报", "intent_type": "in_session_execution"}
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "RWA 周报",
                        "objective": "出周报",
                        "plan_markdown": "## 思路\n聚焦三条赛道。\n\n## 执行\n1. 核验来源。",
                        "steps": ["核验来源", "撰写周报"],
                        "success_criteria": ["含 5-10 条带链接更新"],
                        "stop_conditions": ["用户拒绝"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert "聚焦三条赛道" in result["plan_json"]["plan_markdown"]
    assert "聚焦三条赛道" in service.calls[0]["fill"]["plan_markdown"]


@pytest.mark.asyncio
async def test_exit_plan_mode_reads_markdown_from_provisioned_plan_file(tmp_path, monkeypatch):
    """MD-first submission: the long user-facing plan may live in the exact
    Plan Mode plan file instead of being serialized through JSON tool args.
    This avoids long Markdown escaping failures while keeping the governed
    plan_json hash-covered after submission."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    plan_file = "workspace/plans/session-plan.plan.md"
    absolute_plan_file = tmp_path / plan_file
    absolute_plan_file.parent.mkdir(parents=True)
    absolute_plan_file.write_text(
        "# 跨链桥技术架构报告计划\n\n"
        "## 目标\n"
        "先确认技术路径、项目架构、安全模型、跨链消息标准和趋势五个维度。\n\n"
        "## 执行顺序\n"
        "1. 只读核验来源。\n"
        "2. 写 Markdown 报告。\n",
        encoding="utf-8",
    )

    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "进入计划模式，做一个关于跨链桥的报告",
            "intent_type": "in_session_execution",
            "plan_file_path": plan_file,
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "跨链桥报告计划",
                        "objective": "产出技术架构型跨链桥报告。",
                        "steps": ["确认范围", "核验来源", "撰写报告"],
                        "success_criteria": ["PlanCard 展示 Markdown 计划", "报告完成后有来源 ledger"],
                        "stop_conditions": ["用户拒绝计划"],
                    },
                    workspace=tmp_path,
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert "跨链桥技术架构报告计划" in result["plan_json"]["plan_markdown"]
    assert "只读核验来源" in service.calls[0]["fill"]["plan_markdown"]


@pytest.mark.asyncio
async def test_exit_plan_mode_treats_provisioned_plan_file_as_authoritative(tmp_path, monkeypatch):
    """When Plan Mode provisions a writable plan file, that file is the approval
    artifact. A stale or fabricated plan_markdown argument must not override it."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    plan_file = "workspace/plans/authoritative.plan.md"
    absolute_plan_file = tmp_path / plan_file
    absolute_plan_file.parent.mkdir(parents=True)
    absolute_plan_file.write_text(
        "# 文件里的真实计划\n\n## 执行策略\n只读核验当前仓库状态，然后提交可确认计划。\n",
        encoding="utf-8",
    )

    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "进入计划模式，修复 Plan Mode",
            "intent_type": "in_session_execution",
            "plan_file_path": plan_file,
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Plan Mode 修复计划",
                        "objective": "补齐 Plan Mode 差距。",
                        "plan_markdown": "# 参数里的旧计划\n\n这段不应该进入 PlanCard。",
                        "steps": ["确认差距", "修改实现", "验证"],
                        "success_criteria": ["PlanCard 使用文件计划"],
                        "stop_conditions": ["用户拒绝计划"],
                    },
                    workspace=tmp_path,
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "needs_plan"
    assert "文件里的真实计划" in result["plan_json"]["plan_markdown"]
    assert "参数里的旧计划" not in result["plan_json"]["plan_markdown"]
    assert "文件里的真实计划" in service.calls[0]["fill"]["plan_markdown"]


@pytest.mark.asyncio
async def test_exit_plan_mode_requires_nonblank_provisioned_plan_file(tmp_path, monkeypatch):
    """A provisioned plan file narrows the writable/readable plan artifact to one
    exact path. If it is blank, exit_plan_mode must fail instead of accepting a
    separate markdown argument."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    plan_file = "workspace/plans/blank.plan.md"
    absolute_plan_file = tmp_path / plan_file
    absolute_plan_file.parent.mkdir(parents=True)
    absolute_plan_file.write_text("   \n", encoding="utf-8")

    token = set_interactive_plan_mode(
        {
            "active": True,
            "original_request": "进入计划模式，修复 Plan Mode",
            "intent_type": "in_session_execution",
            "plan_file_path": plan_file,
        }
    )
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "Plan Mode 修复计划",
                        "objective": "补齐 Plan Mode 差距。",
                        "plan_markdown": "# 参数里的计划\n\n不能绕过空文件。",
                        "steps": ["确认差距", "修改实现", "验证"],
                        "success_criteria": ["PlanCard 使用文件计划"],
                        "stop_conditions": ["用户拒绝计划"],
                    },
                    workspace=tmp_path,
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "error"
    assert result["error_code"] == "missing_plan_file_body"
    assert service.calls == []
    assert service.generate_calls == []


@pytest.mark.asyncio
async def test_exit_plan_mode_rejects_blank_plan_markdown(monkeypatch):
    """A blank plan_markdown means the agent filled fields without authoring a
    plan — reject (no plan row created) so it writes the real plan this turn."""
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.handlers import plan_mode as handler

    service = _PlanService()
    monkeypatch.setattr(handler, "get_plan_mode_service", lambda: service)

    token = set_interactive_plan_mode({"active": True, "original_request": "x", "intent_type": "in_session_execution"})
    try:
        result = json.loads(
            await handler.exit_plan_mode(
                _request(
                    {
                        "title": "t",
                        "objective": "o",
                        "plan_markdown": "   ",
                        "steps": ["a"],
                        "success_criteria": ["c"],
                        "stop_conditions": ["s"],
                    }
                )
            )
        )
    finally:
        reset_interactive_plan_mode(token)

    assert result["status"] == "error"
    assert result["error_code"] == "missing_plan_body"
    # No plan row created for an empty body — neither create nor fill path ran.
    assert service.calls == []
    assert service.generate_calls == []
