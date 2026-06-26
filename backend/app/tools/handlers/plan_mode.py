"""Interactive Plan Mode tools."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from app.services import plan_mode_core
from app.services.plan_mode_runtime_context import (
    interactive_plan_mode_active,
    interactive_plan_mode_metadata,
)
from app.services.plan_mode_service import get_plan_mode_service
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("description") or item.get("title") or item.get("name") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            step = dict(item)
            step.setdefault("order", index)
            description = str(step.get("description") or step.get("title") or step.get("task") or "").strip()
            if description:
                step["description"] = description
            steps.append(step)
            continue
        description = str(item or "").strip()
        if description:
            steps.append({"order": index, "description": description})
    return steps


def _risk(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        level = str(value.get("level") or "medium").lower()
        reasons = _string_list(value.get("reasons"))
    else:
        level = "medium"
        reasons = []
    if level not in {"low", "medium", "high"}:
        level = "medium"
    return {"level": level, "reasons": reasons}


def _estimated_cost(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"tokens_per_run": "unknown", "expected_duration": "unknown"}
    return {
        "tokens_per_run": str(value.get("tokens_per_run") or "unknown"),
        "expected_duration": str(value.get("expected_duration") or "unknown"),
    }


def _wake_policy(value: Any, *, handoff_target: str) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("type"):
        return dict(value)
    if handoff_target == "scheduled_trigger":
        return {"type": "manual"}
    return {"type": "none"}


def _handoff(args: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    # CC parity default: live chat plans continue in the current session unless an
    # explicit target (scheduled_trigger / delegation / detached) was set on the
    # args or carried in the plan-mode metadata. Product workflows such as Deep
    # Research stay in the hidden execution_contract instead of becoming Plan Mode
    # handoff targets.
    explicit_target = args.get("handoff_target") or metadata.get("handoff_target")
    contract = args.get("execution_contract")
    if not isinstance(contract, dict):
        contract = metadata.get("execution_contract")
    inferred_target = (
        "agent_team"
        if isinstance(contract, dict) and str(contract.get("type") or "").strip() in {"agent_team", "team"}
        else "continue_current_session"
    )
    target = str(explicit_target or inferred_target).strip() or inferred_target
    if target == "deep_research":
        target = "continue_current_session"
    payload = args.get("handoff_payload") if isinstance(args.get("handoff_payload"), dict) else {}
    return {
        "target": target,
        "create_trigger": target == "scheduled_trigger",
        "payload": payload,
    }


def _execution_contract(args: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    explicit = args.get("execution_contract")
    if isinstance(explicit, dict) and explicit:
        return dict(explicit)
    if metadata.get("deep_research"):
        deep_args = dict(metadata.get("deep_research_args") or {})
        if not deep_args:
            original_request = str(metadata.get("original_request") or args.get("original_request") or "").strip()
            if original_request:
                deep_args["question"] = original_request
        return {
            "type": "workflow",
            "workflow_ref": "deep_research.v1",
            "args": deep_args,
            "source": "interactive_plan_mode",
        }
    return {}


def _tenant_id(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _plan_uuid(value: Any) -> uuid.UUID | None:
    """Parse a pre-armed draft plan id from Plan Mode metadata (cut ③a).

    Returns ``None`` for absent / malformed ids so ordinary explicit Plan Mode
    sessions without a pre-created ``plan_id`` keep creating a fresh awaiting
    plan.
    """
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _read_provisioned_plan_markdown(request: ToolExecutionRequest, metadata: dict[str, Any]) -> str:
    """Read the MD-first plan body from the exact runtime-provisioned plan file.

    The model may mention a path in tool args, but the trusted source is the
    runtime PlanMode metadata. This keeps the Plan Mode write/read permission at
    one exact file and avoids turning exit_plan_mode into an arbitrary file read.
    """
    plan_file_path = str(metadata.get("plan_file_path") or "").strip()
    if not plan_file_path:
        return ""
    candidate = Path(plan_file_path)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return ""
    workspace = Path(request.context.workspace)
    absolute_path = (workspace / candidate).resolve()
    try:
        workspace_root = workspace.resolve()
        absolute_path.relative_to(workspace_root)
    except ValueError:
        return ""
    if not absolute_path.is_file():
        return ""
    try:
        return absolute_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@tool(
    ToolMeta(
        name="exit_plan_mode",
        description=(
            "Submit the final Plan Mode plan for user confirmation. Use this only when interactive Plan Mode is "
            "active and the plan is ready. This does not execute the work; it creates a confirmable plan card."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short user-facing plan title."},
                "objective": {"type": "string", "description": "What the confirmed work will accomplish."},
                "plan_markdown": {
                    "type": "string",
                    "description": (
                        "Concise markdown plan preview for the user. If the runtime provisioned a plan file, "
                        "write/update that exact file first; the runtime reads the file as the trusted plan body."
                    ),
                },
                "plan_markdown_path": {
                    "type": "string",
                    "description": (
                        "Optional echo of the runtime-provisioned plan file path. The runtime only trusts the "
                        "exact path already stored in Plan Mode metadata."
                    ),
                },
                "steps": {
                    "type": "array",
                    "description": "Ordered plan steps. Strings or objects with description/expected_output are accepted.",
                    "items": {"type": ["string", "object"]},
                },
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "stop_conditions": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "risk_assessment": {"type": "object"},
                "estimated_cost": {"type": "object"},
                "wake_policy": {"type": "object"},
                "handoff_target": {"type": "string"},
                "handoff_payload": {"type": "object"},
                "execution_contract": {
                    "type": "object",
                    "description": (
                        "Hidden machine-readable execution contract for the runtime after the user confirms. "
                        "Do not copy workflow tool names, internal artifact paths, or audit filenames into "
                        "user-visible plan text; keep them here when needed."
                    ),
                },
            },
            "required": ["title", "objective", "steps", "success_criteria", "stop_conditions"],
        },
        category="plan",
        display_name="Exit Plan Mode",
        icon="\u2705",
        read_only=False,
        adapter="request",
    )
)
async def exit_plan_mode(request: ToolExecutionRequest) -> str:
    metadata = interactive_plan_mode_metadata()
    if not metadata:
        return json.dumps(
            {
                "status": "error",
                "error_code": "not_in_plan_mode",
                "message": "exit_plan_mode can only be used while interactive Plan Mode is active.",
            },
            ensure_ascii=False,
        )

    args = dict(request.arguments or {})

    # plan_markdown is the plan body (CC parity: the plan is a markdown article the
    # user confirms, not a field form). When Plan Mode provisioned a writable plan
    # file, that exact file is the trusted approval artifact; tool args cannot
    # override it because they are not the governed MD-first working plan.
    provisioned_plan_file = str(metadata.get("plan_file_path") or "").strip()
    if provisioned_plan_file:
        plan_markdown = _read_provisioned_plan_markdown(request, metadata)
        if not plan_markdown:
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "missing_plan_file_body",
                    "message": (
                        "The runtime provisioned an exact Plan Mode plan file, but it is missing or blank. "
                        "Write the substantive markdown plan to that file first, then call exit_plan_mode. "
                        "Do not bypass the file with a separate plan_markdown argument."
                    ),
                },
                ensure_ascii=False,
            )
    else:
        plan_markdown = str(args.get("plan_markdown") or "").strip()
    if not plan_markdown:
        return json.dumps(
            {
                "status": "error",
                "error_code": "missing_plan_body",
                "message": (
                    "plan_markdown is required and must be substantive: write the plan as a markdown "
                    "article — your reasoning, the approach, trade-offs, and the concrete execution "
                    "steps — not just structured fields. This article is what the user confirms. If a "
                    "blocking decision is still open, call ask_user_question first instead of guessing."
                ),
            },
            ensure_ascii=False,
        )

    handoff = _handoff(args, metadata)
    execution_contract = _execution_contract(args, metadata)
    intent_type = str(metadata.get("intent_type") or args.get("intent_type") or "in_session_execution")
    if intent_type not in plan_mode_core.INTENT_TYPES:
        intent_type = "in_session_execution"
    original_request = str(
        metadata.get("original_request") or args.get("original_request") or args.get("objective") or ""
    )
    title = str(args.get("title") or original_request[:80] or "Plan Mode plan").strip()
    objective = str(args.get("objective") or original_request or title).strip()
    fill = {
        "title": title,
        "objective": objective,
        "motivation": original_request or objective,
        # The agent-authored plan body — the canonical 偏离① fix: it used to be
        # collected by the schema then dropped here, so the card re-rendered from
        # structured fields. Now it lands in plan_json (hash-covered) as the body.
        "plan_markdown": plan_markdown,
        "steps": _steps(args.get("steps")),
        "success_criteria": _string_list(args.get("success_criteria")),
        "wake_policy": _wake_policy(args.get("wake_policy"), handoff_target=handoff["target"]),
        "required_capabilities": _string_list(args.get("required_capabilities")),
        "external_side_effects": args.get("external_side_effects")
        if isinstance(args.get("external_side_effects"), list)
        else [],
        "risk_assessment": _risk(args.get("risk_assessment")),
        "estimated_cost": _estimated_cost(args.get("estimated_cost")),
        "stop_conditions": _string_list(args.get("stop_conditions")),
        "handoff": handoff,
        "assumptions": _string_list(args.get("assumptions")),
        "open_questions": _string_list(args.get("open_questions")),
    }
    if execution_contract:
        fill["execution_contract"] = execution_contract
    # P1 binding: Plan Mode entered from a blocked gated tool carries the
    # action artifact computed at gate-check time (e.g. start_workflow's
    # definition/args hashes). Landing it in the fill puts it in plan_json —
    # covered by the plan hash — so the gate's confirmed-plan binding check
    # passes when the agent re-runs the exact blocked action.
    armed_artifact = metadata.get("action_artifact")
    if isinstance(armed_artifact, dict):
        fill["action_artifact"] = armed_artifact

    # Path-unification cut ③a — dual-state submission. A system_plan_run launcher
    # (REST create/regenerate/revise, Feishu classification) pre-creates a draft
    # plan and arms Plan Mode with its ``plan_id``; the agent fills THAT draft so
    # the id the entry point already returned to the frontend stays stable.
    # Ordinary explicit Plan Mode sessions have no pre-created plan
    # (plan_id absent), so they create a fresh awaiting plan. Both branches land the SAME
    # agent-authored ``fill`` through the structured-fill path — the only
    # difference is whether the row already exists.
    existing_plan_id = _plan_uuid(metadata.get("plan_id"))
    service = get_plan_mode_service()
    if existing_plan_id is not None:
        plan = await service.generate_plan(plan_id=existing_plan_id, fill=fill)
    else:
        signature_payload = {
            "agent_id": str(request.context.agent_id),
            "session_id": request.context.session_id,
            "intent_type": intent_type,
            "original_request": original_request,
            "fill": fill,
        }
        signature = (
            "interactive_plan:"
            + hashlib.sha256(
                json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:32]
        )

        plan = await service.ensure_awaiting_plan_from_fill(
            agent_id=request.context.agent_id,
            intent_type=intent_type,
            signature=signature,
            fill=fill,
            original_request=original_request or objective,
            source="interactive_plan_mode",
            tenant_id=_tenant_id(request.context.tenant_id),
            session_id=request.context.session_id,
            runtime_task_id=None,
            requested_by_user_id=request.context.user_id,
            metadata_json={
                "interactive_plan_mode": True,
                "entry_reason": metadata.get("reason"),
                "deep_research_plan": bool(metadata.get("deep_research")),
            },
        )

    if getattr(plan, "status", None) == "planning_failed":
        plan_metadata = getattr(plan, "metadata_json", None) or {}
        planning_errors = plan_metadata.get("planning_errors")
        if not isinstance(planning_errors, list):
            planning_errors = []
        payload = {
            "status": "planning_failed",
            "item_type": "plan_proposal",
            "plan_id": str(plan.id),
            "plan_version": getattr(plan, "plan_version", 1),
            "plan_hash": getattr(plan, "plan_hash", None),
            "plan_json": getattr(plan, "plan_json", None) or {},
            "planning_errors": planning_errors,
            "summary": "计划未通过校验，需要修改后重新生成。",
            "next_action": "请修改可见计划内容后重新提交；不要确认或开始执行这个失败计划。",
            "requested_by_user_id": str(request.context.user_id),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    payload = {
        "status": "needs_plan",
        "item_type": "plan_proposal",
        "plan_id": str(plan.id),
        "plan_version": getattr(plan, "plan_version", 1),
        "plan_hash": getattr(plan, "plan_hash", None),
        "plan_json": getattr(plan, "plan_json", None) or fill,
        "summary": f"计划已生成，等待用户确认后再开始执行（plan_id={plan.id}）。",
        "next_action": "请在计划卡片中确认、请求修改或拒绝。",
        "requested_by_user_id": str(request.context.user_id),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


@tool(
    ToolMeta(
        name="ask_user_question",
        description=(
            "Ask the current user 1-4 brief multiple-choice questions and pause for their answer. Use "
            "this in Plan Mode (or normal chat) when a missing decision materially changes scope, "
            "risk, cost, recipient, cadence, data source, deliverable format, or irreversible "
            "behavior — instead of assuming a default. Each question has 2-4 distinct options "
            "(label + description); the user can always pick 'Other' to type a free answer, and "
            "multiSelect questions allow multiple picks. This is NOT approval: do not use it to ask "
            "'is this plan OK?' (exit_plan_mode is the approval request). After calling it, end your "
            "turn; the user's answers arrive as the next message."
        ),
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "1-4 multiple-choice questions to ask the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The complete question, clear and specific, ending with '?'.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Very short chip label (≤12 chars), e.g. 'Scope', 'Tracks', 'Cadence'.",
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    "2-4 distinct choices; each an object with label (1-5 words) and "
                                    "description (trade-offs). 'Other' free-text is offered automatically — "
                                    "do not add it yourself."
                                ),
                                "items": {"type": "object"},
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow selecting multiple options (default false).",
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
                "blocking": {
                    "type": "boolean",
                    "description": "Whether planning cannot proceed until answered (default true).",
                },
            },
            "required": ["questions"],
        },
        category="plan",
        display_name="Ask User Question",
        icon="❓",
        read_only=True,
        parallel_safe=False,
        governance="safe",
        adapter="request",
    )
)
async def ask_user_question(request: ToolExecutionRequest) -> str:
    """Pause planning to ask the current user multiple-choice question(s) (CC-align Phase B).

    Read-only: it neither writes nor calls out — it surfaces the questions to the
    same user/session and signals the agent to end its turn and wait. The user's
    answers arrive as the next message and the Plan Mode turn resumes (Plan Mode
    stays active because exit_plan_mode was not called). Mirrors CC's
    AskUserQuestion shape: questions[1-4] each with header + options(label,
    description) + multiSelect; 'Other' free-text is always available client-side.
    """
    args = dict(request.arguments or {})
    raw_questions = args.get("questions")
    # Back-compat: accept a single {question, options, multiSelect} shorthand.
    if not isinstance(raw_questions, list) or not raw_questions:
        single = str(args.get("question") or "").strip()
        if single:
            raw_questions = [
                {"question": single, "options": args.get("options"), "multiSelect": args.get("multiSelect")}
            ]

    normalized: list[dict[str, Any]] = []
    questions_list = raw_questions if isinstance(raw_questions, list) else []
    for entry in questions_list[:4]:  # CC: max 4 questions
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("question") or "").strip()
        if not text:
            continue
        options: list[dict[str, str]] = []
        raw_options = entry.get("options") if isinstance(entry.get("options"), list) else []
        for opt in raw_options:
            if isinstance(opt, dict) and str(opt.get("label") or "").strip():
                options.append(
                    {"label": str(opt["label"]).strip(), "description": str(opt.get("description") or "").strip()}
                )
            elif isinstance(opt, str) and opt.strip():
                options.append({"label": opt.strip(), "description": ""})
        normalized.append(
            {
                "question": text,
                "header": str(entry.get("header") or "").strip()[:12],
                "options": options,
                "multiSelect": bool(entry.get("multiSelect", False)),
            }
        )

    if not normalized:
        return json.dumps(
            {
                "status": "error",
                "error_code": "missing_question",
                "message": "ask_user_question requires a non-empty 'questions' list (each with a question and options).",
            },
            ensure_ascii=False,
        )

    payload = {
        "status": "awaiting_user_clarification",
        "questions": normalized,
        "blocking": bool(args.get("blocking", True)),
        "next_action": (
            "END your turn now — the question card is shown to the user. They pick options (or 'Other' "
            "free text); their answers arrive as the next message. Do NOT assume answers or call "
            "exit_plan_mode until they reply."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


@tool(
    ToolMeta(
        name="request_plan_mode",
        description=(
            "Request to enter Plan Mode for the current task. Use this when the work warrants drafting "
            "and confirming a plan before acting — multi-step or multi-system changes, irreversible or "
            "externally visible actions (sends, deletes, schedules, posts, payments), ambiguous scope, "
            "or expensive long-running work. You do NOT enter Plan Mode yourself: this surfaces an "
            "approval card to the user. If they approve, Plan Mode starts and you draft a confirmable "
            "plan; if they decline, you continue normally. After calling it, END your turn and wait — "
            "the user's decision arrives as the next message. Do not use it for simple, single-step, or "
            "read-only requests. A prior 'start' instruction is not a bypass when the work is irreversible, "
            "externally visible, high-cost, ambiguous, or multi-system."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why planning this task first helps — name what makes it multi-step, risky, "
                        "ambiguous, or expensive, and what the plan would cover. Shown to the user on "
                        "the approval card."
                    ),
                },
            },
            "required": ["reason"],
        },
        category="plan",
        display_name="Request Plan Mode",
        icon="\U0001f5fa️",
        read_only=True,
        parallel_safe=False,
        governance="safe",
        adapter="request",
    )
)
async def request_plan_mode(request: ToolExecutionRequest) -> str:
    """Request user approval to enter Plan Mode (CC EnterPlanMode parity).

    Two-step async shape: this handler only emits a ``plan_mode_entry_requested``
    signal and the agent ends its turn. The user is the gate — the frontend shows
    an approval card; on approval it sends a message carrying ``plan_mode_requested``
    which drives the existing entry path (``_maybe_handle_plan_mode_entry`` →
    ``classify_plan_mode_entry`` → ``_activate_interactive_plan_mode``). Nothing
    flips into Plan Mode from this tool result alone. Read-only: it neither writes
    nor calls out, it only surfaces the request to the current user.
    """
    # Already inside Plan Mode → requesting entry is meaningless. Use
    # exit_plan_mode (submit the plan) or ask_user_question (clarify) instead.
    if interactive_plan_mode_active():
        return json.dumps(
            {
                "status": "error",
                "error_code": "already_in_plan_mode",
                "message": (
                    "Plan Mode is already active. Draft the plan and submit it with exit_plan_mode, or "
                    "use ask_user_question if a blocking decision is still open."
                ),
            },
            ensure_ascii=False,
        )

    args = dict(request.arguments or {})
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return json.dumps(
            {
                "status": "error",
                "error_code": "missing_reason",
                "message": (
                    "request_plan_mode requires a non-empty 'reason' — explain why planning this task "
                    "first helps so the user can decide whether to approve."
                ),
            },
            ensure_ascii=False,
        )

    payload = {
        "status": "plan_mode_entry_requested",
        "reason": reason,
        "next_action": (
            "END your turn now — the approval card is shown to the user. If they approve, Plan Mode "
            "starts and you draft a confirmable plan next turn; if they decline, continue normally. "
            "Do NOT start the work or assume approval until they reply."
        ),
        "requested_by_user_id": str(request.context.user_id),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
