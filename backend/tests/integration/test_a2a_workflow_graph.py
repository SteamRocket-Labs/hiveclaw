"""Real PostgreSQL graph admission/journal/recovery; no provider calls."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session as real_scoped
from app.models.chat_session import ChatSession
from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.models.workflow import WorkflowStep
from app.runtime.a2a_workflow import A2AWorkflowDefinition
from app.services import a2a_workflow_runtime as runtime
from .test_a2a_delegation_request_snapshot import _bind_runtime_store, _seed_tenant


@pytest.mark.asyncio
async def test_real_graph_start_replay_native_child_and_recovery(owner_sessionmaker, owner_engine, monkeypatch):
    import app.database as database
    from app.services import runtime_budget_service, runtime_task_worker
    from app.services.agent_tool_domains import messaging
    from app.services.workflow_launch import execute_claimed_workflow_run

    seeded = await _seed_tenant(owner_sessionmaker)
    tenant, user = seeded["tenant_id"], seeded["user_id"]
    root_agent, worker = seeded["parent_agent_id"], seeded["target_agent_id"]
    session_id, run_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChatSession(
                id=session_id,
                agent_id=root_agent,
                user_id=user,
                tenant_id=tenant,
                title="A2A graph",
                transcript_metadata_json={"permission_profile": {"mode": "default"}},
            )
        )
        await db.commit()
    _bind_runtime_store(monkeypatch, owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(database, "engine", owner_engine)
    monkeypatch.setattr(runtime_budget_service, "async_session", owner_sessionmaker)

    def scoped(tenant_id=None, **kwargs):
        return real_scoped(tenant_id, session_factory=owner_sessionmaker)

    async def resolve_agent(agent_id, **kwargs):
        from app.services.tenant_resolver import resolve_tenant_for_agent

        return await resolve_tenant_for_agent(agent_id, session_factory=owner_sessionmaker)

    async def notify(**kwargs):
        return None

    monkeypatch.setattr(runtime, "tenant_scoped_session", scoped)
    monkeypatch.setattr(messaging, "tenant_scoped_session", scoped)
    monkeypatch.setattr(messaging, "resolve_tenant_for_agent", resolve_agent)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    graph = A2AWorkflowDefinition.model_validate(
        {
            "name": "Persisted graph",
            "participants": {"worker": {"agent_id": str(worker)}},
            "nodes": [
                {
                    "id": "draft",
                    "type": "agent_handoff_step",
                    "agent_ref": "worker",
                    "task": "Create the report",
                    "output_contract": {"artifacts": [{"name": "report", "path": "workspace/report.md"}]},
                },
                {"id": "review", "type": "gate_step", "reason": "Review the actual report"},
            ],
            "edges": [{"from": "draft", "to": "review"}],
        }
    )
    kwargs = dict(
        tenant_id=tenant,
        agent_id=root_agent,
        requester_user_id=user,
        session_id=session_id,
        run_id=run_id,
        definition=graph,
        args={},
        permission_profile={"mode": "default"},
    )
    first = await runtime.start_run(**kwargs)
    replay = await runtime.start_run(**kwargs)
    assert first["replayed"] is False and replay["replayed"] is True
    with pytest.raises(runtime.A2AWorkflowConflict, match="another_execution"):
        await runtime.start_run(**{**kwargs, "requester_user_id": uuid4()})
    async with owner_sessionmaker() as db:
        assert (
            await db.execute(
                select(func.count())
                .select_from(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.root_runtime_task_id == run_id)
            )
        ).scalar_one() == 1
    result = await execute_claimed_workflow_run(run_id, session_factory=owner_sessionmaker)
    assert result["status"] == "pending", result
    async with owner_sessionmaker() as db:
        step = (await db.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalar_one()
        journal = json.loads(step.result_ref)
        child_id = UUID(journal["task_id"])
        child = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == child_id))).scalar_one()
        assert child.task_type == "delegation" and child.child_agent_id == worker
        assert child.root_user_id == user and child.root_runtime_task_id == run_id
        assert child.metadata_json["tool_profile"] == "agent_message"
        assert child.child_session_id != str(session_id)
    # A restarted graph worker observes the same child, never a second send.
    again = await execute_claimed_workflow_run(run_id, session_factory=owner_sessionmaker)
    assert again["status"] == "pending"
    async with owner_sessionmaker() as db:
        assert (
            await db.execute(
                select(func.count())
                .select_from(RuntimeTask)
                .where(RuntimeTask.root_runtime_task_id == run_id, RuntimeTask.task_type == "delegation")
            )
        ).scalar_one() == 1
        child = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == child_id))).scalar_one()
        child.status = "completed"
        await db.commit()
    # Terminal prose/status without its required immutable file cannot advance the gate.
    missing = await execute_claimed_workflow_run(run_id, session_factory=owner_sessionmaker)
    assert missing["status"] == "suspended" and "required_artifact_missing" in missing["reason"]
    from app.api import a2a_workflows as api
    from fastapi import HTTPException
    from types import SimpleNamespace

    async with owner_sessionmaker() as db:
        with pytest.raises(HTTPException) as denied:
            await api._authorize_run(db, SimpleNamespace(id=uuid4()), root_agent, run_id)
        assert denied.value.status_code == 404
        requester = (await db.execute(select(User).where(User.id == user))).scalar_one()
        retried = await api.control(
            root_agent, run_id, api.GraphControlRequest(action="retry", node_id="draft"), db=db, current_user=requester
        )
        assert retried["status"] == "pending"
        assert retried["steps"][0]["journal"]["attempt"] == 2
        assert retried["steps"][0]["journal"]["previous_attempts"][0]["task_id"] == str(child_id)
    next_attempt = await execute_claimed_workflow_run(run_id, session_factory=owner_sessionmaker)
    assert next_attempt["status"] == "pending", next_attempt
    async with owner_sessionmaker() as db:
        assert (
            await db.execute(
                select(func.count())
                .select_from(RuntimeTask)
                .where(RuntimeTask.root_runtime_task_id == run_id, RuntimeTask.task_type == "delegation")
            )
        ).scalar_one() == 2
        # An ambiguous child cannot become a falsely cancelled graph or be retried.
        child = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.root_runtime_task_id == run_id,
                    RuntimeTask.id != child_id,
                    RuntimeTask.task_type == "delegation",
                )
            )
        ).scalar_one()
        child.status = "needs_reconciliation"
        retry_child_id = child.id
        await db.commit()
        from app.agents import orchestrator

        async def uncertain_cancel(*args, **kwargs):
            return None

        monkeypatch.setattr(orchestrator, "cancel_async_delegation", uncertain_cancel)
        requester = (await db.execute(select(User).where(User.id == user))).scalar_one()
        cancelled = await api.control(
            root_agent, run_id, api.GraphControlRequest(action="cancel"), db=db, current_user=requester
        )
        assert cancelled["status"] == "suspended"
        assert cancelled["reason"] == "cancel_needs_reconciliation"
        await execute_claimed_workflow_run(run_id, session_factory=owner_sessionmaker)
        with pytest.raises(HTTPException) as denied:
            await api.control(
                root_agent,
                run_id,
                api.GraphControlRequest(action="retry", node_id="draft"),
                db=db,
                current_user=requester,
            )
        assert denied.value.status_code == 409
        await db.rollback()
        child = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == retry_child_id))).scalar_one()
        child.status = "killed"
        await db.commit()
        requester = (await db.execute(select(User).where(User.id == user))).scalar_one()
        cancelled = await api.control(
            root_agent, run_id, api.GraphControlRequest(action="cancel"), db=db, current_user=requester
        )
        assert cancelled["status"] == "killed"
