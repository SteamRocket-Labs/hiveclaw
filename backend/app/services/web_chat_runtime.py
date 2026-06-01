from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_agent_expired
from app.kernel.contracts import ExecutionIdentityRef
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.services.chat_message_parts import (
    build_active_packs_event,
    build_chunk_event,
    build_compaction_event,
    build_done_event,
    build_permission_event,
    build_thinking_event,
    build_tool_call_event,
)
from app.services.llm_error_policy import is_llm_error_message
from app.services import plan_mode_core
from app.services.plan_mode_service import get_plan_mode_service
from app.services.web_chat_broker import web_chat_broker


WEB_CHAT_TURN_TASK_TYPE = "web_chat_turn"
_ACTIVE_STATUSES = ("pending", "running")
_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
_TASKS: dict[str, asyncio.Task] = {}


class ActiveWebChatRunExists(Exception):
    def __init__(self, run: dict[str, Any]) -> None:
        super().__init__("A web chat run is already active for this session")
        self.run = run


def _run_id(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _runtime_task_to_run(task: RuntimeTask) -> dict[str, Any]:
    created_at = getattr(task, "created_at", None)
    started_at = getattr(task, "started_at", None)
    completed_at = getattr(task, "completed_at", None)
    return {
        "run_id": task.id.hex,
        "status": task.status,
        "created_at": created_at.isoformat() if created_at else None,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "result_summary": getattr(task, "result_summary", None),
    }


def conversation_from_history_messages(history_messages) -> list[dict]:
    """Convert persisted chat rows back into provider-compatible conversation entries."""
    conversation: list[dict] = []
    for msg in history_messages:
        if msg.role == "tool_call":
            try:
                tc_data = json.loads(msg.content)
                tc_name = tc_data.get("name", "unknown")
                tc_args = tc_data.get("args", {})
                tc_result = tc_data.get("result", "")
                tc_id = f"call_{msg.id}"
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": tc_name, "arguments": json.dumps(tc_args, ensure_ascii=False)},
                        }
                    ],
                }
                if tc_data.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = tc_data["reasoning_content"]
                conversation.append(assistant_msg)

                tool_result = str(tc_result)
                if len(tool_result) > 50000:
                    logger.info("[WebChatRun] Tool result truncated on reload: {}→50000 chars", len(tool_result))
                    tool_result = tool_result[:50000] + "\n\n[... truncated, full output may be in workspace/tool_results/]"
                conversation.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})
            except Exception as exc:
                logger.debug("[WebChatRun] Skipped malformed tool_call record: {}", exc)
            continue

        if msg.role == "assistant" and is_llm_error_message(msg.content):
            continue

        entry = {"role": msg.role, "content": msg.content}
        if getattr(msg, "thinking", None):
            entry["reasoning_content"] = msg.thinking
        conversation.append(entry)
    return conversation


def register_web_chat_run_for_test(run_id: str, *, cancel_event: asyncio.Event) -> None:
    _CANCEL_EVENTS[str(run_id)] = cancel_event


def unregister_web_chat_run_for_test(run_id: str) -> None:
    _CANCEL_EVENTS.pop(str(run_id), None)
    _TASKS.pop(str(run_id), None)


async def handle_web_chat_disconnect(_run_id: str | None = None) -> None:
    """Disconnecting a subscriber must not cancel the underlying background run."""
    return None


async def broadcast_web_chat_event(agent_id: uuid.UUID, session_id: str | uuid.UUID | None, event: dict[str, Any]) -> None:
    await web_chat_broker.send_session_message(str(agent_id), str(session_id) if session_id else None, event)


async def _find_active_run(db: AsyncSession, *, agent_id: uuid.UUID, session_id: str | uuid.UUID) -> RuntimeTask | None:
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.task_type == WEB_CHAT_TURN_TASK_TYPE,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_active_web_chat_run(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
) -> dict[str, Any] | None:
    task = await _find_active_run(db, agent_id=agent_id, session_id=session_id)
    return _runtime_task_to_run(task) if task else None


