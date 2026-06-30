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

from sqlalchemy import and_, desc, or_, select

from app.agents.subagent import (
    PUBLIC_BUILTIN_SUBAGENT_TYPES,
    SUBAGENT_TYPE_GENERAL_PURPOSE,
    SubagentSpawnContext,
    SubagentSpec,
    canonical_subagent_type,
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
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.services.agent_session_continuation import continue_agent_session_from_mailbox
from app.services.agent_team_runtime_service import (
    send_agent_team_message_from_tool_request,
    spawn_agent_team_member_from_tool_request,
)
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


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
        "description": {
            "type": "string",
            "description": (
                "Optional short description of why this session-local worker is being spawned. "
                "Used for trace/read-model context only; the executable instruction belongs in prompt."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Canonical AgentTool prompt: the self-contained instruction for the session-local worker. "
                "The worker receives this prompt as its task and returns only a conclusion digest."
            ),
        },
        "subagent_type": {
            "type": "string",
            "enum": list(PUBLIC_BUILTIN_SUBAGENT_TYPES),
            "description": (
                "Canonical AgentTool worker type. Defaults to 'general-purpose'. Use 'explorer' for "
                "read-only reconnaissance and 'critic' for independent verification."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model override, resolved by model id, label, or provider model name within the tenant pool."
            ),
        },
        "task": {
            "type": "string",
            "description": (
                "Compatibility alias for prompt. Prefer prompt for new calls."
            ),
        },
        "type": {
            "type": "string",
            "enum": list(PUBLIC_BUILTIN_SUBAGENT_TYPES),
            "description": (
                "Compatibility alias for subagent_type. Prefer subagent_type for new calls."
            ),
        },
        "name": {
            "type": "string",
            "description": "Optional short name for the subagent (e.g. 'market-scout'). Defaults to the type name.",
        },
        "team_name": {
            "type": "string",
            "description": (
                "CC AgentTool teammate branch: when team_name and name are both set, spawn an addressable "
                "Agent Team teammate instead of a one-shot worker. Call team_create first to create the team."
            ),
        },
        "mode": {
            "type": "string",
            "description": "Optional teammate permission/planning mode. Used only with team_name + name teammate spawn.",
        },
        "isolation": {
            "type": "string",
            "enum": ["none", "all"],
            "description": (
                "Fork mode. 'none' starts a fresh AgentTool worker with only your prompt. 'all' forks the current "
                "session context into the child. If subagent_type/type/definition_name is omitted on a foreground "
                "spawn, 'all' is used; background agents stay fresh unless isolation='all' is explicit."
            ),
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
                "When true, returns immediately with run_id and child_session_id while the worker continues. "
                "Do not busy-poll by default: keep working and wait for the completion wake. Use check_subagent "
                "only for explicit fallback inspection. Default false runs to completion and returns the digest now."
            ),
        },
        "ledger_todo_id": {
            "type": "string",
            "description": (
                "Optional id of your work-ledger todo this subagent serves: spawn stamps the "
                "subagent as the todo's owner and completion writes the terminal status back."
            ),
        },
        "permission_profile": {
            "type": "object",
            "description": (
                "Optional scoped execution profile for skill handoffs. When allowed_tools is present, the child "
                "subagent is hard-limited to that tool set through the normal governed runtime."
            ),
            "properties": {
                "mode": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "writable_roots": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "anyOf": [{"required": ["prompt"]}, {"required": ["task"]}],
}


def _normalize_spawn_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize CC AgentTool fields and legacy Hive aliases into one shape."""

    task = str(arguments.get("prompt") or arguments.get("task") or "").strip()
    explicit_type = any(str(arguments.get(key) or "").strip() for key in ("subagent_type", "type", "definition_name"))
    team_name = str(arguments.get("team_name") or "").strip()
    subagent_type = str(
        arguments.get("subagent_type") or arguments.get("type") or SUBAGENT_TYPE_GENERAL_PURPOSE
    ).strip()
    if not subagent_type:
        subagent_type = SUBAGENT_TYPE_GENERAL_PURPOSE
    subagent_type = canonical_subagent_type(subagent_type, default=SUBAGENT_TYPE_GENERAL_PURPOSE)
    raw_isolation = str(arguments.get("isolation") or "").strip()
    if raw_isolation not in {"none", "all"}:
        raw_isolation = ""
    background = bool(arguments.get("run_in_background"))
    isolation = raw_isolation or ("all" if not explicit_type and not team_name and not background else "none")
    return {
        **arguments,
        "task": task,
        "prompt": task,
        "type": subagent_type,
        "subagent_type": subagent_type,
        "description": str(arguments.get("description") or "").strip(),
        "model": str(arguments.get("model") or "").strip(),
        "team_name": team_name,
        "mode": str(arguments.get("mode") or "").strip(),
        "isolation": isolation,
        "_explicit_subagent_type": explicit_type,
    }


def _permission_profile_allowed_tools(arguments: dict[str, Any]) -> tuple[str, ...]:
    profile = arguments.get("permission_profile")
    if not isinstance(profile, dict):
        return ()
    raw_allowed = profile.get("allowed_tools")
    if not isinstance(raw_allowed, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw_allowed if str(item).strip()))


async def _load_parent_messages_for_fork(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str | None,
    limit: int = 120,
) -> list[dict[str, Any]]:
    if tenant_id is None or not session_id:
        return []
    try:
        session_uuid = uuid.UUID(str(session_id))
    except ValueError:
        return []

    from app.models.audit import ChatMessage
    from app.services.web_chat_runtime import _apply_active_projection_to_history, conversation_from_history_messages

    async with tenant_scoped_session(tenant_id) as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_uuid,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return []
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == str(session_uuid),
                    )
                    .order_by(desc(ChatMessage.created_at))
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        rows.reverse()
        projected = await _apply_active_projection_to_history(db, session, rows)
        return conversation_from_history_messages(projected)


@tool(
    ToolMeta(
        name="spawn_subagent",
        description=(
            "AgentTool-compatible To Session Worker: spawn a session-local worker, fork the current session "
            "context, launch a background agent, or spawn an Agent Team teammate via team_name + name. "
            "Use this to parallelize independent work, protect the parent context from noisy exploration, "
            "or get independent verification before reporting completion. "
            "This is not A2A delegation and does not require a colleague relationship. Built-in types: "
            "'general-purpose' — default edit-capable worker for a scoped task; "
            "'explorer' — fast READ-ONLY reconnaissance over files and the web; use for finding files, "
            "searching content, and answering questions about a large body of material. "
            "'critic' — read-only verification specialist; pass it the original task plus the claims or "
            "artifacts to check and it returns a PASS/FAIL/PARTIAL verdict with evidence. "
            "Named definitions (via definition_name) override type, tools, model, and prompt from their "
            "stored 定义.md. Subagents cannot delegate or spawn further and run under the same governance "
            "as you. To hand work to another standalone digital employee (To Employee), use delegate_to_agent instead. "
            "For Agent Team: first call team_create to create the Team container, then call this tool with "
            "team_name and name to spawn a teammate; use send_agent_session_message with to/member_name to talk "
            "inside the Team. "
            "If the step ORDER itself is the requirement (fixed sequence, mid-run approval gates, "
            "budgeted fan-out), use propose_dynamic_workflow/preview_workflow/start_workflow instead of spawning."
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
    normalized_args = _normalize_spawn_arguments(request.arguments)
    task = str(normalized_args.get("task") or "").strip()
    if not task:
        return _json({"ok": False, "error": "prompt or task is required"})

    from app.services.plan_mode_runtime_context import interactive_plan_mode_active
    from app.tools.plan_mode_policy import is_plan_mode_tool_allowed

    plan_mode_active = interactive_plan_mode_active()
    if plan_mode_active and not is_plan_mode_tool_allowed("spawn_subagent", normalized_args):
        return _json(
            {
                "ok": False,
                "error": (
                    "Interactive Plan Mode only allows synchronous inline explorer/critic subagents. "
                    "Workers, background runs, persistent definitions, and ledger ownership require plan approval first."
                ),
            }
        )

    team_name = str(normalized_args.get("team_name") or "").strip()
    member_name = str(normalized_args.get("name") or "").strip()
    if team_name and member_name:
        try:
            return _json(
                await spawn_agent_team_member_from_tool_request(
                    request,
                    team_name=team_name,
                    member_name=member_name,
                    prompt=task,
                    description=str(normalized_args.get("description") or ""),
                    subagent_type=str(normalized_args.get("subagent_type") or SUBAGENT_TYPE_GENERAL_PURPOSE),
                    model=str(normalized_args.get("model") or ""),
                    mode=str(normalized_args.get("mode") or ""),
                )
            )
        except Exception as exc:
            return _json({"ok": False, "error": f"teammate spawn failed: {exc}"})

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

    definition_name = str(normalized_args.get("definition_name") or "").strip()
    raw_rounds = normalized_args.get("max_tool_rounds")
    max_tool_rounds = int(raw_rounds) if isinstance(raw_rounds, int) else None
    model_override = str(normalized_args.get("model") or "").strip()

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
        if model_override:
            spec.model = model_override
        if normalized_args.get("description"):
            spec.description = str(normalized_args["description"])
        if str(normalized_args.get("isolation") or "").strip() in {"none", "all"}:
            spec.isolation = str(normalized_args["isolation"])  # type: ignore[assignment]
        definition_scope = resolved.scope
    else:
        subagent_type = str(normalized_args.get("subagent_type") or SUBAGENT_TYPE_GENERAL_PURPOSE).strip()
        if subagent_type not in PUBLIC_BUILTIN_SUBAGENT_TYPES:
            return _json(
                {
                    "ok": False,
                    "error": (
                        "unknown builtin subagent type "
                        f"{subagent_type!r}; expected one of {list(PUBLIC_BUILTIN_SUBAGENT_TYPES)}"
                    ),
                }
            )
        name = str(normalized_args.get("name") or subagent_type).strip() or subagent_type
        try:
            name = validate_subagent_name(name)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        spec = SubagentSpec(
            name=name,
            description=str(normalized_args.get("description") or ""),
            type=subagent_type,
            model=model_override or None,
            max_tool_rounds=max_tool_rounds,
            isolation=str(normalized_args.get("isolation") or "none"),  # type: ignore[arg-type]
            has_own_memory=not plan_mode_active,
        )

    scoped_allowed_tools = _permission_profile_allowed_tools(normalized_args)
    if scoped_allowed_tools:
        spec.allowed_tools = scoped_allowed_tools

    # §12.5: memory follows the definition's scope — agent-private definitions
    # accumulate craft in the agent workspace; tenant definitions (and inline
    # specs, unchanged) share the tenant store.
    if plan_mode_active:
        memory_store = None
    elif spec.memory_scope in {"project", "local"}:
        memory_store = memory_store_for_agent(agent_id)
    elif spec.memory_scope == "user":
        memory_store = memory_store_for_tenant(tenant_id) if tenant_id is not None else None
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
        trace_id=f"subagent:{request.context.session_id or agent_id}:{uuid.uuid4().hex}",
        model_resolver=(lambda model_name: _resolve_model_override(model_name, tenant_id)) if tenant_id else None,
        memory_store=memory_store,
        memory_distiller=memory_distiller,
        parent_session_id=request.context.session_id,
        parent_messages=await _load_parent_messages_for_fork(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=request.context.session_id,
        )
        if spec.isolation == "all"
        else [],
    )

    ledger_todo_id = str(normalized_args.get("ledger_todo_id") or "").strip() or None

    if bool(normalized_args.get("run_in_background")):
        # Durable fire-and-forget: record the run so a crash → reconcile → failed
        # (not a forever-"running" poll), then schedule the worker and return now.
        from app.services.subagent_run_service import make_run_completer, start_subagent_run

        started = await start_subagent_run(
            parent_agent_id=agent_id,
            parent_user_id=request.context.user_id,
            spec_name=spec.name,
            spec_type=spec.type,
            task=task,
            parent_session_id=request.context.session_id,
            trace_id=ctx.trace_id,
            context_mode=spec.isolation,
        )
        run_id = started.run_id
        ctx.subagent_run_id = run_id
        ctx.child_session_id = started.child_session_id
        await spawn_subagent(
            ctx,
            spec,
            task,
            fork=spec.isolation,
            ledger_todo_id=ledger_todo_id,
            run_in_background=True,
            on_complete=make_run_completer(run_id),
        )
        child_session_id = started.child_session_id
        return _json(
            {
                "ok": True,
                "mode": "background",
                "run_id": run_id,
                "child_session_id": child_session_id,
                "subagent": spec.name,
                "type": spec.type,
                "definition_scope": definition_scope,
                "team_name": normalized_args.get("team_name") or None,
                "status": "running",
                "session_state": "running",
                "continuation": {
                    "address": child_session_id,
                    "tool": "send_agent_session_message",
                    "interrupt_supported": False,
                },
                "transcript_refs": {
                    "session_id": child_session_id,
                    "parent_session_id": request.context.session_id,
                },
                "message": (
                    f"Subagent {spec.name!r} is running in the background. Keep working and wait for the "
                    "completion wake; use check_subagent only if you explicitly need fallback status inspection."
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
            "team_name": normalized_args.get("team_name") or None,
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
            "This is a fallback inspection tool, not the normal wait path: parent agents should usually "
            "keep working and react to the completion wake. Pass run_id to get one run's status, child "
            "session refs, and any terminal conclusion digest; omit run_id to list recent background runs."
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
        session_contract = (
            metadata.get("session_contract") if isinstance(metadata.get("session_contract"), dict) else {}
        )
        child_session_id = (
            str(record.get("child_session_id") or "").strip()
            or str(metadata.get("child_session_id") or "").strip()
            or str(session_contract.get("continuation_address") or "").strip()
            or None
        )
        status = record.get("status")
        result = record.get("result") or record.get("result_summary") or ""
        return _json(
            {
                "ok": True,
                "run_id": run_id,
                "name": record.get("child_agent_name"),
                "status": status,
                "result": result,
                "child_session_id": child_session_id,
                "session_state": {
                    "status": status,
                    "active_run_id": run_id if status in {"pending", "running", "in_progress"} else None,
                    "child_session_id": child_session_id,
                    "parent_session_id": record.get("parent_session_id"),
                },
                "transcript_refs": {
                    "session_id": child_session_id,
                    "parent_session_id": record.get("parent_session_id"),
                    "trace_id": record.get("trace_id"),
                },
                "continuation": {
                    "address": child_session_id,
                    "tool": session_contract.get("continuation_tool") or "send_agent_session_message",
                    "available": bool(child_session_id),
                },
                "model_guidance": "fallback_inspection_only",
                "orphaned_by_restart": bool(metadata.get("orphaned_by_restart")),
            }
        )
    return _json({"ok": True, "runs": await list_subagent_runs(agent_id)})


@tool(
    ToolMeta(
        name="send_agent_session_message",
        description=(
            "Append a follow-up message to an existing child agent session mailbox. Use this for Agent Team "
            "member sessions or background subagent child_session_id values returned by spawn_subagent. For "
            "Agent Team workspaces you may pass to/member_name plus team_name, or team_id plus member_name, "
            "with to/member_name='*' to broadcast, "
            "so you do not need to copy child session UUIDs. This records the continuation in the Session/T0 "
            "transcript; active runtimes consume it through the session mailbox layer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "The child_session_id returned by spawn_subagent or an Agent Team member session id.",
                },
                "team_id": {
                    "type": "string",
                    "description": "Agent Team id returned by team_create when addressing a Team member by name.",
                },
                "team_name": {
                    "type": "string",
                    "description": "Agent Team name. Use this with to/member_name when you do not want to copy the team UUID.",
                },
                "to": {
                    "type": "string",
                    "description": "CC SendMessage-style teammate name, or '*' to broadcast to active members.",
                },
                "member_name": {
                    "type": "string",
                    "description": "Agent Team member name to message, or '*' to broadcast to active members.",
                },
                "message": {
                    "type": "string",
                    "description": "Follow-up instruction to append to the child session mailbox.",
                },
                "interrupt": {
                    "type": "boolean",
                    "description": "Whether the follow-up should interrupt an active child run when a runtime supports it.",
                },
            },
            "required": ["message"],
            "anyOf": [
                {"required": ["child_session_id"]},
                {"required": ["team_id", "member_name"]},
                {"required": ["team_name", "to"]},
                {"required": ["to"]},
            ],
        },
        category="coordination",
        display_name="Send Agent Session Message",
        icon="✉️",
        governance="sensitive",
        adapter="request",
    )
)
async def send_agent_session_message(request: ToolExecutionRequest) -> str:
    child_session_uuid = _uuid_or_none(request.arguments.get("child_session_id"))
    message = str(request.arguments.get("message") or "").strip()
    if child_session_uuid is None:
        if request.arguments.get("team_id") or request.arguments.get("team_name") or request.arguments.get("to"):
            try:
                return _json(await send_agent_team_message_from_tool_request(request))
            except Exception as exc:
                return _json({"ok": False, "error": f"Agent Team message failed: {exc}"})
        return _json({"ok": False, "error": "child_session_id or team_id/member_name is required"})
    if not message:
        return _json({"ok": False, "error": "message is required"})

    tenant_id = _uuid_or_none(request.context.tenant_id) or await resolve_tenant_for_agent(request.context.agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == child_session_uuid,
                or_(
                    ChatSession.agent_id == request.context.agent_id,
                    and_(
                        ChatSession.peer_agent_id == request.context.agent_id,
                        ChatSession.source_channel == "agent",
                    ),
                ),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            return _json({"ok": False, "error": "child agent session not found for this agent"})

        agent = (await db.execute(select(Agent).where(Agent.id == session.agent_id))).scalar_one_or_none()
        user_id = _uuid_or_none(getattr(session, "user_id", None)) or _uuid_or_none(request.context.user_id)
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none() if user_id else None
        if agent is None or user is None:
            return _json({"ok": False, "error": "child agent session continuation principal could not be loaded"})

        interrupt = bool(request.arguments.get("interrupt"))
        result = await continue_agent_session_from_mailbox(
            db=db,
            agent=agent,
            user=user,
            session=session,
            message=message,
            parent_session_id=request.context.session_id,
            interrupt_requested=interrupt,
        )

    return _json(
        {
            "ok": bool(result.get("ok", True)),
            "child_session_id": str(child_session_uuid),
            "status": result.get("status") or "queued",
            "interrupt_requested": interrupt,
            "consumer": result.get("consumer"),
            "run_id": result.get("run_id"),
            "reason": result.get("reason"),
            "message": "Follow-up sent to child session continuation controls.",
        }
    )
