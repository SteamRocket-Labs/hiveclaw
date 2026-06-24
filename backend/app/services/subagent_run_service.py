"""Durable run records for background subagents (Step 8)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.services.chat_transcript import append_session_event
from app.services.runtime_task_service import (
    build_completion_journal_entry,
    build_restart_reconciliation_metadata,
    build_restart_replay_contract,
    build_restart_replay_journal_entry,
    create_runtime_task_record,
    get_runtime_task_record,
    list_active_runtime_task_records,
    merge_restart_replay_journal,
    update_runtime_task_record,
)
from app.services.tenant_resolver import resolve_tenant_for_agent

SUBAGENT_RUN_TASK_TYPE = "subagent"
SUBAGENT_RESTART_REPLAY_SAFE_TYPES = frozenset({SUBAGENT_TYPE_EXPLORER, SUBAGENT_TYPE_CRITIC})


@dataclass(frozen=True, slots=True)
class SubagentRunStart:
    run_id: str
    child_session_id: str | None


def _subagent_type_restart_replay_safe(spec_type: str | None) -> bool:
    return str(spec_type or "").strip() in SUBAGENT_RESTART_REPLAY_SAFE_TYPES


def _uuid_or_none(value: uuid.UUID | str | None) -> uuid.UUID | None:
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


def _build_subagent_session_contract(
    *,
    run_id: str,
    child_session_id: str | None,
) -> dict[str, Any]:
    return {
        "kind": "subagent_child_session",
        "participant_type": "subagent",
        "continuation_address": child_session_id,
        "continuation_tool": "send_agent_session_message",
        "run_id": run_id,
        "timeout_is_terminal_failure": False,
        "inspection_mode": "fallback_only",
    }


async def create_subagent_child_session(
    *,
    parent_agent_id: uuid.UUID,
    parent_user_id: uuid.UUID,
    spec_name: str,
    spec_type: str,
    task: str,
    run_id: str,
    parent_session_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    context_mode: str = "none",
) -> str:
    """Create the lightweight child-session projection for a background subagent."""

    resolved_tenant_id = tenant_id if tenant_id is not None else await resolve_tenant_for_agent(parent_agent_id)
    child_session_id = uuid.uuid4()
    parent_session_uuid = _uuid_or_none(parent_session_id)
    metadata = {
        "session_contract": _build_subagent_session_contract(
            run_id=run_id,
            child_session_id=str(child_session_id),
        ),
        "session_state": "running",
        "subagent_name": spec_name,
        "subagent_type": spec_type,
        "run_id": run_id,
        "parent_session_id": parent_session_id,
        "context_mode": context_mode,
        "trace_id": trace_id,
        "lightweight_identity": True,
        "has_digital_employee_identity": False,
    }
    async with tenant_scoped_session(resolved_tenant_id) as db:
        session = ChatSession(
            id=child_session_id,
            agent_id=parent_agent_id,
            tenant_id=resolved_tenant_id,
            user_id=parent_user_id,
            title=f"Subagent / {spec_name}",
            source_channel="subagent",
            session_kind="subagent",
            actor_type="subagent",
            runtime_source="subagent",
            visibility_scope="team",
            listed_surface="parent",
            parent_session_id=parent_session_uuid,
            root_session_id=parent_session_uuid,
            # RuntimeTask is created immediately after this projection. Keep the
            # FK empty here to avoid referencing a row that has not been inserted
            # yet; the run id remains in metadata and the RuntimeTask points back
            # to this child session via child_session_id.
            runtime_task_id=None,
            transcript_metadata_json=metadata,
        )
        db.add(session)
        await append_session_event(
            db=db,
            agent_id=parent_agent_id,
            tenant_id=resolved_tenant_id,
            session_id=child_session_id,
            actor_type="agent",
            event_type="subagent_task_started",
            content=task,
            role="user",
            user_id=parent_user_id,
            run_id=None,
            runtime_task_id=None,
            root_session_id=parent_session_uuid,
            parent_session_id=parent_session_uuid,
            metadata=metadata,
            visibility_scope="team",
            listed_surface="parent",
            source="subagent",
        )
        await db.commit()
    return str(child_session_id)


async def start_subagent_run(
    *,
    parent_agent_id: uuid.UUID,
    parent_user_id: uuid.UUID | None = None,
    spec_name: str,
    spec_type: str,
    task: str,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    context_mode: str = "none",
) -> SubagentRunStart:
    """Create the durable ``running`` record for a background subagent. Returns
    the run id and child session id the parent can use for continuation."""
    run_id = str(uuid.uuid4())
    child_session_id = None
    if parent_user_id is not None:
        child_session_id = await create_subagent_child_session(
            parent_agent_id=parent_agent_id,
            parent_user_id=parent_user_id,
            spec_name=spec_name,
            spec_type=spec_type,
            task=task,
            run_id=run_id,
            parent_session_id=parent_session_id,
            trace_id=trace_id,
            context_mode=context_mode,
        )
    replay_safe = _subagent_type_restart_replay_safe(spec_type)
    side_effect_risk = "read_only" if replay_safe else "mutating"
    metadata: dict[str, Any] = {
        "subagent_type": spec_type,
        "subagent_name": spec_name,
        "child_session_id": child_session_id,
        "context_mode": context_mode,
        "session_contract": _build_subagent_session_contract(
            run_id=run_id,
            child_session_id=child_session_id,
        ),
        "resumable_subagent": True,
        "resume_after_restart": True,
        "side_effect_risk": side_effect_risk,
        "restart_replay_contract": build_restart_replay_contract(
            task_type=SUBAGENT_RUN_TASK_TYPE,
            task_id=run_id,
            side_effect_risk=side_effect_risk,
            trace_id=trace_id,
            session_id=parent_session_id,
        ),
    }
    metadata = merge_restart_replay_journal(
        metadata,
        build_restart_replay_journal_entry(
            task_type=SUBAGENT_RUN_TASK_TYPE,
            task_id=run_id,
            side_effect_risk=side_effect_risk,
            phase="spawn_intent_recorded",
            trace_id=trace_id,
            session_id=parent_session_id,
        ),
    )
    persisted_run_id = await create_runtime_task_record(
        task_id=run_id,
        task_type=SUBAGENT_RUN_TASK_TYPE,
        status="running",
        parent_agent_id=parent_agent_id,
        child_agent_name=spec_name,
        prompt=task,
        trace_id=trace_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        metadata_json=metadata,
    )
    return SubagentRunStart(run_id=persisted_run_id, child_session_id=child_session_id)


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
                        side_effect_risk="read_only"
                        if result.type in SUBAGENT_RESTART_REPLAY_SAFE_TYPES
                        else "mutating",
                        summary=summary,
                    )
                ]
            },
        )
        await update_subagent_child_session_state_for_run(
            run_id=run_id,
            status=status,
            summary=summary,
        )

    return _complete


async def update_subagent_child_session_state_for_run(
    *,
    run_id: str,
    status: str,
    summary: str,
) -> None:
    record = await get_runtime_task_record(run_id)
    if record is None:
        return
    child_session_id = str(
        record.get("child_session_id") or (record.get("metadata") or {}).get("child_session_id") or ""
    ).strip()
    child_session_uuid = _uuid_or_none(child_session_id)
    parent_agent_uuid = _uuid_or_none(record.get("parent_agent_id"))
    if child_session_uuid is None or parent_agent_uuid is None:
        return

    tenant_id = await resolve_tenant_for_agent(parent_agent_uuid)
    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == child_session_uuid, ChatSession.agent_id == parent_agent_uuid)
        )
        session = result.scalar_one_or_none()
        if session is None:
            return
        metadata = dict(session.transcript_metadata_json or {})
        metadata["session_state"] = status
        metadata["last_run_id"] = run_id
        metadata["last_result_summary"] = summary
        session.transcript_metadata_json = metadata
        await append_session_event(
            db=db,
            agent_id=parent_agent_uuid,
            tenant_id=tenant_id,
            session_id=child_session_uuid,
            actor_type="agent",
            event_type="subagent_task_completed" if status == "completed" else "subagent_task_failed",
            content=summary,
            role="assistant",
            user_id=session.user_id,
            run_id=run_id,
            runtime_task_id=run_id,
            root_session_id=session.root_session_id,
            parent_session_id=session.parent_session_id,
            metadata={
                **metadata,
                "status": status,
            },
            visibility_scope=session.visibility_scope,
            listed_surface=session.listed_surface,
            source="subagent",
        )
        await db.commit()


async def _resolve_parent_runtime(parent_agent_id: uuid.UUID) -> SubagentSpawnContext | None:
    from app.services.model_resolution import choose_runtime_model_pair

    async with async_session() as db:
        async with enter_rls_bypass(db, reason="background subagent restart runtime bootstrap"):
            agent = (await db.execute(select(Agent).where(Agent.id == parent_agent_id))).scalar_one_or_none()
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
        replay_safe = _subagent_type_restart_replay_safe(spec_type)
        if not replay_safe:
            await update_runtime_task_record(
                run_id,
                status="needs_reconciliation",
                result_summary=(
                    "Subagent was not resumed after restart because its type is not safe to replay without "
                    "duplicating side effects. Reconciliation is required before retry."
                ),
                metadata_json=build_restart_reconciliation_metadata(
                    metadata,
                    task_type=SUBAGENT_RUN_TASK_TYPE,
                    task_id=run_id,
                    blocker="non_idempotent_subagent_type",
                    summary=(
                        "Subagent was not resumed after restart because its type is not safe to replay without "
                        "duplicating side effects. Reconciliation is required before retry."
                    ),
                    trace_id=str(record.get("trace_id") or ""),
                    session_id=str(record.get("child_session_id") or record.get("parent_session_id") or ""),
                ),
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
        resume_metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type=SUBAGENT_RUN_TASK_TYPE,
                task_id=run_id,
                side_effect_risk=str(metadata.get("side_effect_risk") or "read_only"),
                phase="resume_intent_recorded",
                trace_id=str(record.get("trace_id") or ""),
                session_id=str(record.get("child_session_id") or record.get("parent_session_id") or ""),
            ),
        )
        await update_runtime_task_record(
            run_id,
            status="running",
            metadata_json={
                "resumed_after_restart": True,
                "restart_replay_contract": metadata.get("restart_replay_contract"),
                "restart_replay_journal": resume_metadata.get("restart_replay_journal"),
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
            "child_session_id": r.child_session_id or (r.metadata_json or {}).get("child_session_id"),
            "session_state": {
                "status": r.status,
                "active_run_id": str(r.id) if r.status in {"pending", "running", "in_progress"} else None,
                "child_session_id": r.child_session_id or (r.metadata_json or {}).get("child_session_id"),
                "parent_session_id": r.parent_session_id,
            },
            "transcript_refs": {
                "session_id": r.child_session_id or (r.metadata_json or {}).get("child_session_id"),
                "parent_session_id": r.parent_session_id,
                "trace_id": r.trace_id,
            },
            "orphaned_by_restart": bool((r.metadata_json or {}).get("orphaned_by_restart")),
        }
        for r in rows
    ]
