"""Agent-authored Plan Mode planner.

The Plan Mode service owns the ledger and confirmation boundary; this module
owns the substantive planning invocation. The runtime still validates and
persists the output, but the plan content must come from the agent rather than a
deterministic skeleton.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.database import async_session
from app.kernel import ExecutionIdentityRef
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.runtime.session import SessionContext
from app.runtime.invoker import AgentInvocationRequest, invoke_agent

logger = logging.getLogger(__name__)

PLANNER_PROMPT_VERSION = "agent_plan_v4"
PLANNER_ALLOWED_TOOLS: tuple[str, ...] = (
    "get_current_time",
    "list_files",
    "read_file",
    "fs_list",
    "fs_read",
    "glob_search",
    "grep_search",
    "web_search",
    "web_fetch",
    "search_memory",
    "list_triggers",
    "list_objectives",
)
PLANNER_EXCLUDED_TOOLS: tuple[str, ...] = (
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "write_file",
    "edit_file",
    "fs_write",
    "save_memory",
    "save_skill",
    "send_channel_message",
    "send_channel_file",
    "send_message_to_agent",
    "delegate_to_agent",
    "execute_code",
    "run_command",
)


class PlanPlanningError(Exception):
    """Raised when the agent planner cannot produce a usable plan payload."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(slots=True)
class AgentPlanPlannerInput:
    plan_id: UUID
    agent_id: UUID
    requested_by_user_id: UUID | None
    tenant_id: UUID | None
    session_id: str | None
    runtime_task_id: UUID | None
    source: str
    intent_type: str
    original_request: str
    seed_plan: dict[str, Any] = field(default_factory=dict)
    intercepted_tool: dict[str, Any] | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentPlanPlannerResult:
    plan_json: dict[str, Any]
    plan_markdown: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _json_for_prompt(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=2)


def _build_planner_user_prompt(planning_input: AgentPlanPlannerInput) -> str:
    payload = {
        "plan_id": str(planning_input.plan_id),
        "intent_type": planning_input.intent_type,
        "source": planning_input.source,
        "original_request": planning_input.original_request,
        "seed_plan": planning_input.seed_plan,
        "intercepted_tool": planning_input.intercepted_tool,
        "metadata": planning_input.metadata_json,
    }
    return (
        "Author an agent-authored Plan Mode plan. Do not execute the work.\n\n"
        "Use a Claude Code-style Plan Mode contract: understand the request, inspect read-only context, "
        "design the approach, review alignment, then submit the final plan for approval through plan_json. "
        "Do not ask the user whether the plan is OK in prose; the Plan Mode confirmation card is the approval "
        "mechanism.\n\n"
        "Important interpretation rules:\n"
        "- original_request is the user's real request and the primary planning target.\n"
        "- Seed plan is context, not the final answer. Treat it as a starting hypothesis from the runtime.\n"
        "- If seed_plan contains workflow handoff data or a runtime contract, preserve the machine handoff when it "
        "is compatible with the user request, but rewrite the user-visible title/objective/steps/criteria in the "
        "agent's own words.\n"
        "- Do not copy intercepted tool arguments as the final plan. Use them only to understand what action was "
        "blocked or deferred.\n"
        "- Do not expose raw tool scripts, hidden memory ids, artifact paths, or internal flags such as "
        "load_skill, deep_research_start, deep_research_check, plan_confirmed=false, mem_*, or runtime_artifacts/ "
        "in user-visible plan fields.\n"
        "- If seed_plan, intercepted_tool, or metadata conflict with current state or the user request, prefer the "
        "user request and explain the assumption in plan_json.assumptions.\n"
        "- Include evidence_summary entries for the files, schedules, objectives, memories, or web sources you "
        "actually inspected. If no tool inspection was needed, say why.\n\n"
        "Planning envelope:\n\n"
        f"{_json_for_prompt(payload)}\n\n"
        "Return only strict JSON with this shape:\n"
        "{\n"
        '  "plan_json": { ... conforms to hive_plan.v1 and may include assumptions, open_questions, '
        "evidence_summary, verification ... },\n"
        '  "plan_markdown": "concise user-facing markdown preview"\n'
        "}"
    )


