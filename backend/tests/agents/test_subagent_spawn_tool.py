"""Tests for the public spawn_subagent entry + its tool handler (cut ②).

Style mirrors test_subagent.py: inject a fake invoke / monkeypatch the DB-bound
resolver so no real DB or LLM is touched.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.subagent import (
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SubagentHandle,
    SubagentResult,
    SubagentSpawnContext,
    SubagentSpec,
    explorer_spec,
    spawn_subagent,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def _spawn_ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _ok_invoke(content: str = "done", tokens: int = 5):
    async def invoke(_request):
        return SimpleNamespace(content=content, tokens_used=tokens)

    return invoke


def _tool_request(
    arguments: dict,
    *,
    session_id: str | None = "sess-1",
    tenant_id: str | None = None,
    round_state: dict | None = None,
) -> ToolExecutionRequest:
    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        workspace=Path("/tmp"),
        session_id=session_id,
        round_state=round_state,
    )
    return ToolExecutionRequest(tool_name="spawn_subagent", arguments=arguments, context=context)


# --- public spawn_subagent entry --------------------------------------------


@pytest.mark.asyncio
async def test_spawn_subagent_returns_resolved_handle():
    ctx = _spawn_ctx(trace_id="tr-9", depth=1)
    handle = await spawn_subagent(ctx, explorer_spec("scout"), "task", invoke=_ok_invoke(content="done"))
    assert handle.name == "scout"
    assert handle.trace_id == "tr-9"
    assert handle.depth == 2  # ctx.depth + 1
    assert handle.result is not None
    assert handle.result.ok
    assert handle.result.content == "done"


@pytest.mark.asyncio
async def test_spawn_subagent_depth_limit_yields_failed_handle():
    ctx = _spawn_ctx(depth=2, max_depth=2)  # child depth 3 > 2
    handle = await spawn_subagent(ctx, explorer_spec("scout"), "task", invoke=_ok_invoke())
    assert handle.result is not None
    assert handle.result.status == "depth_limited"


# --- spawn_subagent tool handler --------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_requires_task():
    import app.tools.handlers.subagent as handler_mod

    out = await handler_mod.spawn_subagent_tool(_tool_request({}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "prompt or task" in data["error"]


@pytest.mark.asyncio
async def test_spawn_tool_reports_missing_model(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    async def fake_resolve(agent_id):
        return None, None, None

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t"}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "No model" in data["error"]


@pytest.mark.asyncio
async def test_spawn_tool_resolves_model_and_spawns(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type="explorer", status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "investigate", "name": "scout", "max_tool_rounds": 5})
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["content"] == "digest"
    assert data["tokens_used"] == 7
    assert captured["task"] == "investigate"
    assert captured["spec"].name == "scout"
    assert captured["spec"].max_tool_rounds == 5
    assert captured["ctx"].parent_session_id == "sess-1"
    assert captured["ctx"].parent_agent_name == "HR"


@pytest.mark.asyncio
async def test_spawn_tool_returns_execution_shape_recommendation(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type="explorer", status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request(
            {"task": "run fixed ordered steps", "name": "scout"}, round_state={"execution_shape": "fixed_sequence"}
        )
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["execution_shape_decision"]["tool_name"] == "spawn_subagent"
    assert data["execution_shape_decision"]["execution_shape"] == "fixed_sequence"
    assert data["execution_shape_decision"]["recommendation"] == "use_dynamic_workflow"
    assert data["execution_shape_decision"]["severity"] == "warning"


@pytest.mark.asyncio
async def test_spawn_tool_foreground_returns_child_session_continuation(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_create_child_session(**kwargs):
        captured["create_child_session"] = kwargs
        return "child-session"

    async def fake_update_child_session_state(**kwargs):
        captured["update_child_session"] = kwargs

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        captured["kwargs"] = kwargs
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    async def fake_active_agent_team_contract(_request):
        return None

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "active_agent_team_contract_from_tool_request", fake_active_agent_team_contract)
    monkeypatch.setattr(handler_mod, "create_subagent_child_session", fake_create_child_session, raising=False)
    monkeypatch.setattr(
        handler_mod,
        "update_subagent_child_session_state",
        fake_update_child_session_state,
        raising=False,
    )
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    parent_session_id = str(uuid.uuid4())
    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "investigate", "name": "scout"}, session_id=parent_session_id)
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["mode"] == "foreground"
    assert data["child_session_id"] == "child-session"
    assert data["return_contract"] == "inline_result"
    assert data["subagent_return_contract"]["schema"] == "hive.ccplus.subagent_return_contract.v1"
    assert data["subagent_return_contract"]["return_contract"] == "inline_result"
    assert data["subagent_return_contract"]["result_visibility"] == "current_tool_result"
    assert data["subagent_return_contract"]["busy_poll_allowed"] is False
    assert data["continuation"]["address"] == "child-session"
    assert data["continuation"]["tool"] == "send_agent_session_message"
    assert data["transcript_refs"]["session_id"] == "child-session"
    assert captured["ctx"].child_session_id == "child-session"
    assert captured["create_child_session"]["parent_session_id"] == parent_session_id
    assert captured["update_child_session"]["status"] == "completed"


@pytest.mark.asyncio
async def test_spawn_tool_foreground_uses_execution_admission_and_settles(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services.runtime_budget_service import RuntimeBudgetReservationResult

    captured: dict = {}
    budget_run_id = uuid.uuid4()

    async def fake_resolve(_agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="done", tokens_used=7),
        )

    async def fake_active_agent_team_contract(_request):
        return None

    class BudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=False,
                idempotent=False,
                budget_run_id=reservation.budget_run_id,
            )

        async def settle(self, settlement):
            captured["settlement"] = settlement

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "active_agent_team_contract_from_tool_request", fake_active_agent_team_contract)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)
    monkeypatch.setattr(handler_mod, "RuntimeBudgetService", BudgetService)
    request = _tool_request({"task": "investigate", "name": "scout"}, session_id=None)
    request.context.budget_run_id = str(budget_run_id)

    data = json.loads(await handler_mod.spawn_subagent_tool(request))

    assert data["ok"] is True
    assert captured["reservation"].budget_run_id == budget_run_id
    assert captured["reservation"].subagents == 1
    assert captured["reservation"].background_tasks == 0
    assert captured["settlement"].actual_subagents == 1
    assert captured["settlement"].actual_tokens == 7


@pytest.mark.asyncio
async def test_spawn_tool_foreground_releases_admission_when_spawn_raises(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services.runtime_budget_service import RuntimeBudgetReservationResult

    captured: dict = {}
    budget_run_id = uuid.uuid4()

    async def fake_resolve(_agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def failing_spawn(*_args, **_kwargs):
        raise RuntimeError("spawn failed before result")

    async def fake_active_agent_team_contract(_request):
        return None

    class BudgetService:
        async def reserve(self, reservation):
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=False,
                idempotent=False,
                budget_run_id=reservation.budget_run_id,
            )

        async def settle(self, settlement):
            captured["settlement"] = settlement

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "active_agent_team_contract_from_tool_request", fake_active_agent_team_contract)
    monkeypatch.setattr(handler_mod, "spawn_subagent", failing_spawn)
    monkeypatch.setattr(handler_mod, "RuntimeBudgetService", BudgetService)
    request = _tool_request({"task": "investigate", "name": "scout"}, session_id=None)
    request.context.budget_run_id = str(budget_run_id)

    with pytest.raises(RuntimeError, match="spawn failed before result"):
        await handler_mod.spawn_subagent_tool(request)

    assert captured["settlement"].actual_subagents == 1
    assert captured["settlement"].actual_tokens == 0
    assert captured["settlement"].reason == "foreground_subagent_failed"


@pytest.mark.asyncio
async def test_agent_session_followup_admission_exposes_approval_wait(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    budget_run_id = uuid.uuid4()

    class WaitingBudgetService:
        async def reserve(self, reservation):
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["continuation_wakes"],
            )

    monkeypatch.setattr(handler_mod, "RuntimeBudgetService", WaitingBudgetService)

    admission_pair = await handler_mod._reserve_agent_session_message_budget(
        budget_run_id=str(budget_run_id),
        child_session_id=uuid.uuid4(),
        parent_session_id=str(uuid.uuid4()),
    )

    assert admission_pair is not None
    _admission, decision = admission_pair
    assert decision.status == "waiting_budget_approval"
    assert decision.user_message == "运行额度已达上限，已请求管理员批准；当前工作尚未执行。"


@pytest.mark.asyncio
async def test_spawn_tool_permission_profile_narrows_child_allowed_tools(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["spec"] = spec
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request(
            {
                "task": "use the loaded research skill",
                "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
            }
        )
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert captured["spec"].allowed_tools == ("web_search", "read_file")


@pytest.mark.asyncio
async def test_spawn_tool_accepts_agenttool_prompt_alias_and_defaults_general_purpose(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        captured["kwargs"] = kwargs
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(
                name=spec.name,
                type=spec.type,
                status="completed",
                content="digest",
                tokens_used=7,
            ),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"prompt": "investigate"}))
    data = json.loads(out)

    assert data["ok"] is True
    assert data["type"] == SUBAGENT_TYPE_GENERAL_PURPOSE
    assert captured["task"] == "investigate"
    assert captured["spec"].type == SUBAGENT_TYPE_GENERAL_PURPOSE
    assert captured["spec"].name == SUBAGENT_TYPE_GENERAL_PURPOSE


@pytest.mark.asyncio
async def test_spawn_tool_canonical_subagent_type_overrides_legacy_type_alias(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["spec"] = spec
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"prompt": "verify this", "type": "explorer", "subagent_type": "critic"})
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["type"] == "critic"
    assert captured["spec"].type == "critic"


@pytest.mark.asyncio
async def test_spawn_tool_wires_llm_memory_distiller(monkeypatch):
    """The self-evolution loop must be LIVE in production: when a memory store
    exists, the spawn context carries an LLM How-distiller bound to the
    parent's model — without it, 记忆.md is read-only forever."""

    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        model = SimpleNamespace(provider="openai", api_key="k", model="m", base_url=None)
        return model, None, SimpleNamespace(name="HR")

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t"}, tenant_id=str(uuid.uuid4())))
    assert json.loads(out)["ok"] is True
    assert captured["ctx"].memory_store is not None
    assert captured["ctx"].memory_distiller is not None


