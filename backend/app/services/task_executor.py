"""Background task executor — runs agent tasks through the unified runtime."""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.kernel.contracts import ExecutionIdentityRef
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.task import Task, TaskLog
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.session_service import create_chat_session, session_conversation_id


TASK_EXECUTION_ADDENDUM = """## TASK EXECUTION MODE

You are executing an assigned task autonomously. Take initiative and work toward completion \
without waiting for follow-up input.

### How to Execute
- Break complex tasks into steps and execute each sequentially.
- Start with kernel tools. When you need more capability, use `load_skill` or `tool_search` to activate the right toolset.
- If the task involves contacting someone or searching external systems, load the matching skill first.
- Focus on doing the work — report progress only when it materially changes execution state.

### When Things Fail
- If a tool call fails, read the error, diagnose the root cause, and try a different approach.
- If the same approach fails 3 times with different errors, stop and report with specific details.
- If blocked by missing permissions, data, or dependencies, state the blocker explicitly and stop.

### Completion Criteria
Before marking the task complete, verify:
1. All subtasks have been executed (not just planned).
2. Results are concrete — actual output, file paths, or message confirmations.
3. Errors encountered are reported with their resolutions.

### Final Report Format
- Outcome: what was completed and what remains open.
- Evidence: files changed, commands run, URLs checked, tool confirmations.
- Blockers: unresolved issues or dependencies that prevented completion.
- Next Steps: only include if follow-up work is still required.
"""


def _build_task_user_prompt(
    task_type: str,
    task_title: str,
    task_description: str,
    supervision_target: str,
) -> str:
    if task_type == "supervision":
        user_prompt = f"[督办任务] {task_title}"
        if task_description:
            user_prompt += f"\n任务描述: {task_description}"
        if supervision_target:
            user_prompt += f"\n督办对象: {supervision_target}"
        return user_prompt + "\n\n请执行此督办任务：联系督办对象，了解进展，并汇报结果。"

    user_prompt = f"[任务执行] {task_title}"
    if task_description:
        user_prompt += f"\n任务描述: {task_description}"
    return user_prompt + "\n\n请认真完成此任务，给出详细的执行结果。"


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Flow:
      - todo tasks: pending → doing → done
      - supervision tasks: pending → doing → pending (stays active, just logs result)
    """
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    # Step 1: Mark as doing
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            logger.warning(f"[TaskExec] Task {task_id} not found")
            return

        task.status = "doing"
        db.add(TaskLog(task_id=task_id, content="🤖 开始执行任务..."))
        await db.commit()
        task_title = task.title
        task_description = task.description or ""
        task_type = task.type  # 'todo' or 'supervision'
        supervision_target = task.supervision_target_name or ""

    user_prompt = _build_task_user_prompt(
        task_type,
        task_title,
        task_description,
        supervision_target,
    )
    runtime_messages = [{"role": "user", "content": user_prompt}]

    # Step 2: Load agent + model
    async with async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            await _log_error(task_id, "数字员工未找到")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        model_id = agent.primary_model_id or agent.fallback_model_id
        if not model_id:
            await _log_error(task_id, f"{agent.name} 未配置 LLM 模型，无法执行任务")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        model_result = await db.execute(
            select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
        )
        model = model_result.scalar_one_or_none()
        # Load fallback model for runtime resilience (mirrors websocket/heartbeat behavior)
        fallback_model = None
        if agent.fallback_model_id and agent.fallback_model_id != model_id:
            fb_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            fallback_model = fb_result.scalar_one_or_none()
        if not model:
            await _log_error(task_id, "配置的模型不存在")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        agent_name = agent.name
        creator_id = agent.creator_id
        participant_result = await db.execute(
            select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
        )
        agent_participant = participant_result.scalar_one_or_none()
        agent_participant_id = agent_participant.id if agent_participant else None

        reflection_session = await create_chat_session(
            db,
            agent_id=agent_id,
            user_id=creator_id,
            participant_id=agent_participant_id,
            source_channel="task",
            title=f"🧾 Task: {task_title}"[:200],
        )
        reflection_session_id = reflection_session.id

        db.add(
            ChatMessage(
                agent_id=agent_id,
                conversation_id=session_conversation_id(reflection_session),
                role="user",
                content=user_prompt,
                user_id=creator_id,
                participant_id=agent_participant_id,
            )
        )
        await db.commit()

    # Step 4: Call unified runtime
    try:
        logger.info(f"[TaskExec] Invoking unified runtime for task: {task_title}")

        async def _on_tool_call(data: dict) -> None:
            if data.get("status") != "done":
                return
            async with async_session() as tc_db:
                tc_db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=session_conversation_id(reflection_session),
                        role="tool_call",
                        content=json.dumps(
                            {
                                "name": data.get("name"),
                                "args": data.get("args"),
                                "status": "done",
                                "result": str(data.get("result", ""))[:2000],
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        user_id=creator_id,
                        participant_id=agent_participant_id,
                    )
                )
                await tc_db.commit()

        result = await invoke_agent(
            AgentInvocationRequest(
                model=model,
                fallback_model=fallback_model,
                messages=runtime_messages,
                memory_messages=runtime_messages,
                agent_name=agent_name,
                role_description=agent.role_description or "",
                agent_id=agent_id,
                user_id=creator_id,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=agent_id,
                    label=f"Agent: {agent_name} (task)",
                ),
                system_prompt_suffix=TASK_EXECUTION_ADDENDUM,
                session_context=SessionContext(
                    source="task",
                    channel="task",
                    session_id=str(reflection_session_id),
                    metadata={
                        "task_id": str(task_id),
                        "task_type": task_type,
                    },
                ),
                on_tool_call=_on_tool_call,
                core_tools_only=True,
                max_tool_rounds=getattr(agent, "max_tool_rounds", None),
            )
        )
        reply = result.content
        logger.info(f"[TaskExec] Runtime reply: {reply[:80]}")
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[TaskExec] Error: {error_msg}")
        await _log_error(task_id, f"执行出错: {error_msg[:150]}")
        if task_type == 'supervision':
            await _restore_supervision_status(task_id)
        return

    # Step 5: Save result and update status
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    conversation_id=session_conversation_id(reflection_session),
                    role="assistant",
                    content=reply,
                    user_id=creator_id,
                    participant_id=agent_participant_id,
                )
            )
            if task_type == 'supervision':
                # Supervision tasks stay active; just log the result
                task.status = "pending"
                db.add(TaskLog(task_id=task_id, content=f"✅ 督办执行完成\n\n{reply}"))
            else:
                task.status = "done"
                task.completed_at = datetime.now(timezone.utc)
                db.add(TaskLog(task_id=task_id, content=f"✅ 任务完成\n\n{reply}"))
            await db.commit()
            logger.info(f"[TaskExec] Task {task_id} {'logged' if task_type == 'supervision' else 'completed'}!")

    # Log activity
    from app.services.activity_logger import log_activity
    await log_activity(
        agent_id, "task_updated",
        f"{'督办' if task_type == 'supervision' else '任务'}执行: {task_title[:60]}",
        detail={"task_id": str(task_id), "task_type": task_type, "title": task_title, "reply": reply[:500]},
        related_id=task_id,
    )


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    logger.error(f"[TaskExec] Error for {task_id}: {message}")
    async with async_session() as db:
        db.add(TaskLog(task_id=task_id, content=f"❌ {message}"))
        await db.commit()


async def _restore_supervision_status(task_id: uuid.UUID) -> None:
    """Restore supervision task status back to pending after a failed execution."""
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task and task.status == "doing":
            task.status = "pending"
            await db.commit()
