"""Background task executor — runs agent tasks through the unified runtime."""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.kernel.contracts import ExecutionIdentityRef
from app.database import tenant_scoped_session
from app.memory.t0.ledger import seal_t0_session_segment
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.task import Task, TaskLog
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason
from app.services.chat_transcript import AppendSessionEventResult, append_session_event
from app.services.tenant_resolver import resolve_tenant_for_agent


TASK_EXECUTION_ADDENDUM = """<role>
You are executing an assigned task autonomously. Drive it to completion
without waiting for follow-up input. Your job is to produce a verifiable
outcome — not a plan, not a status update, not a discussion.
</role>

<execution_model>
- Decompose the task into concrete steps and execute each in sequence.
- Start with kernel tools (file I/O, search, skills). Escalate via `load_skill`
  or `tool_search` only when kernel tools cannot reach the capability needed.
- If the task involves contacting someone or touching external systems
  (Feishu, email, plaza, APIs), load the matching skill FIRST so you have
  the right toolset before you commit to an approach.
- Report progress ONLY when it materially changes execution state (e.g.,
  newly discovered blocker, capability gap, or final completion). Silent
  progress on predictable steps is fine.
</execution_model>

<failure_handling>
When a tool call fails:
1. Read the actual error message. Do not guess.
2. Diagnose the root cause (wrong argument? missing permission? stale data?).
3. Try a DIFFERENT approach. Don't retry the same command hoping it works.

**3-strike rule**:
- 3 attempts with DIFFERENT errors → a real capability/permission gap.
  Stop, report the specific gap as a Blocker, and end the task.
- 3 attempts with the SAME error → you are not actually diagnosing.
  Stop and report the error literally — the parent needs to re-scope.

**Hard blockers** (missing permissions, data, or dependencies): state the
blocker explicitly and stop. Don't fabricate workarounds that bypass the
original intent.
</failure_handling>

<completion_criteria>
Before marking complete, every item below must be true:
1. ✅ Every sub-step was EXECUTED (not just planned or described).
2. ✅ Each claimed outcome has CONCRETE evidence (file path, stdout, URL,
   tool response — something the user can verify).
3. ✅ Errors encountered are named and either resolved or listed as Blockers.
4. ✅ The final report uses the format below. No free-form prose in place of it.
</completion_criteria>

<final_report_format>
```
Outcome:
- <what was completed — one bullet per discrete result>
- <include "partial" or "blocked" items only here if they made meaningful progress>

Evidence:
- <file path / line range for each file touched>
- <command + exit code / key stdout for each run>
- <tool response id or URL for each external action>

Blockers:
- <specific unresolved items, or "none">

Next Steps:
- <only include if follow-up work is still required; omit the section entirely otherwise>
```
</final_report_format>

<good_report_examples>
**Example A — implementation task (full completion)**
```
Outcome:
- Fixed token-expiry race in middleware by reordering refresh check before
  response header write
- Added regression test covering the expired-token path
- All auth integration tests still pass

Evidence:
- backend/app/auth/middleware.py:138-148 (3 lines changed)
- backend/tests/auth/test_middleware.py::test_expired_token_refreshes — new test
- `pytest tests/auth -q` → 24 passed, 0 failed (exit 0)

Blockers:
- none
```

**Example B — external-system task**
```
Outcome:
- Sent the weekly sector brief to the investment-managers Feishu group
- Confirmed delivery via message ID response

Evidence:
- feishu_send_message response: message_id=om_xxx123, chat_id=oc_yyy456
- Brief source: workspace/briefs/2026-04-16-sector-brief.md (2,340 chars)

Blockers:
- none
```

**Example C — task that hit a real blocker**
```
Outcome:
- Read the deploy-config files and mapped the current build stages
- Attempted to run the integration suite but cannot proceed (see Blockers)

Evidence:
- Notes: workspace/deploy_audit.md
- pytest command attempted: `pytest tests/integration -q`
- stdout: "sqlalchemy.exc.OperationalError: could not connect to server"

Blockers:
- No Postgres credentials in this workspace — integration tests cannot run.
  Parent must either provide DATABASE_URL or delegate the test run to a
  worker with DB access.
```
</good_report_examples>

<bad_report_examples>
DO NOT return any of these:

❌ **Plan instead of execution**
```
Outcome:
- Will analyze the auth module
- Plans to run the tests
```
(The task is to DO, not plan. If you haven't executed, don't claim Outcome.)

❌ **Evidence-free claims**
```
Outcome:
- Fixed the bug
Evidence:
- See above
```
(No file, no line, no test output. The parent cannot verify this.)

❌ **Retry loop without diagnosis**
(Calling the same failing tool 5 times with identical arguments, then
claiming "tool is broken". After 3 different-error attempts or 1 repeat
of the same error, diagnose or stop — don't grind.)

❌ **Fabricated verification**
```
Outcome:
- Deployed to production
Evidence:
- Deploy succeeded (I didn't actually run the command)
```
(If you didn't run it, don't claim it. Put "awaiting deploy" in Blockers.)

❌ **Burying blockers in prose**
```
Outcome:
- Mostly done — some parts didn't work and I think there might be an issue
  with permissions but I wasn't sure so I just skipped those parts.
```
(Specific blockers go in the Blockers section with specific details, not
hand-waved inside Outcome.)
</bad_report_examples>

<hard_rules>
1. The Final Report format is required. Parent tooling parses it. Surrounding
   prose or missing sections breaks downstream consumers.
2. Never fabricate evidence. "Ran tests" means you ran the command and can
   quote stdout. If you can't, it's a Blocker.
3. "Complete" requires ALL of completion_criteria. Partial ≠ complete.
4. Do not wait for follow-up input. If you need input, state the specific
   missing piece as a Blocker and stop.
</hard_rules>
"""