@pytest.mark.asyncio
async def test_spawn_tool_disables_memory_writeback_inside_interactive_plan_mode(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode

    captured: dict = {}

    async def fake_resolve(agent_id):
        model = SimpleNamespace(provider="openai", api_key="k", model="m", base_url=None)
        return model, None, SimpleNamespace(name="Planner")

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    token = set_interactive_plan_mode({"original_request": "plan", "plan_file_path": "workspace/plans/p.md"})
    try:
        out = await handler_mod.spawn_subagent_tool(
            _tool_request({"task": "inspect current implementation", "type": "explorer"}, tenant_id=str(uuid.uuid4()))
        )
    finally:
        reset_interactive_plan_mode(token)

    assert json.loads(out)["ok"] is True
    assert captured["spec"].type == "explorer"
    assert captured["spec"].has_own_memory is False
    assert captured["ctx"].memory_store is None
    assert captured["ctx"].memory_distiller is None
    assert captured["kwargs"].get("run_in_background", False) is False


@pytest.mark.asyncio
async def test_spawn_tool_inline_type_selects_builtin(monkeypatch):
    """CC parity: all three builtin types are reachable from the spawn surface."""

    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["spec"] = spec
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "verify this", "type": "critic"}))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["type"] == "critic"
    assert captured["spec"].type == "critic"
    assert captured["spec"].name == "critic"  # name defaults to the type