async def start_web_chat_run(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    display_content: str = "",
    file_name: str = "",
    plan_mode_requested: bool = False,
) -> dict[str, Any]:
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    active = await _find_active_run(db, agent_id=agent.id, session_id=session.id)
    if active:
        raise ActiveWebChatRunExists(_runtime_task_to_run(active))

    run_uuid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    saved_content = display_content if display_content else content
    if file_name:
        saved_content = f"[file:{file_name}]\n{saved_content}"

    db.add(
        ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content=saved_content,
            conversation_id=str(session.id),
        )
    )
    session.last_message_at = now
    if not getattr(session, "title", "") or str(session.title).startswith("Session "):
        title_src = display_content if display_content else content
        clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
        if file_name and not clean_title:
            clean_title = f"📎 {file_name}"
        session.title = clean_title[:40] if clean_title else content[:40]

    runtime_task = RuntimeTask(
        id=run_uuid,
        task_type=WEB_CHAT_TURN_TASK_TYPE,
        status="running",
        parent_agent_id=agent.id,
        child_agent_id=agent.id,
        child_agent_name=getattr(agent, "name", None),
        prompt=content,
        trace_id=f"web-chat:{run_uuid.hex}",
        parent_session_id=str(session.id),
        child_session_id=str(session.id),
        depth=1,
        started_at=now,
        metadata_json={
            "user_id": str(user.id),
            "session_id": str(session.id),
            "display_content": display_content,
            "file_name": file_name,
            "source": "web",
            "cancelled_by_user": False,
            "plan_mode_requested": bool(plan_mode_requested),
        },
    )
    db.add(runtime_task)
    await db.commit()

    cancel_event = asyncio.Event()
    _CANCEL_EVENTS[run_uuid.hex] = cancel_event
    task = asyncio.create_task(execute_web_chat_run(run_uuid, cancel_event=cancel_event))
    _TASKS[run_uuid.hex] = task
    task.add_done_callback(lambda _task, run_id=run_uuid.hex: _TASKS.pop(run_id, None))

    payload = _runtime_task_to_run(runtime_task)
    await broadcast_web_chat_event(agent.id, session.id, {"type": "run_started", **payload})
    return payload


async def cancel_web_chat_run(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    run_id: str | uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    run_uuid = _run_id(run_id)
    result = await db.execute(
        select(RuntimeTask).where(
            RuntimeTask.id == run_uuid,
            RuntimeTask.task_type == WEB_CHAT_TURN_TASK_TYPE,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(_ACTIVE_STATUSES),
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Active run not found")

    cancel_event = _CANCEL_EVENTS.get(run_uuid.hex)
    if cancel_event is not None:
        cancel_event.set()
    metadata = dict(task.metadata_json or {})
    metadata["cancelled_by_user"] = True
    metadata["cancelled_by_user_id"] = str(user_id)
    task.metadata_json = metadata
    task.status = "killed"
    task.result_summary = task.result_summary or "Generation stopped by user."
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()

    payload = _runtime_task_to_run(task)
    await broadcast_web_chat_event(agent_id, session_id, {"type": "run_cancelled", **payload})
    return payload


async def _claim_pending_reply_suffix_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str | None,
) -> str:
    if not session_id:
        return ""

    from app.services.pending_reply_service import (
        claim_and_fulfill_pending_replies,
        format_pending_reply_context,
        sender_identity_from_session,
    )

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(str(session_id))))
    session = session_result.scalar_one_or_none()
    sender_identity = sender_identity_from_session(session)
    if not sender_identity:
        return ""

    claimed = await claim_and_fulfill_pending_replies(db, agent_id=agent_id, sender_identity=sender_identity)
    if not claimed:
        return ""
    await db.commit()
    return format_pending_reply_context(claimed)


