"""Tool handler for ``spawn_subagent`` — lets an agent spawn a lightweight worker.

Cut ② (docs/subagent-source-capability.md §5.1 / §5.2.1): the public single-worker
spawn entry, exposed as an LLM-callable tool. Serves ONLY lightweight workers
(术语边界 invariant); peer delegation to a standalone digital employee stays on
``delegate_to_agent``.

The calling agent's model is resolved here because the tool layer cannot see it
through ``ToolExecutionContext`` — this mirrors ``RuntimeResearchWorker._resolve_models``.
``delegation_token`` / ``tool_executor`` are left ``None``: the spawned worker runs
on the kernel's default governed tool path, so governance still applies.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from app.agents.subagent import (
    _TYPE_PRESETS,
    SUBAGENT_TYPE_EXPLORER,
    SubagentSpawnContext,
    SubagentSpec,
    spawn_subagent,
)
from app.agents.subagent_definition import (
    SCOPE_AGENT,
    list_subagent_definitions,
    resolve_subagent_definition,
    validate_subagent_name,
)
from app.agents.subagent_memory import make_llm_how_distiller, memory_store_for_agent, memory_store_for_tenant
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _resolve_parent_runtime(
    agent_id: uuid.UUID,
) -> tuple[Any | None, Any | None, Any | None]:
    """Resolve the calling agent's primary/fallback model (mirrors worker._resolve_models)."""

    # RLS 阶段1: bootstrap read of the whole agent row by PK to learn its tenant
    # and model ids; an audited single-row bypass breaks the chicken-and-egg
    # (we hold only agent_id, and `agents` fails closed under a non-owner role).
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason=f"subagent parent-runtime resolution for agent {agent_id}"),
    ):
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None:
            return None, None, None
        model = None
        fallback_model = None
        if agent.primary_model_id:
            model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.primary_model_id,
                        LLMModel.tenant_id == agent.tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if agent.fallback_model_id:
            fallback_model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id,
                        LLMModel.tenant_id == agent.tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return model or fallback_model, fallback_model, agent


async def _resolve_model_override(model_name: str, tenant_id: uuid.UUID | None) -> Any | None:
    """Resolve a persistent subagent definition's model override within the tenant model pool."""

    value = str(model_name or "").strip()
    if not value:
        return None
    # RLS 阶段1: `llm_models` is policy-bearing — scope by the tenant arg the
    # caller already holds (subagent definition's tenant). None pins an empty
    # GUC (fail-closed) which is the same safe default as get_db().
    async with tenant_scoped_session(tenant_id) as db:
        base_filters: list[Any] = [LLMModel.enabled.is_(True)]
        if tenant_id is not None:
            base_filters.append(LLMModel.tenant_id == tenant_id)

        try:
            model_id = uuid.UUID(value)
        except ValueError:
            model_id = None
        if model_id is not None:
            return (
                await db.execute(select(LLMModel).where(LLMModel.id == model_id, *base_filters))
            ).scalar_one_or_none()

        return (
            await db.execute(
                select(LLMModel).where(
                    *base_filters,
                    (LLMModel.label == value) | (LLMModel.model == value),
                )
            )
        ).scalar_one_or_none()


_SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "The self-contained task for the spawned subagent. It runs in isolation with only "
                "this task as context and returns a conclusion digest — not its intermediate steps."
            ),
        },
        "type": {
            "type": "string",
            "enum": ["explorer", "worker", "critic"],
            "description": (
                "Built-in subagent type for inline spawns (ignored when definition_name is set). "
                "Defaults to 'explorer'."
            ),
        },
        "name": {
            "type": "string",
            "description": "Optional short name for the subagent (e.g. 'market-scout'). Defaults to the type name.",
        },
        "definition_name": {
            "type": "string",
            "description": (
                "Optional persistent subagent definition name, resolved agent-scope first "
                "(your workspace subagents/<name>.md), then tenant shared library. When set, the stored "
                "定义.md contract controls type, tools, model, rounds, isolation, and system prompt."
            ),
        },
        "max_tool_rounds": {
            "type": "integer",
            "description": "Optional cap on the subagent's tool rounds (default 8).",
        },
        "run_in_background": {
            "type": "boolean",
            "description": (
                "When true, fire-and-forget: returns a run_id immediately instead of waiting for the "
                "result, so you can keep working and spawn more. Poll the result later with "
                "check_subagent(run_id). Default false (run to completion and return the digest now)."
            ),
        },
        "ledger_todo_id": {
            "type": "string",
            "description": (
                "Optional id of your work-ledger todo this subagent serves: spawn stamps the "
                "subagent as the todo's owner and completion writes the terminal status back."
            ),
        },
    },
    "required": ["task"],
}


