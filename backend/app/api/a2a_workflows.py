"""Owner-scoped full-Agent graph previews, runs and recovery controls."""

from datetime import UTC, datetime
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action
from app.core.security import get_current_user
from app.database import get_db
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.a2a_workflow import A2AGateNode, A2AWorkflowDefinition, normalize_a2a_args
from app.services import a2a_workflow_runtime as runtime
from app.services.workflow_runtime_service import PGRunLeaseManager

router = APIRouter(prefix="/agents/{agent_id}/a2a-workflows", tags=["a2a-workflows"])


class GraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    definition: A2AWorkflowDefinition
    args: dict = Field(default_factory=dict)


class GraphStartRequest(GraphRequest):
    run_id: UUID
    definition_hash: str


class GraphControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["resume", "approve", "reject", "retry", "cancel"]
    node_id: str | None = None


async def _authorize_run(db, user, agent_id, run_id, *, writable=False):
    try:
        task, steps = await runtime.load_run(db, run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if task.parent_agent_id != agent_id or task.root_user_id != user.id:
        raise HTTPException(404, "A2A workflow run not found")
    await authorize_session_action(
        db,
        user,
        agent_id=agent_id,
        session_id=UUID(task.parent_session_id),
        action="a2a_workflow_control" if writable else "a2a_workflow_read",
        require_writable=writable,
    )
    return task, steps


@router.post("/preview")
async def preview(
    agent_id: UUID,
    payload: GraphRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    decision = await authorize_session_action(
        db, current_user, agent_id=agent_id, session_id=payload.session_id, action="a2a_workflow_preview"
    )
    try:
        args = normalize_a2a_args(payload.definition, payload.args)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if decision.agent.id in {item.agent_id for item in payload.definition.participants.values()}:
        raise HTTPException(422, "The coordinating Agent must be separate from the graph participants")
    from app.services.agent_tool_domains.messaging import _resolve_target_agent_runtime

    checks = []
    for node in payload.definition.ordered_nodes():
        if isinstance(node, A2AGateNode):
            continue
        source = runtime._source_node(payload.definition, node.id)
        source_id = payload.definition.participants[source.agent_ref].agent_id if source else agent_id
        target_id = payload.definition.participants[node.agent_ref].agent_id
        _, _, _, error = await _resolve_target_agent_runtime(source_id, "", target_agent_id=target_id)
        checks.append(
            {
                "node_id": node.id,
                "source_agent_id": str(source_id),
                "target_agent_id": str(target_id),
                "allowed": error is None,
                "reason": error,
            }
        )
    return {
        "kind": "a2a_workflow",
        "definition_hash": payload.definition.definition_hash,
        "definition": payload.definition.model_dump(mode="json", by_alias=True),
        "args": args,
        "node_order": [node.id for node in payload.definition.ordered_nodes()],
        "collaboration_checks": checks,
        "admissible": all(check["allowed"] for check in checks),
        "started": False,
    }


@router.post("/runs", status_code=201)
async def start(
    agent_id: UUID,
    payload: GraphStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    decision = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=payload.session_id,
        action="a2a_workflow_start",
        require_writable=True,
    )
    if payload.definition_hash != payload.definition.definition_hash:
        raise HTTPException(409, "definition_hash_mismatch")
    if agent_id in {item.agent_id for item in payload.definition.participants.values()}:
        raise HTTPException(422, "The coordinating Agent must be separate from the graph participants")
    metadata = dict(decision.session.transcript_metadata_json or {})
    await db.commit()
    try:
        return await runtime.start_run(
            tenant_id=decision.agent.tenant_id,
            agent_id=agent_id,
            requester_user_id=current_user.id,
            session_id=payload.session_id,
            run_id=payload.run_id,
            definition=payload.definition,
            args=payload.args,
            permission_profile=metadata.get("permission_profile"),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/runs")
async def list_runs(
    agent_id: UUID, session_id: UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    await authorize_session_action(
        db, current_user, agent_id=agent_id, session_id=session_id, action="a2a_workflow_list"
    )
    rows = (
        await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.task_type == "workflow",
                RuntimeTask.parent_agent_id == agent_id,
                RuntimeTask.root_user_id == current_user.id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.metadata_json["kind"].astext == "a2a_workflow",
            )
            .order_by(RuntimeTask.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return [
        {
            "run_id": str(row.id),
            "status": row.status,
            "name": (row.metadata_json or {}).get("definition", {}).get("name", "A2A Workflow"),
        }
        for row in rows
    ]


@router.get("/runs/{run_id}")
async def read_run(
    agent_id: UUID, run_id: UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    task, steps = await _authorize_run(db, current_user, agent_id, run_id)
    return runtime.run_payload(task, steps)


@router.post("/runs/{run_id}/control")
async def control(
    agent_id: UUID,
    run_id: UUID,
    payload: GraphControlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.agents.orchestrator import cancel_async_delegation
    from app.services.runtime_task_service import get_runtime_task_record
    from app.services.runtime_task_worker import notify_runtime_task_worker

    await _authorize_run(db, current_user, agent_id, run_id, writable=True)
    await db.commit()
    lease = await PGRunLeaseManager(None).try_acquire(run_id)
    if lease is None:
        raise HTTPException(409, "run_busy_retry_same_action")
    try:
        task, steps = await runtime.load_run(db, run_id, lock=True)
        if task.status in {"completed", "killed"}:
            raise HTTPException(409, "run_is_terminal")
        if payload.action == "cancel":
            # Reconcile every dispatched child before claiming the graph is cancelled.
            # Do not hold the root row lock while native cancellation settles its ledger.
            await db.commit()
            unsettled = []
            for row in steps:
                journal = json.loads(row.result_ref or "{}")
                child_id = journal.get("task_id")
                if not child_id:
                    continue
                child = await get_runtime_task_record(child_id)
                if child is None:
                    unsettled.append(child_id)
                    continue
                if child and child["status"] not in {"completed", "failed", "killed", "skipped"}:
                    await cancel_async_delegation(
                        child_id, parent_agent_id=UUID(journal["source_agent_id"]), force=True
                    )
                    child = await get_runtime_task_record(child_id)
                    if child is None or child["status"] not in {"completed", "failed", "killed", "skipped"}:
                        unsettled.append(child_id)
            task, steps = await runtime.load_run(db, run_id, lock=True)
            task.status = "suspended" if unsettled else "killed"
            metadata = dict(task.metadata_json or {})
            metadata["a2a_reason"] = "cancel_needs_reconciliation" if unsettled else "user_cancelled"
            metadata["cancel_requested_by"] = str(current_user.id)
            task.metadata_json = metadata
            if not unsettled:
                task.completed_at = datetime.now(UTC)
        else:
            if task.status != "suspended":
                raise HTTPException(409, "run_must_be_suspended")
            row = next((r for r in steps if r.step_id == payload.node_id), None)
            if payload.action != "resume" and row is None:
                raise HTTPException(404, "node_not_found")
            if payload.action in {"approve", "reject"}:
                if row.step_type != "gate_step" or row.status != "suspended":
                    raise HTTPException(409, "node_is_not_a_waiting_human_gate")
                journal = json.loads(row.result_ref or "{}")
                journal.update(
                    {
                        "decision": payload.action,
                        "decided_by": str(current_user.id),
                        "decided_at": datetime.now(UTC).isoformat(),
                    }
                )
                row.result_ref = json.dumps(journal)
            elif payload.action == "retry":
                if row.step_type != "agent_handoff_step" or row.status != "suspended":
                    raise HTTPException(409, "node_is_not_retryable")
                journal = json.loads(row.result_ref or "{}")
                child = await get_runtime_task_record(journal.get("task_id", ""))
                if not child or child["status"] not in {"completed", "failed", "killed", "skipped"}:
                    raise HTTPException(409, "child_must_be_reconciled_before_retry")
                previous = {k: v for k, v in journal.items() if k != "previous_attempts"}
                row.result_ref = json.dumps(
                    {
                        "attempt": int(journal.get("attempt", 1)) + 1,
                        "previous_attempts": [*journal.get("previous_attempts", []), previous],
                    }
                )
                row.status, row.error = "pending", None
            task.status = "suspended" if payload.action == "reject" else "pending"
            metadata = dict(task.metadata_json or {})
            metadata["a2a_reason"] = "human_gate_rejected" if payload.action == "reject" else None
            task.metadata_json = metadata
        task.claimed_by, task.claim_expires_at, task.scheduled_at = None, None, None
        if task.status == "killed":
            from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

            await settle_and_enqueue_runtime_task_terminal(
                db,
                task,
                terminal_source="a2a_workflow.user_cancel",
                root_reason_code="user_cancelled",
                root_state="cancelled",
            )
        await db.commit()
        result = runtime.run_payload(task, steps)
    finally:
        await lease.release()
    if result["status"] == "pending":
        await notify_runtime_task_worker(reason="a2a_workflow_user_control", runtime_task_id=run_id)
    return result