def _build_task_user_prompt(
    task_title: str,
    task_description: str,
) -> str:
    user_prompt = f"[任务执行] {task_title}"
    if task_description:
        user_prompt += f"\n任务描述: {task_description}"
    return user_prompt + "\n\n请认真完成此任务，给出详细的执行结果。"


async def _append_task_session_event(
    *,
    db,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    task_id: uuid.UUID,
    task_type: str,
    event_type: str,
    role: str,
    actor_type: str,
    content: str,
    actor_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    participant_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    t0_role: str | None = None,
) -> AppendSessionEventResult:
    return await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        actor_type=actor_type,
        event_type=event_type,
        role=role,
        t0_role=t0_role,
        user_id=actor_id,
        participant_id=participant_id,
        message_id=message_id,
        content=content,
        visibility_scope="agent_owner",
        listed_surface="task_updates",
        source="task",
        metadata={"task_id": str(task_id), "task_type": task_type, **(metadata or {})},
    )


def _seal_task_t0_segment(
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    task_id: uuid.UUID,
    task_type: str,
    tenant_id: uuid.UUID | None = None,
) -> None:
    try:
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="task_complete",
            metadata={
                "task_id": str(task_id),
                "task_type": task_type,
                "tenant_id": str(tenant_id) if tenant_id else None,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[TaskExec] T0 seal skipped for task {}: {}", task_id, exc)


async def _task_plan_gate_allows(db, *, task: Task, agent_id: uuid.UUID) -> tuple[bool, str | None]:
    """Final Plan Mode backstop for background task execution.

    REST/tool entrypoints gate before creating/running todo tasks, but
    ``execute_task`` is the actual execution choke point. It must also verify the
    persisted task provenance so internal callers or restart paths cannot bypass
    Plan Mode by calling the executor directly.
    """
    if str(getattr(task, "type", "todo") or "todo").strip() != "todo":
        return True, None

    from app.services.plan_mode_gate import get_plan_mode_gate

    gate = get_plan_mode_gate()
    decision = await gate.check(
        db,
        agent_id=agent_id,
        action_kind="start_long_task",
        confirmed_plan_id=getattr(task, "plan_id", None),
        plan_version=getattr(task, "plan_version", None),
        plan_hash=getattr(task, "plan_hash", None),
        action_artifact={"metadata": {"plan_exempt_reason": getattr(task, "plan_exempt_reason", None)}},
    )
    return decision.allowed, decision.reason


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Flow: pending → doing → done.
    """
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    # Detached background task (asyncio.create_task from api/tasks.py): the HTTP
    # request's tenant ContextVar is gone by the time this runs, yet step 2 reads
    # agents/llm_models (RLS-policied) and the whole flow must pin one tenant. Resolve
    # the owning tenant once via an audited bypass read, then pin every session below.
    tenant_id = await resolve_tenant_for_agent(agent_id)

    # Step 1: Mark as doing
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            logger.warning(f"[TaskExec] Task {task_id} not found")
            return

        plan_allowed, plan_reason = await _task_plan_gate_allows(db, task=task, agent_id=agent_id)
        if not plan_allowed:
            db.add(
                TaskLog(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    content=(
                        "Plan Mode blocked autonomous task execution: "
                        f"{plan_reason or 'plan_required'}. Confirm a plan before running this task."
                    ),
                )
            )
            await db.commit()
            logger.info("[TaskExec] Plan Mode blocked task {} for agent {}: {}", task_id, agent_id, plan_reason)
            return

        task.status = "doing"
        db.add(TaskLog(tenant_id=tenant_id, task_id=task_id, content="🤖 开始执行任务..."))
        await db.commit()
        task_title = task.title
        task_description = task.description or ""
        task_type = task.type  # 'todo'

    user_prompt = _build_task_user_prompt(
        task_title,
        task_description,
    )
    runtime_messages = [{"role": "user", "content": user_prompt}]

    # Step 2: Load agent + model
    async with tenant_scoped_session(tenant_id) as db:
        agent_result = await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            await _log_error(task_id, "数字员工未找到", tenant_id=tenant_id)
            return
        lifecycle_reason = get_agent_lifecycle_block_reason(agent)
        if lifecycle_reason:
            await _log_error(
                task_id,
                f"数字员工当前不可执行：{lifecycle_reason}",
                tenant_id=tenant_id,
            )
            return

        model_id = agent.primary_model_id or agent.fallback_model_id
        if not model_id:
            await _log_error(task_id, f"{agent.name} 未配置 LLM 模型，无法执行任务", tenant_id=tenant_id)
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
            await _log_error(task_id, "配置的模型不存在", tenant_id=tenant_id)
            return

        agent_name = agent.name
        creator_id = agent.creator_id
        participant_result = await db.execute(
            select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
        )
        agent_participant = participant_result.scalar_one_or_none()
        agent_participant_id = agent_participant.id if agent_participant else None

        reflection_session = ChatSession(
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=creator_id,
            participant_id=agent_participant_id,
            source_channel="task",
            session_kind="user_task",
            actor_type="agent",
            runtime_source="runtime_task",
            visibility_scope="agent_owner",
            listed_surface="task_updates",
            title=f"🧾 Task: {task_title}"[:200],
        )
        db.add(reflection_session)
        await db.flush()
        reflection_session_id = reflection_session.id

        await _append_task_session_event(
            db=db,
            agent_id=agent_id,
            session_id=reflection_session_id,
            task_id=task_id,
            task_type=task_type,
            event_type="user_message",
            actor_type="user",
            role="user",
            content=user_prompt,
            actor_id=creator_id,
            tenant_id=tenant_id,
            participant_id=agent_participant_id,
            metadata={"title": task_title},
        )
        await db.commit()

    # Step 4: Call unified runtime
    try:
        logger.info(f"[TaskExec] Invoking unified runtime for task: {task_title}")

        async def _on_tool_call(data: dict) -> None:
            if data.get("status") != "done":
                return
            tool_payload = json.dumps(
                {
                    "name": data.get("name"),
                    "args": data.get("args"),
                    "status": "done",
                    "result": str(data.get("result", ""))[:2000],
                    "reasoning_content": data.get("reasoning_content"),
                    "reasoning_signature": data.get("reasoning_signature"),
                },
                ensure_ascii=False,
                default=str,
            )
            async with tenant_scoped_session(tenant_id) as tc_db:
                await _append_task_session_event(
                    db=tc_db,
                    agent_id=agent_id,
                    session_id=reflection_session_id,
                    task_id=task_id,
                    task_type=task_type,
                    event_type="tool_result",
                    actor_type="agent",
                    role="tool_call",
                    t0_role="tool",
                    content=tool_payload,
                    actor_id=agent_id,
                    tenant_id=tenant_id,
                    participant_id=agent_participant_id,
                    metadata={"tool_name": data.get("name"), "status": "done"},
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
        await _log_error(task_id, f"执行出错: {error_msg[:150]}", tenant_id=tenant_id)
        return

    # Step 5: Save result and update status
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            await _append_task_session_event(
                db=db,
                agent_id=agent_id,
                session_id=reflection_session_id,
                task_id=task_id,
                task_type=task_type,
                event_type="assistant_message",
                actor_type="assistant",
                role="assistant",
                content=reply,
                actor_id=agent_id,
                tenant_id=tenant_id,
                participant_id=agent_participant_id,
                metadata={"title": task_title},
            )
            task.status = "done"
            task.completed_at = datetime.now(timezone.utc)
            db.add(TaskLog(tenant_id=tenant_id, task_id=task_id, content=f"✅ 任务完成\n\n{reply}"))
            await db.commit()
            _seal_task_t0_segment(
                agent_id=agent_id,
                session_id=reflection_session_id,
                task_id=task_id,
                task_type=task_type,
                tenant_id=tenant_id,
            )
            logger.info(f"[TaskExec] Task {task_id} completed!")

    # Log activity
    from app.services.activity_logger import log_activity

    await log_activity(
        agent_id,
        "task_updated",
        f"任务执行: {task_title[:60]}",
        detail={"task_id": str(task_id), "task_type": task_type, "title": task_title, "reply": reply[:500]},
        related_id=task_id,
    )


async def _log_error(task_id: uuid.UUID, message: str, *, tenant_id: uuid.UUID | None = None) -> None:
    """Add an error log to the task."""
    logger.error(f"[TaskExec] Error for {task_id}: {message}")
    async with tenant_scoped_session(tenant_id) as db:
        db.add(TaskLog(tenant_id=tenant_id, task_id=task_id, content=f"❌ {message}"))
        await db.commit()