@tool(
    ToolMeta(
        name="spawn_subagent",
        description=(
            "Spawn a lightweight subagent to handle one self-contained task in isolation and return a "
            "conclusion digest. Use this to parallelize work or keep a noisy sub-investigation out of "
            "your own context. Built-in types: "
            "'explorer' — fast READ-ONLY reconnaissance over files and the web; use for finding files, "
            "searching content, and answering questions about a large body of material. "
            "'worker' — general-purpose agent that can read AND edit workspace files; use to complete "
            "one well-scoped multi-step task end to end. "
            "'critic' — read-only verification specialist; pass it the original task plus the claims or "
            "artifacts to check and it returns a PASS/FAIL/PARTIAL verdict with evidence. "
            "Named definitions (via definition_name) override type, tools, model, and prompt from their "
            "stored 定义.md. Subagents cannot delegate or spawn further and run under the same governance "
            "as you. To hand work to another standalone digital employee, use delegate_to_agent instead. "
            "If the step ORDER itself is the requirement (fixed sequence, mid-run approval gates, "
            "budgeted fan-out), use preview_workflow/start_workflow instead of spawning."
        ),
        parameters=_SPAWN_PARAMETERS,
        category="coordination",
        display_name="Spawn Subagent",
        icon="🧬",
        governance="sensitive",
        adapter="request",
    )
)
async def spawn_subagent_tool(request: ToolExecutionRequest) -> str:
    task = str(request.arguments.get("task") or "").strip()
    if not task:
        return _json({"ok": False, "error": "task is required"})

    from app.services.plan_mode_runtime_context import interactive_plan_mode_active
    from app.tools.plan_mode_policy import is_plan_mode_tool_allowed

    plan_mode_active = interactive_plan_mode_active()
    if plan_mode_active and not is_plan_mode_tool_allowed("spawn_subagent", request.arguments):
        return _json(
            {
                "ok": False,
                "error": (
                    "Interactive Plan Mode only allows synchronous inline explorer/critic subagents. "
                    "Workers, background runs, persistent definitions, and ledger ownership require plan approval first."
                ),
            }
        )

    agent_id = request.context.agent_id
    model, fallback_model, agent = await _resolve_parent_runtime(agent_id)
    if model is None or agent is None:
        return _json({"ok": False, "error": "No model or agent available for spawning a subagent"})

    tenant_id: uuid.UUID | None = None
    raw_tenant = request.context.tenant_id
    if raw_tenant:
        try:
            tenant_id = uuid.UUID(str(raw_tenant))
        except ValueError:
            tenant_id = None

    definition_name = str(request.arguments.get("definition_name") or "").strip()
    raw_rounds = request.arguments.get("max_tool_rounds")
    max_tool_rounds = int(raw_rounds) if isinstance(raw_rounds, int) else None

    definition_scope: str | None = None
    if definition_name:
        try:
            resolved = resolve_subagent_definition(definition_name, agent_id=agent_id, tenant_id=tenant_id)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if resolved is None:
            # §12.4: attach the merged available list (agent + tenant + builtin
            # template rows) with their whenToUse descriptions so the model can
            # self-correct — builtins spawn via inline `type`, not definition_name.
            available = list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id)
            return _json(
                {
                    "ok": False,
                    "error": (
                        f"subagent definition {definition_name!r} not found in agent or tenant scope. "
                        "Builtin types (scope=builtin) are inline templates: spawn them via 'type' without "
                        "definition_name."
                    ),
                    "available": [
                        {"name": row["name"], "scope": row["scope"], "description": row.get("description", "")}
                        for row in available
                    ],
                }
            )
        spec = resolved.spec
        definition_scope = resolved.scope
    else:
        subagent_type = str(request.arguments.get("type") or SUBAGENT_TYPE_EXPLORER).strip()
        if subagent_type not in _TYPE_PRESETS:
            return _json(
                {
                    "ok": False,
                    "error": (
                        f"unknown builtin subagent type {subagent_type!r}; expected one of {sorted(_TYPE_PRESETS)}"
                    ),
                }
            )
        name = str(request.arguments.get("name") or subagent_type).strip() or subagent_type
        try:
            name = validate_subagent_name(name)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        spec = SubagentSpec(
            name=name,
            type=subagent_type,
            max_tool_rounds=max_tool_rounds,
            has_own_memory=not plan_mode_active,
        )

    # §12.5: memory follows the definition's scope — agent-private definitions
    # accumulate craft in the agent workspace; tenant definitions (and inline
    # specs, unchanged) share the tenant store.
    if plan_mode_active:
        memory_store = None
    elif definition_scope == SCOPE_AGENT:
        memory_store = memory_store_for_agent(agent_id)
    else:
        memory_store = memory_store_for_tenant(tenant_id) if tenant_id is not None else None
    # Cut ⑥ live wire: LLM How-distillation on the parent's model. Without a
    # distiller the run ends with no writeback and 记忆.md stays read-only.
    memory_distiller = None
    if memory_store is not None:
        memory_distiller = make_llm_how_distiller(
            {
                "provider": model.provider,
                "api_key": model.api_key,
                "model": model.model,
                "base_url": getattr(model, "base_url", None),
            },
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    ctx = SubagentSpawnContext(
        parent_agent_id=agent_id,
        parent_user_id=request.context.user_id,
        model=model,
        fallback_model=fallback_model,
        parent_agent_name=getattr(agent, "name", "Agent"),
        tenant_id=tenant_id,
        model_resolver=(lambda model_name: _resolve_model_override(model_name, tenant_id)) if tenant_id else None,
        memory_store=memory_store,
        memory_distiller=memory_distiller,
        parent_session_id=request.context.session_id,
    )

    ledger_todo_id = str(request.arguments.get("ledger_todo_id") or "").strip() or None

    if bool(request.arguments.get("run_in_background")):
        # Durable fire-and-forget: record the run so a crash → reconcile → failed
        # (not a forever-"running" poll), then schedule the worker and return now.
        from app.services.subagent_run_service import make_run_completer, start_subagent_run

        run_id = await start_subagent_run(
            parent_agent_id=agent_id,
            spec_name=spec.name,
            spec_type=spec.type,
            task=task,
            parent_session_id=request.context.session_id,
        )
        await spawn_subagent(
            ctx,
            spec,
            task,
            fork=spec.isolation,
            ledger_todo_id=ledger_todo_id,
            run_in_background=True,
            on_complete=make_run_completer(run_id),
        )
        return _json(
            {
                "ok": True,
                "mode": "background",
                "run_id": run_id,
                "subagent": spec.name,
                "type": spec.type,
                "definition_scope": definition_scope,
                "status": "running",
                "message": (
                    f"Subagent {spec.name!r} is running in the background. Keep working and poll the "
                    f"result later with check_subagent(run_id={run_id!r})."
                ),
            }
        )

    handle = await spawn_subagent(ctx, spec, task, fork=spec.isolation, ledger_todo_id=ledger_todo_id)
    result = handle.result
    return _json(
        {
            "ok": bool(result and result.ok),
            "subagent": spec.name,
            "type": spec.type,
            "definition_scope": definition_scope,
            "status": result.status if result else "failed",
            "content": result.content if result else "",
            "error": result.error if result else "spawn produced no result",
            "tokens_used": result.tokens_used if result else 0,
        }
    )


@tool(
    ToolMeta(
        name="check_subagent",
        description=(
            "Check a background subagent spawned with spawn_subagent(run_in_background=true). "
            "Pass run_id to get one run's status and (when finished) its conclusion digest; omit run_id "
            "to list your recent background runs. A run is 'running', 'completed' (result ready), or "
            "'failed' — including a worker that died in a process restart, which resolves as failed rather "
            "than staying 'running' forever, so this poll always terminates."
        ),
        parameters={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id returned by a background spawn_subagent. Omit to list recent runs.",
                },
            },
        },
        category="coordination",
        display_name="Check Subagent",
        icon="🔍",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
    )
)
async def check_subagent(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.subagent_run_service import get_subagent_run, list_subagent_runs

    run_id = str(arguments.get("run_id") or "").strip()
    if run_id:
        record = await get_subagent_run(run_id, agent_id)
        if record is None:
            return _json({"ok": False, "error": f"no background subagent run {run_id!r} for this agent"})
        metadata = record.get("metadata") or {}
        return _json(
            {
                "ok": True,
                "run_id": run_id,
                "name": record.get("child_agent_name"),
                "status": record.get("status"),
                "result": record.get("result") or "",
                "orphaned_by_restart": bool(metadata.get("orphaned_by_restart")),
            }
        )
    return _json({"ok": True, "runs": await list_subagent_runs(agent_id)})