async def _persist_assistant_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    content: str,
    thinking: str | None,
) -> None:
    async with _async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content=content,
                thinking=thinking,
                conversation_id=session_id,
            )
        )
        await db.commit()


async def _persist_tool_call(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    data: dict[str, Any],
) -> None:
    raw_result = data.get("result") or ""
    raw_str = str(raw_result)
    if len(raw_str) > 50000:
        raw_str = raw_str[:50000] + "\n\n[... truncated]"
    async with _async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": data.get("name", ""),
                        "args": data.get("args"),
                        "status": "done",
                        "result": raw_str,
                        "reasoning_content": data.get("reasoning_content"),
                    },
                    ensure_ascii=False,
                ),
                conversation_id=session_id,
            )
        )
        await db.commit()


async def _persist_runtime_event(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    data: dict[str, Any],
) -> None:
    async with _async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="system",
                content=json.dumps(data, ensure_ascii=False),
                conversation_id=session_id,
            )
        )
        await db.commit()


def _simulation_title(content: str) -> str:
    return content[:80] if content else ""


def _plan_mode_generated_message(plan: Any) -> str:
    plan_id = getattr(plan, "id", "")
    status = getattr(plan, "status", "awaiting_confirmation")
    if status == "planning_failed":
        metadata = getattr(plan, "metadata_json", None) or {}
        errors = metadata.get("planning_errors") if isinstance(metadata, dict) else None
        error_text = ""
        if isinstance(errors, list):
            visible_errors = [str(item).strip() for item in errors if str(item).strip()]
            if visible_errors:
                error_text = "失败原因：" + "；".join(visible_errors[:3]) + "。"
        elif isinstance(errors, str) and errors.strip():
            error_text = f"失败原因：{errors.strip()}。"
        return (
            f"已进入计划模式，但计划生成失败（plan_id={plan_id}）。"
            f"{error_text}"
            "请在计划卡片中重新生成、修改后重试或拒绝；我不会开始执行。"
        )
    return (
        f"已进入计划模式，并生成一份待确认计划（plan_id={plan_id}）。"
        "请在计划卡片中确认、修改或拒绝；确认后我再开始执行。"
    )


def _activate_interactive_plan_mode(
    runtime_session_context: Any | None,
    *,
    original_request: str,
    decision: plan_mode_core.PlanModeEntryDecision,
    session_id: str | None,
) -> dict[str, Any]:
    handoff_target = "objective_trigger" if decision.action_kind == "create_enabled_trigger" else "long_task"
    metadata: dict[str, Any] = {
        "active": True,
        "original_request": original_request,
        "intent_type": decision.intent_type or "long_task",
        "action_kind": decision.action_kind,
        "tool_name": decision.tool_name,
        "reason": decision.reason,
        "handoff_target": handoff_target,
    }
    if _is_deep_research_chat_request(original_request):
        metadata.update(
            {
                "handoff_target": "deep_research",
                "deep_research": True,
                "deep_research_args": _deep_research_chat_arguments(original_request),
            }
        )
    if runtime_session_context is not None:
        runtime_session_context.metadata["plan_mode"] = metadata
    logger.info(
        "[WebChatRun] Interactive Plan Mode activated session={} intent={} target={}",
        session_id,
        metadata.get("intent_type"),
        metadata.get("handoff_target"),
    )
    return metadata