@pytest.mark.asyncio
async def test_spawn_tool_rejects_unknown_inline_type(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t", "type": "bogus"}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "bogus" in data["error"]
    assert "explorer" in data["error"]  # lists the valid builtin types


@pytest.mark.asyncio
async def test_spawn_tool_can_load_persistent_definition(monkeypatch, tmp_path):
    import app.tools.handlers.subagent as handler_mod
    from app.agents.subagent_definition import definition_store_for_tenant
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    tenant_id = str(uuid.uuid4())
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="critic-def",
            description="Persistent critic for diff review.",
            type="critic",
            allowed_tools=("read_file",),
            max_tool_rounds=3,
            system_prompt="Persistent critic prompt.",
        )
    )
    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "review", "definition_name": "critic-def"}, tenant_id=tenant_id)
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "tenant"
    assert captured["task"] == "review"
    assert captured["spec"].name == "critic-def"
    assert captured["spec"].type == "critic"
    assert captured["spec"].allowed_tools == ("read_file",)
    assert captured["spec"].system_prompt == "Persistent critic prompt."
    assert captured["ctx"].memory_store is not None


@pytest.mark.asyncio
async def test_spawn_tool_can_select_persistent_definition_with_subagent_type(monkeypatch, tmp_path):
    import app.tools.handlers.subagent as handler_mod
    from app.agents.subagent_definition import definition_store_for_tenant
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    tenant_id = str(uuid.uuid4())
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="code-reviewer",
            description="Use for independent code review.",
            type="critic",
            allowed_tools=("read_file", "grep_search"),
            max_tool_rounds=5,
            system_prompt="Persistent code reviewer prompt.",
        )
    )
    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        captured["kwargs"] = kwargs
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "review", "subagent_type": "code-reviewer"}, tenant_id=tenant_id)
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "tenant"
    assert data["subagent"] == "code-reviewer"
    assert data["type"] == "critic"
    assert captured["task"] == "review"
    assert captured["spec"].name == "code-reviewer"
    assert captured["spec"].type == "critic"
    assert captured["spec"].allowed_tools == ("read_file", "grep_search")
    assert captured["spec"].system_prompt == "Persistent code reviewer prompt."


