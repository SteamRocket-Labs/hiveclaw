"""Durable run records for background subagents (Step 8)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SUBAGENT_TYPE_EXPLORER,
    SubagentResult,
    SubagentSpec,
    SubagentSpawnContext,
    spawn_subagent,
)
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_service import (
    build_completion_journal_entry,
    create_runtime_task_record,
    get_runtime_task_record,
    list_active_runtime_task_records,
    update_runtime_task_record,
)
from app.services.tenant_resolver import resolve_tenant_for_agent

SUBAGENT_RUN_TASK_TYPE = "subagent"
SUBAGENT_RESTART_REPLAY_SAFE_TYPES = frozenset({SUBAGENT_TYPE_EXPLORER, SUBAGENT_TYPE_CRITIC})


def _subagent_type_restart_replay_safe(spec_type: str | None) -> bool:
    return str(spec_type or "").strip() in SUBAGENT_RESTART_REPLAY_SAFE_TYPES


async def start_subagent_run(
    *,
    parent_agent_id: uuid.UUID,
    spec_name: str,
    spec_type: str,
    task: str,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    """Create the durable ``running`` record for a background subagent. Returns
    the run id the parent later passes to ``check_subagent``."""
    run_id = str(uuid.uuid4())
    replay_safe = _subagent_type_restart_replay_safe(spec_type)
    metadata: dict[str, Any] = {
        "subagent_type": spec_type,
        "subagent_name": spec_name,
        "resumable_subagent": replay_safe,
        "resume_after_restart": replay_safe,
    }
    if not replay_safe:
        metadata["restart_resume_blocker"] = "non_idempotent_subagent_type"
    return await create_runtime_task_record(
        task_id=run_id,
        task_type=SUBAGENT_RUN_TASK_TYPE,
        status="running",
        parent_agent_id=parent_agent_id,
        child_agent_name=spec_name,
        prompt=task,
        trace_id=trace_id,
        parent_session_id=parent_session_id,
        metadata_json=metadata,
    )


def make_run_completer(run_id: str):
    """Return an ``on_complete(result)`` callback that writes the terminal status."""

    async def _complete(result: SubagentResult) -> None:
        status = "completed" if result.ok else "failed"
        summary = (result.content or result.error or "")[:8000]
        await update_runtime_task_record(
            run_id,
            status=status,
            result_summary=summary,
            token_usage={"total_tokens": result.tokens_used},
            metadata_json={
                "completion_journal": [
                    build_completion_journal_entry(
                        task_type=SUBAGENT_RUN_TASK_TYPE,
                        task_id=run_id,
                        status=status,
                        side_effect_risk="read_only" if result.type in SUBAGENT_RESTART_REPLAY_SAFE_TYPES else "mutating",
                        summary=summary,
                    )
                ]
            },
        )

    return _complete


async def _resolve_parent_runtime(parent_agent_id: uuid.UUID) -> SubagentSpawnContext | None:
    from app.services.model_resolution import choose_runtime_model_pair

    async with async_session() as db:
        async with enter_rls_bypass(db, reason="background subagent restart runtime bootstrap"):
            agent = (
                await db.execute(select(Agent).where(Agent.id == parent_agent_id))
            ).scalar_one_or_none()
            if agent is None:
                return None
            primary_model = None
            fallback_model = None
            if getattr(agent, "primary_model_id", None):
                primary_model = (
                    await db.execute(
                        select(LLMModel).where(
                            LLMModel.id == agent.primary_model_id,
                            LLMModel.tenant_id == agent.tenant_id,
                            LLMModel.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
            if getattr(agent, "fallback_model_id", None):
                fallback_model = (
                    await db.execute(
                        select(LLMModel).where(
                            LLMModel.id == agent.fallback_model_id,
                            LLMModel.tenant_id == agent.tenant_id,
                            LLMModel.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
            model, fallback_model = choose_runtime_model_pair(primary_model, fallback_model, None)
            if model is None:
                return None
            return SubagentSpawnContext(
                parent_agent_id=parent_agent_id,
                parent_user_id=agent.creator_id,
                model=model,
                fallback_model=fallback_model,
                parent_agent_name=getattr(agent, "name", None) or "Agent",
                role_description=getattr(agent, "role_description", None) or "",
                tenant_id=agent.tenant_id,
            )


def _coerce_runtime_context(runtime: Any) -> SubagentSpawnContext | None:
    if isinstance(runtime, SubagentSpawnContext):
        return runtime
    if isinstance(runtime, dict) and isinstance(runtime.get("ctx_kwargs"), dict):
        return SubagentSpawnContext(**runtime["ctx_kwargs"])
    return None


async def resume_persisted_subagent_runs(*, limit: int = 50) -> list[str]:
    """Resume replay-safe background subagents after a process restart."""

    resumed: list[str] = []
    records = await list_active_runtime_task_records(limit=limit, statuses=("pending", "running"))
    for record in records:
        if record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
            continue
        run_id = str(record.get("task_id") or "")
        metadata = record.get("metadata") or {}
        spec_type = str(metadata.get("subagent_type") or "")
        if not metadata.get("resume_after_restart") or not metadata.get("resumable_subagent"):
            continue
        if not _subagent_type_restart_replay_safe(spec_type):
            await update_runtime_task_record(
                run_id,
                status="failed",
                result_summary=(
                    "Subagent was not resumed after restart because its type is not safe to replay without "
                    "duplicating side effects."
                ),
                metadata_json={
                    "resume_failed": True,
                    "restart_resume_blocker": "non_idempotent_subagent_type",
                },
            )
            continue
        parent_agent_id = uuid.UUID(str(record.get("parent_agent_id") or ""))
        runtime = _coerce_runtime_context(await _resolve_parent_runtime(parent_agent_id))
        if runtime is None:
            await update_runtime_task_record(
                run_id,
                status="failed",
                result_summary="Subagent could not be resumed after restart because parent runtime is unavailable.",
                metadata_json={"resume_failed": True},
            )
            continue
        runtime.trace_id = str(record.get("trace_id") or runtime.trace_id or "")
        runtime.parent_session_id = str(record.get("parent_session_id") or runtime.parent_session_id or "")
        spec = SubagentSpec(
            name=str(metadata.get("subagent_name") or record.get("child_agent_name") or spec_type),
            type=spec_type,
            max_tool_rounds=metadata.get("max_tool_rounds"),
        )
        await update_runtime_task_record(
            run_id,
            status="running",
            metadata_json={
                "resumed_after_restart": True,
            },
        )
        await spawn_subagent(
            runtime,
            spec,
            str(record.get("prompt") or ""),
            run_in_background=True,
            on_complete=make_run_completer(run_id),
        )
        resumed.append(run_id)
    return resumed


async def get_subagent_run(run_id: str, parent_agent_id: uuid.UUID) -> dict[str, Any] | None:
    """Read one background subagent run by id (terminal status + result).

    Ownership-scoped: returns None unless the run belongs to ``parent_agent_id``
    (``get_runtime_task_record`` is an unscoped by-PK read, so the caller must not
    be able to read another agent's run by guessing its id)."""
    record = await get_runtime_task_record(run_id)
    if record is None or record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
        return None
    if record.get("parent_agent_id") != str(parent_agent_id):
        return None
    return record


async def list_subagent_runs(parent_agent_id: uuid.UUID, *, limit: int = 20) -> list[dict[str, Any]]:
    """Recent background subagent runs spawned by this agent (newest first)."""
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.parent_agent_id == parent_agent_id,
                        RuntimeTask.task_type == SUBAGENT_RUN_TASK_TYPE,
                    )
                    .order_by(RuntimeTask.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "run_id": str(r.id),
            "name": r.child_agent_name,
            "type": (r.metadata_json or {}).get("subagent_type"),
            "status": r.status,
            "result_summary": r.result_summary,
            "orphaned_by_restart": bool((r.metadata_json or {}).get("orphaned_by_restart")),
        }
        for r in rows
    ]
