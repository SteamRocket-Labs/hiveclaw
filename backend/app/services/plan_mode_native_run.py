"""Native Session V2 admission and recovery for system Plan Mode authoring."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput

AUTHORING_KEY = "system_plan_authoring"
AUTHORING_SCHEMA = "hive.system_plan_authoring.v1"


def creation_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    """Caller-supplied metadata cannot mint a native authoring binding."""
    metadata = dict(value or {})
    metadata.pop(AUTHORING_KEY, None)
    return metadata


def authoring_binding(plan: Any) -> dict[str, Any] | None:
    value = (getattr(plan, "metadata_json", None) or {}).get(AUTHORING_KEY)
    return dict(value) if isinstance(value, dict) else None


async def reconcile_authoring(db: Any, plan: AgentPlanRequest) -> str | None:
    """Project mechanical native status without authoring or retrying anything."""
    binding = authoring_binding(plan)
    if binding is None:
        return None
    try:
        run_id = uuid.UUID(binding["run_id"])
        input_id = uuid.UUID(binding["input_id"])
        valid = binding["schema"] == AUTHORING_SCHEMA and binding["plan_version"] == plan.plan_version
    except (KeyError, TypeError, ValueError):
        valid = False
        run_id = input_id = None
    task = await db.get(RuntimeTask, run_id) if valid else None
    row = await db.get(SessionTurnInput, input_id) if valid else None
    if not valid or row is None:
        status = "needs_reconciliation"
    elif (
        row.tenant_id != plan.tenant_id
        or str(row.session_id) != str(plan.session_id)
        or (
            task is not None
            and (
                task.tenant_id != plan.tenant_id
                or task.parent_agent_id != plan.agent_id
                or str(task.parent_session_id) != str(plan.session_id)
                or task.root_user_id != plan.requested_by_user_id
            )
        )
    ):
        status = "needs_reconciliation"
    elif task is not None:
        status = task.status
    elif row.status in {"rejected", "cancelled", "blocked"}:
        status = "failed"
    else:
        status = "queued"
    binding["status"] = status
    metadata = dict(plan.metadata_json or {})
    metadata[AUTHORING_KEY] = binding
    if plan.status in {"draft", "planning", "planning_failed"}:
        if status == "needs_reconciliation":
            plan.status = "planning"
            metadata["planning_errors"] = [
                "Plan authoring requires operator reconciliation. The existing request will not be replayed."
            ]
        elif status in {"completed", "failed", "killed", "cancelled", "skipped"} and not binding.get(
            "launch_requested"
        ):
            plan.status = "planning_failed"
            plan.plan_hash = None
            metadata["planning_errors"] = ["The native authoring run ended without submitting a confirmable plan."]
        else:
            plan.status = "planning"
    if metadata != dict(plan.metadata_json or {}):
        plan.metadata_json = metadata
    return status


async def invoke_native_system_plan(plan: AgentPlanRequest, request: Any) -> None:
    """Admit one separately queued turn under the original requesting principal."""
    from app.services.plan_mode_service import PlanConflictError
    from app.services.runtime_user_identity import load_scoped_runtime_user
    from app.services.session_input_dispatch import input_runtime_run_id
    from app.services.session_live_input import submit_live_human_input
    from app.services.web_chat_runtime import _merge_runtime_permission_metadata
    from app.runtime.ccplus_contracts import DEFAULT_CCPLUS_PERMISSION_MODE

    if plan.requested_by_user_id is None or request.user_id != plan.requested_by_user_id:
        raise PlanConflictError("plan_authoring_principal_unavailable", "An authenticated plan requester is required.")
    async with tenant_scoped_session(plan.tenant_id) as db:
        stored = await db.scalar(select(AgentPlanRequest).where(AgentPlanRequest.id == plan.id).with_for_update())
        if stored is None or stored.status not in {"draft", "planning", "planning_failed"}:
            raise PlanConflictError("illegal_transition", "This plan is no longer awaiting authoring.")
        if (
            stored.agent_id != request.agent_id
            or stored.tenant_id != request.tenant_id
            or stored.requested_by_user_id != request.user_id
        ):
            raise PlanConflictError("plan_authoring_identity_mismatch", "The plan requester or work changed.")
        previous = authoring_binding(stored)
        if previous is not None:
            status = await reconcile_authoring(db, stored)
            if status not in {"completed", "failed", "killed", "cancelled", "skipped"}:
                await db.commit()
                return
        generation = int((previous or {}).get("generation") or 0) + 1
        agent = await db.get(Agent, stored.agent_id)
        user = await load_scoped_runtime_user(db, stored.requested_by_user_id, tenant_id=stored.tenant_id)
        if agent is None or user is None:
            raise PlanConflictError(
                "plan_authoring_principal_unavailable", "The plan requester or employee is unavailable."
            )
        if stored.session_id:
            session = await db.get(ChatSession, uuid.UUID(str(stored.session_id)))
        else:
            session_id = uuid.uuid5(stored.id, "system-plan-authoring-session")
            session = await db.get(ChatSession, session_id)
            if session is None:
                session = ChatSession(
                    id=session_id,
                    tenant_id=stored.tenant_id,
                    agent_id=stored.agent_id,
                    user_id=user.id,
                    title="Plan authoring",
                    source_channel="web",
                    audience_kind="private",
                )
                db.add(session)
                await db.flush()
            stored.session_id = str(session.id)
        if session is None or session.tenant_id != stored.tenant_id or session.agent_id != stored.agent_id:
            raise PlanConflictError("plan_authoring_work_unavailable", "The original plan work is unavailable.")
        input_id = uuid.uuid5(stored.id, f"system-plan-authoring:{stored.plan_version}:{generation}")
        run_id = input_runtime_run_id(input_id, successor=True)
        state = request.session_context.plan_mode
        binding = {
            "schema": AUTHORING_SCHEMA,
            "input_id": str(input_id),
            "run_id": str(run_id),
            "plan_version": stored.plan_version,
            "generation": generation,
            "status": "queued",
            "authorization_scopes": state.authorization_scopes,
            "mode": state.to_metadata(),
        }
        stored.metadata_json = {**dict(stored.metadata_json or {}), AUTHORING_KEY: binding}
        stored.status = "planning"
        runtime_metadata = _merge_runtime_permission_metadata(
            runtime_metadata={
                AUTHORING_KEY: {"plan_id": str(stored.id), "input_id": str(input_id)},
                "channel": session.source_channel or "web",
                "permission_mode": (session.transcript_metadata_json or {}).get("permission_mode")
                or agent.default_session_permission_mode
                or DEFAULT_CCPLUS_PERMISSION_MODE.value,
            },
            session_metadata=session.transcript_metadata_json,
        )
        await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=request.messages[0]["content"],
            display_content="Plan authoring",
            source="system_plan_run",
            input_id=input_id,
            idempotency_key=f"system-plan-authoring:{input_id}",
            requested_kind="queue_next_turn",
            plan_mode_requested=True,
            runtime_metadata=runtime_metadata,
        )
        await db.commit()
        plan.session_id = stored.session_id
        plan.metadata_json = dict(stored.metadata_json or {})
        plan.status = stored.status


async def load_system_plan_mode(db: Any, *, task: RuntimeTask, session: ChatSession | None) -> Any:
    """Recover the pre-armed mode only from this plan's admitted native input."""
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.runtime.session import PlanModeState

    metadata = dict(task.metadata_json or {})
    marker = metadata.get(AUTHORING_KEY)
    if marker is None:
        return None

    def denied() -> ExecutionAuthorityUnavailable:
        return ExecutionAuthorityUnavailable(
            code="system_plan_authoring_unbound", task=task, next_action="reconcile_plan_authoring"
        )

    try:
        if not isinstance(marker, dict):
            raise denied()
        plan = await db.get(AgentPlanRequest, uuid.UUID(marker["plan_id"]))
        binding = authoring_binding(plan) if plan is not None else None
        input_id = uuid.UUID(marker["input_id"])
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id) if row is not None else None
        if (
            plan is None
            or binding is None
            or session is None
            or row is None
            or command is None
            or binding.get("schema") != AUTHORING_SCHEMA
            or binding.get("input_id") != str(input_id)
            or binding.get("run_id") != str(task.id)
            or binding.get("plan_version") != plan.plan_version
            or plan.tenant_id != task.tenant_id
            or plan.agent_id != task.parent_agent_id
            or str(plan.session_id) != str(session.id)
            or str(task.parent_session_id) != str(session.id)
            or plan.requested_by_user_id != task.root_user_id
            or row.tenant_id != plan.tenant_id
            or row.session_id != session.id
            or str(metadata.get("session_v2_input_id")) != str(row.id)
            or str(metadata.get("session_v2_command_id")) != str(command.id)
            or command.principal_type != "user"
            or command.principal_id != plan.requested_by_user_id
            or command.tenant_id != plan.tenant_id
            or command.session_id != session.id
            or (command.target_json or {}).get("runtime_metadata", {}).get(AUTHORING_KEY) != marker
            or plan.status not in {"draft", "planning", "awaiting_confirmation"}
        ):
            raise denied()
        mode = PlanModeState(
            active=True,
            plan_id=str(plan.id),
            intent_type=plan.intent_type,
            original_request=plan.original_request,
            plan_file_path=f"workspace/plans/{plan.id}.plan.md",
            source="system_plan_run",
            reason="system_plan_run",
            authorization_scopes=binding.get("authorization_scopes"),
        )
        if mode.to_metadata() != binding.get("mode"):
            raise denied()
        return mode
    except (KeyError, TypeError, ValueError) as exc:
        raise denied() from exc
