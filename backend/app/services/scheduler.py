"""Lightweight asyncio scheduler for agent cron jobs.

Runs as a background task inside the FastAPI process.
Every 30 seconds, checks for schedules whose next_run_at <= now
and executes them by calling the LLM with the schedule's instruction.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.kernel.contracts import ExecutionIdentityRef
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Compute the next run time from a cron expression."""
    try:
        from croniter import croniter

        base = after or datetime.now(timezone.utc)
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime).replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.error(f"Invalid cron expression '{cron_expr}': {e}")
        return None


async def _execute_schedule(schedule_id: uuid.UUID, agent_id: uuid.UUID, instruction: str):
    """Execute a single schedule by calling the LLM with the instruction."""
    try:
        from app.models.agent import Agent
        from app.models.llm import LLMModel

        async with async_session() as db:
            # Load agent + model
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"Schedule {schedule_id}: agent {agent_id} not found")
                return

            if agent.status != "running":
                logger.info(f"Schedule {schedule_id}: agent {agent.name} not running, skipping")
                return

            from app.core.permissions import is_agent_expired
            if is_agent_expired(agent):
                logger.info(f"Schedule {schedule_id}: agent {agent.name} has expired, skipping")
                return

            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                logger.warning(f"Schedule {schedule_id}: agent {agent.name} has no LLM model")
                return

            model_result = await db.execute(
                select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = model_result.scalar_one_or_none()
            if not model:
                logger.warning(f"Schedule {schedule_id}: LLM model {model_id} not found")
                return

            try:
                runtime_messages = [{"role": "user", "content": f"[自动调度任务] {instruction}"}]
                result = await invoke_agent(
                    AgentInvocationRequest(
                        model=model,
                        messages=runtime_messages,
                        memory_messages=runtime_messages,
                        agent_name=agent.name,
                        role_description=agent.role_description or "",
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        execution_identity=ExecutionIdentityRef(
                            identity_type="agent_bot",
                            identity_id=agent_id,
                            label=f"Agent: {agent.name} (schedule)",
                        ),
                        session_context=SessionContext(
                            source="schedule",
                            channel="schedule",
                            metadata={"schedule_id": str(schedule_id)},
                        ),
                        core_tools_only=True,
                        max_tool_rounds=getattr(agent, "max_tool_rounds", None),
                    )
                )
                reply = result.content
            except Exception as e:
                logger.error(f"Schedule {schedule_id}: unified runtime error: {e}")
                reply = f"(Runtime 调用异常: {str(e)[:200]})"

            # Log activity
            from app.services.activity_logger import log_activity
            await log_activity(
                agent_id, "schedule_run",
                f"定时任务执行: {instruction[:60]}",
                detail={"schedule_id": str(schedule_id), "instruction": instruction, "reply": reply[:500]},
            )

            logger.info(f"Schedule {schedule_id} executed for agent {agent.name}: {reply[:80]}")

    except Exception as e:
        logger.error(f"Schedule {schedule_id} execution error: {e}", exc_info=True)


async def _tick():
    """One scheduler tick: find and execute due schedules."""
    from app.database import async_session
    from app.models.schedule import AgentSchedule
    from app.services.audit_logger import write_audit_log

    now = datetime.now(timezone.utc)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentSchedule).where(
                    AgentSchedule.is_enabled,
                    AgentSchedule.next_run_at <= now,
                )
            )
            due_schedules = result.scalars().all()

            if due_schedules:
                await write_audit_log("schedule_tick", {"due_count": len(due_schedules)})

            for sched in due_schedules:
                # Update run tracking immediately
                next_run = compute_next_run(sched.cron_expr, now)
                sched.last_run_at = now
                sched.next_run_at = next_run
                sched.run_count = (sched.run_count or 0) + 1
                await db.commit()

                await write_audit_log(
                    "schedule_fire",
                    {"schedule_id": str(sched.id), "name": sched.name, "instruction": sched.instruction[:100], "next_run": str(next_run)},
                    agent_id=sched.agent_id,
                )

                # Fire execution in background (don't block ticker)
                asyncio.create_task(
                    _execute_schedule(sched.id, sched.agent_id, sched.instruction)
                )
                logger.info(f"Triggered schedule '{sched.name}' (next: {next_run})")

    except Exception as e:
        logger.error(f"Scheduler tick error: {e}", exc_info=True)
        await write_audit_log("schedule_error", {"error": str(e)[:300]})


async def start_scheduler():
    """Start the background scheduler loop. Call from FastAPI startup."""
    logger.info("🕐 Agent scheduler started (30s interval)")
    while True:
        await _tick()
        await asyncio.sleep(30)