# --- C1 (§12.4): scope resolution chain in the spawn tool --------------------


def _scoped_spawn_setup(monkeypatch, tmp_path):
    """Wire the handler to a tmp AGENT_DATA_DIR with real stores; capture spawn."""

    import app.tools.handlers.subagent as handler_mod
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)
    return handler_mod, captured


@pytest.mark.asyncio
async def test_spawn_tool_agent_definition_wins_and_memory_follows(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "dup"}, tenant_id=tenant_id)
    agent_id = request.context.agent_id

    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="tenant version")
    )
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="agent version")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "agent"
    assert captured["spec"].system_prompt == "agent version"
    # §12.5: memory follows the definition's scope — agent-private store.
    mem_base = str(captured["ctx"].memory_store.base_dir)
    assert f"{agent_id}/subagents/.memory" in mem_base.replace("\\", "/")


@pytest.mark.asyncio
async def test_spawn_tool_tenant_fallback_memory_stays_tenant(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_tenant

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "shared"}, tenant_id=tenant_id)

    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="shared", description="d", type="explorer", system_prompt="tenant shared")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "tenant"
    assert captured["spec"].system_prompt == "tenant shared"
    mem_base = str(captured["ctx"].memory_store.base_dir)
    assert "_tenants" in mem_base and "/subagents/memory" in mem_base.replace("\\", "/")