def _interactive_plan_mode_suffix(metadata: dict[str, Any]) -> str:
    original_request = str(metadata.get("original_request") or "").strip()
    handoff_target = str(metadata.get("handoff_target") or "long_task")
    return (
        "Plan Mode is active. The user indicated that they do not want you to execute yet. "
        "You MUST NOT execute the requested work, mutate workspace files, create triggers/tasks/objectives, "
        "send external messages, delegate work, run commands, or call any non-read-only tools.\n\n"
        "## Original request\n"
        f"{original_request or '(not provided)'}\n\n"
        "## Interactive Planning Workflow\n"
        "You are pair-planning with the user. Follow this loop until the plan is ready:\n"
        "1. Explore — use only read-only tools to inspect code, memory, schedules, objectives, or web facts when needed.\n"
        "2. Capture findings in your own working context; do not expose internal ledger paths or raw tool scripts.\n"
        "3. Ask concise clarification questions only for decisions that materially change scope, risk, cost, recipients, "
        "credentials, or irreversible behavior.\n"
        "4. When the plan is ready, call exit_plan_mode with a concise user-facing plan. Do not ask for approval in prose; "
        "exit_plan_mode creates the confirmation card.\n\n"
        "## Plan Quality Bar\n"
        "- Include context, objective, concrete ordered steps, critical files/artifacts or research lanes when applicable, "
        "success criteria, stop conditions, risk, cost, and verification.\n"
        "- Prefer one recommended approach, not a list of speculative alternatives.\n"
        "- Separate facts, assumptions, and open questions.\n"
        "- If this is Deep Research, plan research scope, evidence lanes, source quality standards, synthesis shape, and "
        "final artifacts; confirmation will hand off to the Deep Research runtime.\n"
        f"- Expected handoff target after confirmation: {handoff_target}.\n\n"
        "Your turn should end by either asking necessary clarification questions or calling exit_plan_mode."
    )


async def _maybe_sync_created_task(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    assistant_response: str,
    tenant_id: uuid.UUID | None = None,
    session_id: str | None = None,
) -> str:
    """Gate the chat "create a task" auto-sync behind Plan Mode (§9.0 / §9.2).

    A regex-detected task-creation intent used to immediately persist a ``Task``
    and background-execute it (``execute_task``). That is an autonomous
    ``start_long_task`` action: per Plan Mode it must NOT run without a confirmed
    plan. So instead of executing, we materialise an awaiting ``PlanRequest`` from
    the detected task title and tell the user to confirm it. The agent never
    backgrounds a task off a bare regex match again.

    Fail-closed: if the plan cannot be created the action is *still* not executed
    (the reply is returned unchanged) — a degraded ledger must never downgrade
    into a silent background run.
    """
    task_match = re.search(
        r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\s]*(.+)",
        content,
        re.IGNORECASE,
    )
    if not task_match or is_llm_error_message(assistant_response):
        return assistant_response
    task_title = task_match.group(1).strip()
    if not task_title:
        return assistant_response

    try:
        plan = await get_plan_mode_service().ensure_awaiting_plan(
            agent_id=agent_id,
            action_kind="start_long_task",
            tool_name="manage_tasks",
            arguments={"action": "create", "title": task_title, "description": content},
            source="web_chat",
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=None,
        )
    except Exception as exc:
        logger.error("[WebChatRun] Failed to create plan for task auto-sync: {}", exc)
        return assistant_response

    return assistant_response + f"\n\n📋 “{task_title}” 需要先确认计划再执行。{_plan_mode_generated_message(plan)}"


_DEEP_RESEARCH_CHAT_RE = re.compile(r"(deep\s*research|deepresearch|深度研究|深度调研)", re.IGNORECASE)


def _is_deep_research_chat_request(content: str) -> bool:
    return bool(_DEEP_RESEARCH_CHAT_RE.search(str(content or "")))


