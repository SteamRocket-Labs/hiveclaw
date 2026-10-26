"""Real-PostgreSQL proof for cross-worker Dynamic Workflow confirmation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.workflow_confirmation_service import (
    WorkflowConfirmationConflict,
    claim_workflow_preview_start,
    create_workflow_preview,
    create_workflow_proposal,
    load_workflow_candidate,
    load_workflow_preview,
)


async def _seed_identity(sessionmaker):
    suffix = uuid.uuid4().hex[:10]
    async with sessionmaker() as db:
        tenant = Tenant(name="Workflow Confirmation Tenant", slug=f"workflow-confirmation-{suffix}")
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"workflow-user-{suffix}",
            email=f"workflow-{suffix}@example.test",
            password_hash="x",
            display_name="Workflow User",
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name="Workflow Agent", creator_id=user.id, owner_user_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        chat_session = ChatSession(agent_id=agent.id, tenant_id=tenant.id, user_id=user.id)
        db.add(chat_session)
        await db.commit()
        return tenant.id, user.id, agent.id, chat_session.id


async def test_preview_survives_worker_restart_and_reconciles_crash_window(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_identity(owner_sessionmaker)
    proposal_payload = {
        "ok": True,
        "status": "dynamic_workflow_proposed",
        "goal": "Audit runtime",
        "candidates": [
            {
                "candidate_id": "audit",
                "lowered_definition": {"name": "audit", "args_schema": {}, "steps": []},
                "preview_args": {},
                "definition_hash": "definition-hash",
                "args_hash": "args-hash",
            }
        ],
    }

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as first_worker:
        proposal = await create_workflow_proposal(
            first_worker,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            proposal=proposal_payload,
        )
        preview = await create_workflow_preview(
            first_worker,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            definition={"name": "audit", "args_schema": {}, "steps": []},
            args={},
            definition_hash="definition-hash",
            args_hash="args-hash",
            preview_payload={"confirmation_required": False, "confirmation_reasons": []},
            proposal=proposal,
            candidate_id="audit",
        )
        proposal_id = proposal.id
        preview_id = preview.id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as second_worker:
        loaded_proposal, candidate = await load_workflow_candidate(
            second_worker,
            proposal_id=proposal_id,
            candidate_id="audit",
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )
        assert loaded_proposal.id == proposal_id
        assert candidate["candidate_id"] == "audit"
        claim = await claim_workflow_preview_start(
            second_worker,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            confirmation_source="agent_current_turn_no_confirmation_required",
            confirmation_evidence_id="turn-1",
        )
        assert claim.outcome == "claimed"
        assert claim.preview.run_id == preview_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as competing_worker:
        with pytest.raises(WorkflowConfirmationConflict) as exc:
            await claim_workflow_preview_start(
                competing_worker,
                preview_id=preview_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                user_id=user_id,
                confirmation_source="agent_current_turn_no_confirmation_required",
                confirmation_evidence_id="turn-2",
            )
        assert exc.value.code == "start_in_progress"

    # Crash window: RuntimeTask was durably created but preview finalization was
    # not. The next worker sees the deterministic run id and reconciles instead
    # of creating a duplicate run.
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as crashed_worker:
        preview = await load_workflow_preview(
            crashed_worker,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            for_update=True,
        )
        preview.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        crashed_worker.add(
            RuntimeTask(
                id=preview_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="pending",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
            )
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as recovery_worker:
        recovered = await claim_workflow_preview_start(
            recovery_worker,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            confirmation_source="agent_current_turn_no_confirmation_required",
            confirmation_evidence_id="turn-3",
        )
        assert recovered.outcome == "replay"
        assert recovered.run_exists is True
        assert recovered.preview.status == "started"
        assert recovered.preview.run_id == preview_id


async def test_preview_response_is_committed_before_request_teardown(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    import asyncio
    import socket

    import httpx
    import uvicorn
    from fastapi import FastAPI

    from app.api.workflows import router
    from app.core.security import get_current_user
    from app.database import get_db

    tenant_id, user_id, agent_id, session_id = await _seed_identity(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)

    teardown_started = asyncio.Event()
    release_teardown = asyncio.Event()

    async def deferred_commit_db():
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db
            teardown_started.set()
            await asyncio.wait_for(release_teardown.wait(), 10)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = deferred_commit_db
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                assert not serving.done()
                await asyncio.sleep(0.01)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=10) as client:
            response = await client.post(
                f"/api/agents/{agent_id}/workflows/preview",
                json={
                    "definition": {
                        "name": "Committed preview",
                        "args_schema": {},
                        "steps": [
                            {
                                "id": "verify",
                                "type": "agent_step",
                                "leaf": {"name": "preview-verifier", "type": "explorer"},
                                "task": "Verify the saved preview.",
                            }
                        ],
                    },
                    "args": {},
                    "session_id": str(session_id),
                },
            )
            assert response.status_code == 200, response.text
            await asyncio.wait_for(teardown_started.wait(), 10)
            payload = response.json()
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as reader:
                preview = await load_workflow_preview(
                    reader,
                    preview_id=uuid.UUID(payload["preview_id"]),
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    user_id=user_id,
                )
                assert preview.status == "ready"
                assert preview.definition_hash == payload["definition_hash"]
                assert str(preview.session_id) == payload["session_id"]
    finally:
        release_teardown.set()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