@pytest.mark.asyncio
async def test_spawn_tool_definition_not_found_lists_both_scopes(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant

    handler_mod, _captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "ghost"}, tenant_id=tenant_id)
    agent_id = request.context.agent_id

    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="ours", description="d", type="critic", system_prompt="tenant def")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is False
    assert "ghost" in data["error"]
    available = {row["name"]: row["scope"] for row in data["available"]}
    assert available["mine"] == "agent"
    assert available["ours"] == "tenant"
    # Builtin template rows included so the model can self-correct to inline spawn.
    assert available["explorer"] == "builtin"
    # CC parity: every row carries its whenToUse so the model can pick by purpose.
    descriptions = {row["name"]: row["description"] for row in data["available"]}
    assert descriptions["mine"] == "d"
    assert descriptions["explorer"]  # builtin whenToUse is non-empty


@pytest.mark.asyncio
async def test_spawn_tool_agent_definition_resolves_without_tenant(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    request = _tool_request({"task": "scout", "definition_name": "mine"}, tenant_id=None)
    agent_id = request.context.agent_id

    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "agent"
    assert captured["spec"].system_prompt == "agent def"


@pytest.mark.asyncio
async def test_spawn_tool_background_returns_child_session_and_wake_first_contract(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services import subagent_run_service as run_svc

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return run_svc.SubagentRunStart(run_id="run-1", child_session_id="child-session")

    def fake_completer(_run_id):
        raise AssertionError("background tool path must enqueue only; worker builds the completion callback")

    async def fake_spawn(*_args, **_kwargs):
        raise AssertionError("background tool path must not spawn in the API/request process")

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(run_svc, "start_subagent_run", fake_start)
    monkeypatch.setattr(run_svc, "make_run_completer", fake_completer)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "investigate", "name": "scout", "run_in_background": True})
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["mode"] == "background"
    assert data["run_id"] == "run-1"
    assert data["child_session_id"] == "child-session"
    assert data["return_contract"] == "background_completion_wake"
    assert data["subagent_return_contract"]["schema"] == "hive.ccplus.subagent_return_contract.v1"
    assert data["subagent_return_contract"]["return_contract"] == "background_completion_wake"
    assert data["subagent_return_contract"]["normal_wait_path"] == "completion_wake"
    assert data["subagent_return_contract"]["fallback_tool"] == "check_subagent"
    assert data["subagent_return_contract"]["busy_poll_allowed"] is False
    assert data["status"] == "queued"
    assert data["session_state"] == "queued"
    assert data["continuation"]["address"] == "child-session"
    assert data["continuation"]["tool"] == "send_agent_session_message"
    assert "wait for the completion wake" in data["message"]
    assert "poll" not in data["message"].lower()
    assert captured["start"]["parent_user_id"] is not None
    assert captured["start"]["parent_session_id"] == "sess-1"
    assert captured["start"]["context_mode"] == "none"


