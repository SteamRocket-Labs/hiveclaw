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

from app.agents.subagent import SubagentSpawnContext, SubagentSpec, spawn_subagent
from app.agents.subagent_definition import (
    SCOPE_AGENT,
    list_subagent_definitions,
    resolve_subagent_definition,
    validate_subagent_name,
)
from app.agents.subagent_memory import memory_store_for_agent, memory_store_for_tenant
from app.database import async_session
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

    async with async_session() as db:
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
    async with async_session() as db:
        base_filters = [LLMModel.enabled.is_(True)]
        if tenant_id is not None:
            base_filters.append(LLMModel.tenant_id == tenant_id)

        try:
            model_id = uuid.UUID(value)
        except ValueError:
            model_id = None
        if model_id is not None:
            return (await db.execute(select(LLMModel).where(LLMModel.id == model_id, *base_filters))).scalar_one_or_none()

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
                "The self-contained task for the spawned explorer. It runs in isolation with only "
                "this task as context and returns a conclusion digest — not its intermediate steps."
            ),
        },
        "name": {
            "type": "string",
            "description": "Optional short name for the explorer (e.g. 'market-scout'). Defaults to 'explorer'.",
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
            "description": "Optional cap on the explorer's tool rounds (default 8).",
        },
    },
    "required": ["task"],
}


@tool(
    ToolMeta(
        name="spawn_subagent",
        description=(
            "Spawn a lightweight read-only explorer subagent to investigate one self-contained task "
            "in isolation and return a conclusion digest. Use this to parallelize exploration or to "
            "keep a noisy sub-investigation out of your own context. The explorer has read-only web + "
            "file + memory tools, cannot delegate or spawn further, and runs under the same governance "
            "as you. To hand work to another standalone digital employee, use delegate_to_agent instead."
        ),
        parameters=_SPAWN_PARAMETERS,
        category="coordination",
        display_name="Spawn Subagent",
        icon="🧬",
        governance="sensitive",
        pack="coordination_pack",
        adapter="request",
    )
)
async def spawn_subagent_tool(request: ToolExecutionRequest) -> str:
    task = str(request.arguments.get("task") or "").strip()
    if not task:
        return _json({"ok": False, "error": "task is required"})

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
            # template rows) so the model can self-correct — builtins spawn via
            # inline `name`/type, not via definition_name.
            available = list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id)
            return _json(
                {
                    "ok": False,
                    "error": (
                        f"subagent definition {definition_name!r} not found in agent or tenant scope. "
                        "Builtin types (scope=builtin) are inline templates: spawn them via 'name' without "
                        "definition_name."
                    ),
                    "available": [{"name": row["name"], "scope": row["scope"]} for row in available],
                }
            )
        spec = resolved.spec
        definition_scope = resolved.scope
    else:
        name = str(request.arguments.get("name") or "explorer").strip() or "explorer"
        try:
            name = validate_subagent_name(name)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        spec = SubagentSpec(name=name, type="explorer", max_tool_rounds=max_tool_rounds)

    # §12.5: memory follows the definition's scope — agent-private definitions
    # accumulate craft in the agent workspace; tenant definitions (and inline
    # specs, unchanged) share the tenant store.
    if definition_scope == SCOPE_AGENT:
        memory_store = memory_store_for_agent(agent_id)
    else:
        memory_store = memory_store_for_tenant(tenant_id) if tenant_id is not None else None
    ctx = SubagentSpawnContext(
        parent_agent_id=agent_id,
        parent_user_id=request.context.user_id,
        model=model,
        fallback_model=fallback_model,
        parent_agent_name=getattr(agent, "name", "Agent"),
        tenant_id=tenant_id,
        model_resolver=(lambda model_name: _resolve_model_override(model_name, tenant_id)) if tenant_id else None,
        memory_store=memory_store,
        parent_session_id=request.context.session_id,
    )

    handle = await spawn_subagent(ctx, spec, task, fork=spec.isolation)
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