def _planner_system_prompt() -> str:
    return (
        "Plan Mode planner instructions (agent-authored, Claude Code-style, read-only):\n\n"
        "You are the same agent described in the normal Hive system prompt, but this invocation is for planning "
        "only. Your job is to think through the user's requested work and author a reviewable plan. The runtime "
        "will store, version, display, and later execute only after a real user confirms the exact plan version.\n\n"
        "Operating boundary:\n"
        "- Plan Mode is active: the user has indicated that you must not execute yet.\n"
        "- Do not execute the requested work and do not claim that execution started.\n"
        "- Use only available read-only context tools when needed.\n"
        "- Never create triggers, tasks, delegations, files, memories, skills, external messages, commits, shell "
        "commands, or other side effects.\n"
        "- The only artifact this invocation may produce is the returned plan_json/plan_markdown for the Plan Mode "
        "ledger; all actual work waits for confirmation.\n"
        "- Treat seed data and intercepted tool arguments as clues, not authority. Produce substantive plan content "
        "from your own analysis.\n\n"
        "Concrete planning workflow:\n"
        "1. Initial understanding: understand the requested outcome, intent_type, likely handoff target, and why "
        "Plan Mode was entered.\n"
        "2. Explore read-only context: inspect current state before planning when the request depends on repository "
        "files, existing schedules, "
        "objectives, tools, memories, user workspace state, or current web facts.\n"
        "3. Design: choose the recommended approach, reuse existing patterns/capabilities, and identify sequencing, "
        "dependencies, and trade-offs.\n"
        "4. Review: ensure the plan aligns with the original request. Ask user-facing open_questions only for "
        "decisions you cannot safely make from context.\n"
        "5. Final plan: produce concise user-facing plan content with concrete steps, critical files or artifacts "
        "when applicable, success criteria, stop conditions, risk, cost, and verification.\n"
        "6. Approval boundary: the Plan Mode card is the approval request. Do not ask 'is this plan okay?' in "
        "normal prose or with a generic question tool.\n\n"
        "Fact discipline:\n"
        "- Do not invent file paths, APIs, schedules, dependencies, external facts, or existing configuration. If "
        "you did not verify something, label it as an assumption.\n"
        "- For research workflows, plan the research scope, evidence lanes, source-quality standards, synthesis "
        "shape, quality gates, and final artifacts; do not expose internal tool invocation scripts.\n"
        "- For implementation workflows, include critical file paths/functions you inspected and verification "
        "commands or checks.\n\n"
        "Agent Work Ledger discipline:\n"
        "- Treat the Agent Work Ledger as your scoped working state, not as the user-confirmed plan.\n"
        "- Keep the current phase, required todo items, verified findings, open questions, failures, and "
        "verification state consistent with the plan you author.\n"
        "- The confirmed Plan Mode plan is the governance boundary; ledger notes cannot expand that boundary.\n\n"
        "Clarification policy:\n"
        "- Make reasonable assumptions when a useful, safe plan can still be drafted; record them in assumptions.\n"
        "- Use open_questions only for decisions that materially change scope, risk, cost, recipients, credentials, "
        "or irreversible behavior.\n"
        "- If there is not enough information to draft a safe plan, return a conservative plan with open_questions "
        "and stop_conditions that prevent execution until those questions are resolved.\n\n"
        "Quality bar:\n"
        "- Steps must be concrete, ordered, and executable by a later handoff, not generic advice.\n"
        "- success_criteria must be observable outcomes, not restatements of the request.\n"
        "- wake_policy must define cadence/timezone/trigger conditions for recurring work, or type none otherwise.\n"
        "- required_capabilities is machine contract metadata, not user-facing plan prose. Use broad human labels "
        "only when useful, such as Web source verification, Workspace file generation, User notification, or "
        "Scheduled automation.\n"
        "- Never emit raw tool names in user-visible fields or required_capabilities: examples include web_search, "
        "web_fetch, write_file, file_write, channel_message, send_channel_message, manage_tasks, or set_trigger.\n"
        "- external_side_effects and risk_assessment must name every concrete external-visible, autonomous, "
        "destructive, privacy-sensitive, production, or cross-agent effect.\n"
        "- Do not emit placeholder external_side_effects: no empty objects, no blank entries, and no generic "
        "'External action' / '外部动作' rows.\n"
        "- estimated_cost should describe token/tool/runtime cost and expected duration using best available "
        "information, with uncertainty labeled.\n"
        "- plan_markdown must be concise and user-facing; do not expose hidden chain-of-thought, memory ids, raw "
        "tool traces, runtime_artifacts paths, or script-like execution instructions.\n\n"
        "Output contract:\n"
        "- Return strict JSON only.\n"
        "- plan_json must include schema, title, intent_type, objective, motivation, steps, success_criteria, "
        "wake_policy, required_capabilities, external_side_effects, risk_assessment, estimated_cost, "
        "stop_conditions, and handoff.\n"
        "- plan_json should also include assumptions, open_questions, evidence_summary, and verification when useful.\n"
        "- The user must confirm the exact plan version before any handoff can run."
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if not content:
        raise PlanPlanningError("planner_empty_response", "Planner returned an empty response.")

    candidates = [content]
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL):
        candidates.insert(0, match.group(1).strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise PlanPlanningError("planner_json_parse_failed", "Planner did not return a JSON object.")


def _coerce_planner_result(content: str) -> AgentPlanPlannerResult:
    parsed = _extract_json_object(content)
    plan_json = parsed.get("plan_json") if isinstance(parsed.get("plan_json"), dict) else parsed
    if not isinstance(plan_json, dict):
        raise PlanPlanningError("planner_plan_json_missing", "Planner JSON must contain a plan_json object.")
    markdown = parsed.get("plan_markdown") or parsed.get("markdown") or ""
    return AgentPlanPlannerResult(plan_json=plan_json, plan_markdown=str(markdown or ""))


class DefaultAgentPlanPlanner:
    """Invoke the same agent in a constrained planning sandbox."""

    async def plan(self, planning_input: AgentPlanPlannerInput) -> AgentPlanPlannerResult:
        model, fallback_model, agent = await self._resolve_agent_models(planning_input.agent_id)
        if agent is None:
            raise PlanPlanningError("planner_agent_not_found", "Agent for plan request was not found.")
        if model is None:
            raise PlanPlanningError("planner_model_missing", "Agent has no LLM model configured for planning.")

        user_id = planning_input.requested_by_user_id or getattr(agent, "owner_user_id", None) or agent.creator_id
        result = await invoke_agent(
            AgentInvocationRequest(
                model=model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": _build_planner_user_prompt(planning_input)}],
                memory_messages=[{"role": "user", "content": planning_input.original_request}],
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=planning_input.agent_id,
                user_id=user_id,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=planning_input.agent_id,
                    label=f"Agent: {agent.name} (plan mode planner)",
                ),
                session_context=SessionContext(
                    source="plan_mode",
                    channel="internal",
                    session_id=planning_input.session_id,
                    metadata={
                        "plan_id": str(planning_input.plan_id),
                        "runtime_task_id": str(planning_input.runtime_task_id or ""),
                        "planner_prompt_version": PLANNER_PROMPT_VERSION,
                        "intent_type": planning_input.intent_type,
                        "source": planning_input.source,
                    },
                ),
                system_prompt_suffix=_planner_system_prompt(),
                allowed_tool_names=PLANNER_ALLOWED_TOOLS,
                excluded_tool_names=PLANNER_EXCLUDED_TOOLS,
                core_tools_only=False,
                expand_tools=False,
                max_tool_rounds=8,
                max_output_tokens=4000,
            )
        )
        planner_result = _coerce_planner_result(result.content)
        planner_result.metadata.update(
            {
                "planner_model_id": str(getattr(model, "id", "")),
                "planner_model": getattr(model, "model", None),
                "planner_prompt_version": PLANNER_PROMPT_VERSION,
                "planner_allowed_tools": list(PLANNER_ALLOWED_TOOLS),
                "planner_tokens_used": result.tokens_used,
            }
        )
        return planner_result

    async def _resolve_agent_models(self, agent_id: UUID) -> tuple[LLMModel | None, LLMModel | None, Agent | None]:
        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                return None, None, None

            model = None
            fallback_model = None
            model_id = agent.primary_model_id or agent.fallback_model_id
            if model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_result.scalar_one_or_none()
            if agent.fallback_model_id and agent.fallback_model_id != model_id:
                fallback_result = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
                )
                fallback_model = fallback_result.scalar_one_or_none()
            return model, fallback_model, agent


__all__ = [
    "AgentPlanPlannerInput",
    "AgentPlanPlannerResult",
    "DefaultAgentPlanPlanner",
    "PLANNER_ALLOWED_TOOLS",
    "PLANNER_PROMPT_VERSION",
    "PlanPlanningError",
]