@pytest.mark.asyncio
async def test_check_subagent_returns_child_session_refs_and_fallback_language(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services import subagent_run_service as run_svc

    owner = uuid.uuid4()

    async def fake_get(run_id, parent_agent_id):
        assert run_id == "run-1"
        assert parent_agent_id == owner
        return {
            "task_type": "subagent",
            "parent_agent_id": str(owner),
            "child_agent_name": "scout",
            "status": "completed",
            "result": "",
            "result_summary": "done",
            "child_session_id": "child-session",
            "metadata": {
                "subagent_name": "scout",
                "subagent_type": "explorer",
                "child_session_id": "child-session",
                "session_contract": {
                    "kind": "subagent_child_session",
                    "continuation_address": "child-session",
                },
            },
        }

    monkeypatch.setattr(run_svc, "get_subagent_run", fake_get)

    out = await handler_mod.check_subagent(owner, {"run_id": "run-1"})
    data = json.loads(out)

    assert data["ok"] is True
    assert data["run_id"] == "run-1"
    assert data["child_session_id"] == "child-session"
    assert data["return_contract"] == "background_completion_wake"
    assert data["subagent_return_contract"]["schema"] == "hive.ccplus.subagent_return_contract.v1"
    assert data["subagent_return_contract"]["return_contract"] == "background_completion_wake"
    assert data["subagent_return_contract"]["normal_wait_path"] == "completion_wake"
    assert data["session_state"]["status"] == "completed"
    assert data["transcript_refs"]["session_id"] == "child-session"
    assert data["continuation"]["address"] == "child-session"
    assert data["result"] == "done"
    assert data["model_guidance"] == "fallback_inspection_only"


@pytest.mark.asyncio
async def test_send_agent_session_message_appends_child_mailbox_event(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    class _ScalarResult:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

    class _FakeDB:
        def __init__(self, row):
            self.row = row
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.row)

        async def commit(self):
            self.commits += 1

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    fake_session = SimpleNamespace(
        id=child_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        parent_session_id=parent_session_id,
        root_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
    )
    fake_db = _FakeDB(fake_session)
    captured: dict = {}

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "queued", "consumer": "mid_run_message_drain", "run_id": "run-1"}

    monkeypatch.setattr(handler_mod, "tenant_scoped_session", lambda _tenant_id: fake_db)
    monkeypatch.setattr(handler_mod, "continue_agent_session_from_mailbox", fake_continue_agent_session_from_mailbox)

    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(child_session_id), "message": "please inspect the new evidence"},
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("/tmp"),
            session_id=str(parent_session_id),
        ),
    )

    out = await handler_mod.send_agent_session_message(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["child_session_id"] == str(child_session_id)
    assert data["status"] == "queued"
    assert data["consumer"] == "mid_run_message_drain"
    assert captured["session"] is fake_session
    assert captured["message"] == "please inspect the new evidence"
    assert captured["parent_session_id"] == str(parent_session_id)


@pytest.mark.asyncio
async def test_send_agent_session_message_budget_denial_does_not_append_mailbox(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.services.runtime_budget_service import RuntimeBudgetDenied

    class _ScalarResult:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(fake_session)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    budget_run_id = uuid.uuid4()
    fake_session = SimpleNamespace(
        id=child_session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        parent_session_id=uuid.uuid4(),
        root_session_id=uuid.uuid4(),
        visibility_scope="team",
        listed_surface="parent",
        transcript_metadata_json={"budget_run_id": str(budget_run_id)},
    )

    class DenyingBudgetService:
        async def reserve(self, reservation):
            assert reservation.budget_run_id == budget_run_id
            assert reservation.continuation_wakes == 1
            raise RuntimeBudgetDenied("runtime budget exhausted", budget_run_id=reservation.budget_run_id)

    async def fail_continue_agent_session_from_mailbox(**_kwargs):
        raise AssertionError("budget-denied send_agent_session_message must not append mailbox")

    monkeypatch.setattr(handler_mod, "tenant_scoped_session", lambda _tenant_id: _FakeDB())
    monkeypatch.setattr(handler_mod, "RuntimeBudgetService", DenyingBudgetService)
    monkeypatch.setattr(handler_mod, "continue_agent_session_from_mailbox", fail_continue_agent_session_from_mailbox)

    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(child_session_id), "message": "continue"},
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("/tmp"),
            session_id=str(uuid.uuid4()),
            budget_run_id=str(budget_run_id),
        ),
    )

    out = await handler_mod.send_agent_session_message(request)
    data = json.loads(out)

    assert data["ok"] is False
    assert data["error_code"] == "runtime_budget_denied"