def _deep_research_chat_arguments(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    is_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
    is_industry = bool(re.search(r"(行业|全景|赛道|市场|landscape|industry|sector)", text, re.IGNORECASE))
    is_full_depth = bool(re.search(r"(全景|深度|完整|报告|full|deep|comprehensive)", text, re.IGNORECASE))
    return {
        "question": text,
        "mode": "industry_research" if is_industry else "topic_deep_dive",
        "depth": "full" if is_full_depth else "standard",
        "output_language": "zh-CN" if is_chinese else "",
        "output_format": "markdown",
        "source_policy": "primary_preferred",
    }


def _plan_mode_recommendation_message(decision: plan_mode_core.PlanModeEntryDecision) -> str:
    subject = decision.title or "这个请求"
    return (
        f"这个请求看起来会创建未来自动执行或持续监控：{subject}\n\n"
        f"{plan_mode_core.PLAN_MODE_RECOMMENDATION_MARKER}。"
        "如果你同意，请回复“进入计划模式”；如果你要跳过，请明确回复“不用计划模式，直接创建”。"
    )


async def _record_plan_mode_recommendation(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    session_id: str | None,
    runtime_task_id: uuid.UUID | None,
    content: str,
    decision: plan_mode_core.PlanModeEntryDecision,
) -> None:
    if user_id is None or not session_id:
        return
    from app.services.plan_mode_recommendation_service import create_plan_recommendation

    async with _async_session() as db:
        await create_plan_recommendation(
            db,
            agent_id=agent_id,
            recommended_to_user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            source="web_chat",
            original_request=content,
            title=decision.title or content[:120],
            intent_type=decision.intent_type or "autonomous_wake",
            action_kind=decision.action_kind or "create_enabled_trigger",
            tool_name=decision.tool_name or "set_trigger",
        )
        await db.commit()


async def _accept_latest_plan_mode_recommendation(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str | None,
):
    if user_id is None or not session_id:
        return None
    from app.services.plan_mode_recommendation_service import accept_latest_recommendation_for_user

    async with _async_session() as db:
        recommendation = await accept_latest_recommendation_for_user(
            db,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )
        if recommendation is not None:
            await db.commit()
        return recommendation


async def _maybe_handle_plan_mode_entry(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    session_id: str | None,
    runtime_task_id: uuid.UUID | None,
    content: str,
    plan_mode_requested: bool = False,
    runtime_session_context: Any | None = None,
) -> str | None:
    """Handle the UX-layer Plan Mode entry before normal agent execution.

    Schedule/monitor intents recommend Plan Mode and stop. Explicit Plan Mode
    selection or long-task intents materialise an awaiting plan. The execution
    safety gate remains in the tool/runtime layer.
    """
    decision = plan_mode_core.classify_plan_mode_entry(content, explicit=plan_mode_requested)
    if decision.mode in {"none", "declined"}:
        return None

    if decision.mode == "recommend":
        await _record_plan_mode_recommendation(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            content=content,
            decision=decision,
        )
        return _plan_mode_recommendation_message(decision)

    accepted_recommendation = None
    if decision.mode == "explicit" and plan_mode_core.is_plan_mode_acceptance_reply(content):
        try:
            accepted_recommendation = await _accept_latest_plan_mode_recommendation(
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("[WebChatRun] Plan recommendation accept binding failed (non-fatal): {}", exc)
            accepted_recommendation = None
    if accepted_recommendation is not None:
        decision = plan_mode_core.PlanModeEntryDecision(
            mode="explicit",
            intent_type=getattr(accepted_recommendation, "intent_type", None) or "autonomous_wake",
            action_kind=getattr(accepted_recommendation, "action_kind", None) or "create_enabled_trigger",
            tool_name=getattr(accepted_recommendation, "tool_name", None) or "set_trigger",
            title=getattr(accepted_recommendation, "title", None) or getattr(accepted_recommendation, "original_request", "")[:120],
            reason="accepted_plan_mode_recommendation",
        )
        content = getattr(accepted_recommendation, "original_request", None) or content

    if not decision.action_kind or not decision.tool_name:
        return None

    _activate_interactive_plan_mode(
        runtime_session_context,
        original_request=content,
        decision=decision,
        session_id=session_id,
    )
    return None


async def _update_runtime_task(
    run_uuid: uuid.UUID,
    *,
    status: str,
    result_summary: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    async with _async_session() as db:
        result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid))
        task = result.scalar_one_or_none()
        if task is None:
            return
        task.status = status
        if result_summary is not None:
            task.result_summary = result_summary
        if metadata_json:
            metadata = dict(task.metadata_json or {})
            metadata.update(metadata_json)
            task.metadata_json = metadata
        if status in {"completed", "failed", "killed", "skipped"} and task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _load_runtime_context(run_uuid: uuid.UUID) -> tuple[RuntimeTask, Agent, User, LLMModel | None, LLMModel | None, list[ChatMessage]]:
    async with _async_session() as db:
        task_result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid))
        runtime_task = task_result.scalar_one_or_none()
        if runtime_task is None:
            raise RuntimeError(f"RuntimeTask {run_uuid.hex} not found")

        agent_result = await db.execute(select(Agent).where(Agent.id == runtime_task.parent_agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            raise RuntimeError(f"Agent {runtime_task.parent_agent_id} not found")

        metadata = dict(runtime_task.metadata_json or {})
        user_id = uuid.UUID(str(metadata.get("user_id")))
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise RuntimeError(f"User {user_id} not found")

        primary_model = None
        fallback_model = None
        if agent.primary_model_id:
            primary_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            primary_model = primary_result.scalar_one_or_none()
        if agent.fallback_model_id:
            fallback_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            fallback_model = fallback_result.scalar_one_or_none()
        if not primary_model and fallback_model:
            primary_model = fallback_model
            fallback_model = None
        if primary_model and agent.tenant_id:
            from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

            default_runtime_model = await resolve_default_model_for_tenant(
                db,
                agent.tenant_id,
                exclude_model_id=primary_model.id,
            )
            primary_model, fallback_model = choose_runtime_model_pair(
                primary_model,
                fallback_model,
                default_runtime_model,
            )

        from app.services.memory_service import compute_history_limit

        history_limit = compute_history_limit(
            primary_model.provider if primary_model else "openai",
            primary_model.model if primary_model else "",
            getattr(primary_model, "max_input_tokens", None) if primary_model else None,
        )
        history_result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent.id,
                ChatMessage.conversation_id == str(runtime_task.parent_session_id),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(history_limit)
        )
        history_messages = list(reversed(history_result.scalars().all()))
        return runtime_task, agent, user, primary_model, fallback_model, history_messages


async def execute_web_chat_run(run_id: str | uuid.UUID, *, cancel_event: asyncio.Event | None = None) -> None:
    run_uuid = _run_id(run_id)
    run_key = run_uuid.hex
    cancel_event = cancel_event or _CANCEL_EVENTS.setdefault(run_key, asyncio.Event())
    streamed_chunks: list[str] = []
    thinking_content: list[str] = []

    try:
        runtime_task, agent, user, llm_model, fallback_model, history_messages = await _load_runtime_context(run_uuid)
        session_id = str(runtime_task.parent_session_id)
        conversation = conversation_from_history_messages(history_messages)
        prompt = runtime_task.prompt or ""
        metadata = runtime_task.metadata_json if isinstance(runtime_task.metadata_json, dict) else {}

        if getattr(agent, "agent_type", None) == "openclaw":
            async with _async_session() as db:
                db.add(
                    GatewayMessage(
                        agent_id=agent.id,
                        sender_user_id=user.id,
                        conversation_id=session_id,
                        content=prompt,
                        status="pending",
                    )
                )
                await db.commit()
            assistant_response = "Message forwarded to OpenClaw agent. Waiting for response..."
            await _persist_assistant_message(
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=assistant_response,
                thinking=None,
            )
            await _update_runtime_task(run_uuid, status="completed", result_summary=assistant_response[:500])
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(assistant_response))
            return

        runtime_session_context = await web_chat_broker.get_or_create_runtime_session(str(agent.id), session_id)

        plan_mode_response = await _maybe_handle_plan_mode_entry(
            agent_id=agent.id,
            user_id=getattr(user, "id", None),
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=session_id,
            runtime_task_id=run_uuid,
            content=prompt,
            plan_mode_requested=bool(metadata.get("plan_mode_requested")),
            runtime_session_context=runtime_session_context,
        )
        if plan_mode_response is not None:
            await _persist_assistant_message(
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=plan_mode_response,
                thinking=None,
            )
            await _update_runtime_task(run_uuid, status="completed", result_summary=plan_mode_response[:500])
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(plan_mode_response))
            return

        if not llm_model:
            assistant_response = f"[LLM Error] {agent.name} has no LLM model configured. Please select a model in the agent's Settings tab."
            await _persist_assistant_message(
                agent_id=agent.id,
                user_id=user.id,
                session_id=session_id,
                content=assistant_response,
                thinking=None,
            )
            await _update_runtime_task(run_uuid, status="failed", result_summary=assistant_response[:500])
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(assistant_response))
            return

        async def stream_to_ws(text: str) -> None:
            streamed_chunks.append(text)
            await broadcast_web_chat_event(agent.id, session_id, build_chunk_event(text))

        async def thinking_to_ws(text: str) -> None:
            thinking_content.append(text)
            await broadcast_web_chat_event(agent.id, session_id, build_thinking_event(text))

        async def runtime_event_to_ws(data: dict[str, Any]) -> None:
            if data.get("type") == "permission":
                event_payload = build_permission_event(data)
            elif data.get("type") == "session_compact":
                event_payload = build_compaction_event(data)
            elif data.get("type") == "pack_activation":
                event_payload = build_active_packs_event(data)
            else:
                event_payload = data
            await broadcast_web_chat_event(agent.id, session_id, event_payload)
            if data.get("type") in {"permission", "session_compact", "pack_activation"}:
                await _persist_runtime_event(agent_id=agent.id, user_id=user.id, session_id=session_id, data=data)

        pending_reply_suffix = ""
        try:
            async with _async_session() as pending_db:
                pending_reply_suffix = await _claim_pending_reply_suffix_for_session(
                    pending_db,
                    agent_id=agent.id,
                    session_id=session_id,
                )
        except Exception as exc:
            logger.warning("[WebChatRun] Pending reply injection failed (non-fatal): {}", exc)

        trusted_decline = plan_mode_core.trusted_decline_metadata(
            content=prompt,
            messages=history_messages,
            explicit=bool(metadata.get("plan_mode_requested")),
        )
        if trusted_decline:
            try:
                from app.services.plan_mode_recommendation_service import decline_latest_recommendation_for_user

                async with _async_session() as recommendation_db:
                    recommendation = await decline_latest_recommendation_for_user(
                        recommendation_db,
                        agent_id=agent.id,
                        user_id=user.id,
                        session_id=session_id,
                    )
                    if recommendation is None:
                        trusted_decline = None
                    else:
                        trusted_decline["recommendation_id"] = str(recommendation.id)
                        await recommendation_db.commit()
            except Exception as exc:
                logger.warning("[WebChatRun] Plan recommendation decline binding failed (non-fatal): {}", exc)
                trusted_decline = None
        if trusted_decline:
            plan_decline_suffix = (
                "Plan Mode governance: the runtime verified that the user declined the immediately preceding "
                "Plan Mode recommendation. If you create or update a scheduled/monitoring trigger as a direct "
                "follow-up, call the trigger tool normally. Do not add opt-out fields to tool arguments, and do "
                "not use this opt-out for long tasks, delegation, or other high-risk actions."
            )
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, plan_decline_suffix) if part)

        if trusted_decline:
            runtime_session_context.metadata[plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY] = trusted_decline
        else:
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        active_plan_mode_metadata = runtime_session_context.metadata.get("plan_mode")
        if isinstance(active_plan_mode_metadata, dict) and active_plan_mode_metadata.get("active"):
            pending_reply_suffix = "\n\n".join(
                part for part in (pending_reply_suffix, _interactive_plan_mode_suffix(active_plan_mode_metadata)) if part
            )

        plan_mode_submitted = False

        def _tool_result_needs_plan(data: dict[str, Any]) -> bool:
            if data.get("name") != "exit_plan_mode" or data.get("status") != "done":
                return False
            try:
                payload = json.loads(str(data.get("result") or "{}"))
            except Exception:
                return False
            return payload.get("status") == "needs_plan"

        async def tool_call_to_ws(data: dict[str, Any]) -> None:  # type: ignore[no-redef]
            nonlocal plan_mode_submitted
            await broadcast_web_chat_event(agent.id, session_id, build_tool_call_event(data))
            if data.get("status") == "done":
                if _tool_result_needs_plan(data):
                    plan_mode_submitted = True
                await _persist_tool_call(agent_id=agent.id, user_id=user.id, session_id=session_id, data=data)

        plan_mode_token = None
        try:
            if isinstance(active_plan_mode_metadata, dict) and active_plan_mode_metadata.get("active"):
                from app.services.plan_mode_runtime_context import set_interactive_plan_mode

                plan_mode_token = set_interactive_plan_mode(active_plan_mode_metadata)
            result = await invoke_agent(
                AgentInvocationRequest(
                    model=llm_model,
                    fallback_model=fallback_model,
                    messages=conversation,
                    agent_name=agent.name,
                    role_description=agent.role_description or "",
                    agent_id=agent.id,
                    user_id=user.id,
                    execution_identity=ExecutionIdentityRef(
                        identity_type="delegated_user",
                        identity_id=user.id,
                        label=f"{user.display_name or user.username} via web",
                    ),
                    on_chunk=stream_to_ws,
                    on_tool_call=tool_call_to_ws,
                    on_thinking=thinking_to_ws,
                    on_event=runtime_event_to_ws,
                    supports_vision=getattr(llm_model, "supports_vision", False),
                    memory_session_id=session_id,
                    memory_messages=conversation,
                    cancel_event=cancel_event,
                    session_context=runtime_session_context,
                    system_prompt_suffix=pending_reply_suffix,
                )
            )
        finally:
            if plan_mode_token is not None:
                from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

                reset_interactive_plan_mode(plan_mode_token)
            if plan_mode_submitted:
                runtime_session_context.metadata.pop("plan_mode", None)
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        assistant_response = await _maybe_sync_created_task(
            agent_id=agent.id,
            user_id=user.id,
            content=prompt,
            assistant_response=result.content,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=session_id,
        )
        thinking = "".join(thinking_content) if thinking_content else None
        await _persist_assistant_message(
            agent_id=agent.id,
            user_id=user.id,
            session_id=session_id,
            content=assistant_response,
            thinking=thinking,
        )
        status = "killed" if cancel_event.is_set() else ("failed" if is_llm_error_message(assistant_response) else "completed")
        metadata_update = {"cancelled_by_user": bool(cancel_event.is_set())}
        await _update_runtime_task(
            run_uuid,
            status=status,
            result_summary=_simulation_title(assistant_response),
            metadata_json=metadata_update,
        )
        await broadcast_web_chat_event(agent.id, session_id, build_done_event(assistant_response, thinking=thinking))
    except Exception as exc:
        logger.exception("[WebChatRun] Run {} failed", run_uuid.hex)
        was_cancelled = cancel_event.is_set()
        await _update_runtime_task(
            run_uuid,
            status="killed" if was_cancelled else "failed",
            result_summary="Generation stopped by user." if was_cancelled else f"Web chat run failed: {type(exc).__name__}",
            metadata_json={"cancelled_by_user": True} if was_cancelled else {"error": str(exc)[:500]},
        )
        if was_cancelled:
            return
        try:
            runtime_task, agent, user, *_rest = await _load_runtime_context(run_uuid)
            session_id = str(runtime_task.parent_session_id)
            await broadcast_web_chat_event(
                agent.id,
                session_id,
                {"type": "error", "content": "[LLM Error] AI 模型调用异常，请稍后重试。"},
            )
        except Exception:
            pass
    finally:
        _CANCEL_EVENTS.pop(run_key, None)


# Kept as an overridable module global for tests and for parity with other services.
from app.database import async_session as _async_session  # noqa: E402
