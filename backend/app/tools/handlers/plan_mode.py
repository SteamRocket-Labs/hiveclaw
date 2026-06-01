"""Interactive Plan Mode tools."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from app.services import plan_mode_core
from app.services.plan_mode_runtime_context import interactive_plan_mode_metadata
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
    if handoff_target == "objective_trigger":
        return {"type": "manual"}
    return {"type": "none"}


def _handoff(args: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("handoff_target") or metadata.get("handoff_target") or "long_task").strip() or "long_task"
    payload = args.get("handoff_payload") if isinstance(args.get("handoff_payload"), dict) else {}
    if target == "deep_research":
        payload = {**dict(metadata.get("deep_research_args") or {}), **dict(payload or {})}
    return {
        "target": target,
        "create_objective": target == "objective_trigger",
        "create_trigger": target == "objective_trigger",
        "payload": payload,
    }


def _tenant_id(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


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
                "plan_markdown": {"type": "string", "description": "Concise markdown plan preview for the user."},
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
            },
            "required": ["title", "objective", "plan_markdown", "steps", "success_criteria", "stop_conditions"],
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
    handoff = _handoff(args, metadata)
    intent_type = str(metadata.get("intent_type") or args.get("intent_type") or "long_task")
    if intent_type not in plan_mode_core.INTENT_TYPES:
        intent_type = "long_task"
    original_request = str(metadata.get("original_request") or args.get("original_request") or args.get("objective") or "")
    title = str(args.get("title") or original_request[:80] or "Plan Mode plan").strip()
    objective = str(args.get("objective") or original_request or title).strip()
    fill = {
        "title": title,
        "objective": objective,
        "motivation": original_request or objective,
        "steps": _steps(args.get("steps")),
        "success_criteria": _string_list(args.get("success_criteria")),
        "wake_policy": _wake_policy(args.get("wake_policy"), handoff_target=handoff["target"]),
        "required_capabilities": _string_list(args.get("required_capabilities")),
        "external_side_effects": args.get("external_side_effects") if isinstance(args.get("external_side_effects"), list) else [],
        "risk_assessment": _risk(args.get("risk_assessment")),
        "estimated_cost": _estimated_cost(args.get("estimated_cost")),
        "stop_conditions": _string_list(args.get("stop_conditions")),
        "handoff": handoff,
        "assumptions": _string_list(args.get("assumptions")),
        "open_questions": _string_list(args.get("open_questions")),
    }
    if metadata.get("deep_research"):
        fill["deep_research"] = {
            "question": (metadata.get("deep_research_args") or {}).get("question") or original_request,
            "planned_from": "interactive_plan_mode",
        }

    signature_payload = {
        "agent_id": str(request.context.agent_id),
        "session_id": request.context.session_id,
        "intent_type": intent_type,
        "original_request": original_request,
        "fill": fill,
    }
    signature = "interactive_plan:" + hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]

    plan = await get_plan_mode_service().ensure_awaiting_plan_from_fill(
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

    payload = {
        "status": "needs_plan",
        "plan_id": str(plan.id),
        "plan_version": getattr(plan, "plan_version", 1),
        "plan_hash": getattr(plan, "plan_hash", None),
        "plan_json": getattr(plan, "plan_json", None) or fill,
        "summary": f"计划已生成，等待用户确认后再开始执行（plan_id={plan.id}）。",
        "next_action": "请在计划卡片中确认、请求修改或拒绝。",
        "requested_by_user_id": str(request.context.user_id),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