@pytest.mark.asyncio
async def test_send_agent_session_message_accepts_a2a_peer_child_session(monkeypatch):
    import app.tools.handlers.subagent as handler_mod
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User

    def _criterion_has_column_value(expr, column_name: str, value) -> bool:
        if getattr(expr, "clauses", None) is not None:
            return any(_criterion_has_column_value(clause, column_name, value) for clause in expr.clauses)
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        if getattr(left, "name", None) != column_name:
            return False
        return getattr(right, "value", None) == value

    def _statement_has_column_value(stmt, column_name: str, value) -> bool:
        return any(
            _criterion_has_column_value(criteria, column_name, value)
            for criteria in getattr(stmt, "_where_criteria", ())
        )

    class _ScalarResult:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

    source_agent_id = uuid.uuid4()
    target_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    fake_session = SimpleNamespace(
        id=child_session_id,
        agent_id=target_agent_id,
        peer_agent_id=source_agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        parent_session_id=parent_session_id,
        root_session_id=parent_session_id,
        source_channel="agent",
        session_kind="delegation_run",
        visibility_scope="agent_owner",
        listed_surface="chat",
    )
    target_agent = SimpleNamespace(id=target_agent_id, name="Target")
    user = SimpleNamespace(id=user_id)

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            entity = stmt.column_descriptions[0].get("entity")
            if entity is ChatSession:
                assert _statement_has_column_value(stmt, "id", child_session_id)
                assert _statement_has_column_value(stmt, "peer_agent_id", source_agent_id)
                assert _statement_has_column_value(stmt, "source_channel", "agent")
                return _ScalarResult(fake_session)
            if entity is Agent:
                assert _statement_has_column_value(stmt, "id", target_agent_id)
                return _ScalarResult(target_agent)
            if entity is User:
                assert _statement_has_column_value(stmt, "id", user_id)
                return _ScalarResult(user)
            return _ScalarResult(None)

    captured: dict = {}

    async def fake_continue_agent_session_from_mailbox(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "queued", "consumer": "mid_run_message_drain", "run_id": "run-a2a"}

    monkeypatch.setattr(handler_mod, "tenant_scoped_session", lambda _tenant_id: _FakeDB())
    monkeypatch.setattr(handler_mod, "continue_agent_session_from_mailbox", fake_continue_agent_session_from_mailbox)

    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(child_session_id), "message": "continue from the peer handoff"},
        context=ToolExecutionContext(
            agent_id=source_agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("/tmp"),
            session_id=str(parent_session_id),
        ),
    )

    out = await handler_mod.send_agent_session_message(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["child_session_id"] == str(child_session_id)
    assert captured["agent"] is target_agent
    assert captured["session"] is fake_session
    assert captured["message"] == "continue from the peer handoff"


@pytest.mark.asyncio
async def test_send_agent_session_message_routes_agent_team_by_name_without_child_session(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    calls = []

    async def fake_team_message(request):
        calls.append(request.arguments)
        return {
            "ok": True,
            "team_id": request.arguments["team_id"],
            "member_name": request.arguments["member_name"],
            "message_count": 1,
            "results": [{"member_name": "critic", "status": "queued", "child_session_id": "child-session-1"}],
        }

    monkeypatch.setattr(handler_mod, "send_agent_team_message_from_tool_request", fake_team_message, raising=False)

    team_id = uuid.uuid4()
    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"team_id": str(team_id), "member_name": "critic", "message": "check the prompt routing"},
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=Path("/tmp"),
            session_id=str(uuid.uuid4()),
        ),
    )

    out = await handler_mod.send_agent_session_message(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["team_id"] == str(team_id)
    assert data["results"][0]["member_name"] == "critic"
    assert calls and calls[0]["member_name"] == "critic"


# ── T1.3 (§8.1 #5) — ledger_todo_id exposed on the spawn contract ──


def test_spawn_parameters_expose_ledger_todo_id():
    """The service entry has accepted ``ledger_todo_id`` since 切口③ (stamp on
    spawn, write-back on completion); T1.3 exposes it on the tool schema."""
    import app.tools.handlers.subagent as handler_mod

    properties = handler_mod._SPAWN_PARAMETERS["properties"]
    assert "ledger_todo_id" in properties
    assert properties["ledger_todo_id"]["type"] == "string"


@pytest.mark.asyncio
async def test_spawn_tool_threads_ledger_todo_id(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ledger_todo_id"] = kwargs.get("ledger_todo_id")
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type="explorer", status="completed", content="d", tokens_used=1),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "investigate", "ledger_todo_id": "todo-42"}))
    assert json.loads(out)["ok"] is True
    assert captured["ledger_todo_id"] == "todo-42"
