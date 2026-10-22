"""Legacy queued batches recover without borrowing another requester's work root."""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("requesters", ["same", "different"])
async def test_legacy_queued_batch_releases_ambiguous_authority_then_readmits_each_requester(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, requesters,  # noqa: F811
):
    from app import database
    from app.api.admin import router as admin_router
    from app.api.plans import router as plan_router
    from app.api.triggers import router as trigger_router
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.models.agent import Agent, AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.models.llm import LLMModel
    from app.services import runtime_task_worker, trigger_daemon
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.trigger_instruction_sources import native_trigger_input_sources

    app, (tenant_id, owner_id, agent_id, owner_session_id, _active_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(admin_router, prefix="/api")
    app.include_router(plan_router, prefix="/api")
    app.include_router(trigger_router, prefix="/api")
    other_id, other_session_id, model_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        other = User(id=other_id, tenant_id=tenant_id, username="schedule-" + other_id.hex,
            email=other_id.hex + "@schedule.test", display_name="Other requester", password_hash="synthetic", role="member")
        operator = User(id=uuid.uuid4(), tenant_id=tenant_id, username="operator-" + other_id.hex,
            email=other_id.hex + "@operator.test", display_name="Operator", password_hash="synthetic", role="platform_admin")
        db.add(operator)
        db.add(other)
        db.add(LLMModel(id=model_id, tenant_id=tenant_id, label="Scheduled native model", provider="openai",
            model="synthetic-scheduled-model", api_key_encrypted="synthetic-no-provider-call", enabled=True))
        await db.flush()
        db.add(AgentPermission(agent_id=agent_id, tenant_id=tenant_id, scope_type="company", access_level="use"))
        db.add(ChatSession(id=other_session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=other_id,
            source_channel="web", title="Other requester's private work"))
        (await db.get(Agent, agent_id)).primary_model_id = model_id
        await db.commit()
        owner = await db.get(User, owner_id)

    original_factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original_factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    scoped_runtime_factories()
    data_root = tmp_path / "agents"
    data_root.mkdir()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(data_root))
    wakeups, leases = [], set()

    async def wakeup(**payload):
        wakeups.append(payload)

    async def acquire(trigger_id, event_key):
        key = (trigger_id, event_key)
        if key in leases:
            return False
        leases.add(key)
        return True

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", wakeup)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", acquire)
    trigger_ids, expected_owners, markers = [], {}, {}
    endpoint = f"/api/agents/{agent_id}"
    actors = [(owner, owner_session_id), (owner, owner_session_id) if requesters == "same" else (other, other_session_id)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://requester-schedules.test") as client:
        for index, (actor, session_id) in enumerate(actors):
            app.dependency_overrides[get_current_user] = lambda actor=actor: actor
            marker = f"PRIVATE_SCHEDULE_{index}_{actor.id.hex}"
            draft = await client.post(endpoint + "/triggers", json={"name": f"requester_job_{index}",
                "type": "interval", "is_enabled": False, "config": {"minutes": 15}, "reason": marker})
            assert draft.status_code == 201, draft.text
            trigger_id = uuid.UUID(draft.json()["id"])
            review = await client.post(endpoint + "/plan-recommendations", json={
                "original_request": "Enable this scheduled task without plan mode.", "session_id": str(session_id),
                "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
            assert review.status_code == 201, review.text
            declined = await client.post(endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
            assert declined.status_code == 200, declined.text
            enabled = await client.patch(endpoint + "/triggers/" + str(trigger_id), json={"is_enabled": True,
                "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
            assert enabled.status_code == 200, enabled.text
            trigger_ids.append(trigger_id)
            expected_owners[str(trigger_id)], markers[str(trigger_id)] = actor.id, marker

    async with owner_sessionmaker() as db:
        for trigger_id in trigger_ids:
            (await db.get(AgentTrigger, trigger_id)).created_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        triggers = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.id.in_(trigger_ids))))
    now = datetime.now(timezone.utc)
    allowed, reason, summary, metadata = await trigger_daemon._preflight_trigger_group(agent_id, triggers, now)
    assert allowed, (reason, summary, metadata)
    legacy_id = await trigger_daemon._create_trigger_runtime_task(agent_id, triggers,
        metadata_json={**metadata, "preflight_allowed": allowed})
    assert legacy_id is not None
    legacy_id = uuid.UUID(str(legacy_id))
    assert await trigger_daemon._mark_trigger_fire_started(agent_id, triggers, now=now,
        runtime_task_id=legacy_id, event_keys={row.id: "legacy-queued-" + row.id.hex for row in triggers})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        legacy = await db.get(RuntimeTask, legacy_id)
        assert legacy.root_user_id == (owner_id if requesters == "same" else None)
        claims = await RuntimeTaskClaimService(db=db, worker_id="legacy-scheduled-worker",
            task_types=["trigger"], lease_seconds=60).claim_available(batch_size=2)
        assert {row.id for row in claims} == {legacy_id}
    error = None
    dispatched = None
    try:
        dispatched = await trigger_daemon.execute_claimed_trigger_runtime_task(str(legacy_id))
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    if requesters == "different":
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            held = await db.get(RuntimeTask, legacy_id)
            observed_hold = {"error": error, "dispatched": dispatched, "status": held.status,
                "child_session_id": held.child_session_id, "metadata": held.metadata_json}
            (tmp_path / "legacy-queued-batch-held.json").write_text(json.dumps(observed_hold, indent=2))
            assert held.status == "needs_reconciliation" and held.child_session_id is None, observed_hold
            rows = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.id.in_(trigger_ids))))
            assert all(row.config["_fire_inflight"]["hold"] is True and row.fire_count == 0 for row in rows)
            assert held.metadata_json["fire_event_payloads"] == {str(row.id): {} for row in rows}
        assert not await trigger_daemon.execute_claimed_trigger_runtime_task(str(legacy_id))
        counts = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(
            worker_id="legacy-trigger-projection", limit=20, session_factory=app_user_sessionmaker)
        assert counts["delivered"] >= 1, counts
        app.dependency_overrides[get_current_user] = lambda: operator
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://legacy-recovery.test") as client:
            path = f"/api/admin/runtime-reconciliation/{legacy_id}"
            inspect = await client.get(path, params={"tenant_id": str(tenant_id)})
            assert inspect.status_code == 200, inspect.text
            view = inspect.json()
            assert view["reason"] == "trigger_batch_requester_mismatch" and view["retry_allowed"] is False
            assert view["trigger_disposition_readiness"]["ready"] is True, view
            rejected = await client.post(path + "/action", params={"tenant_id": str(tenant_id)},
                json={"action": "retry", "reason": "Never replay a requester-ambiguous fire."})
            assert rejected.status_code == 409, rejected.text
            released = await client.post(path + "/action", params={"tenant_id": str(tenant_id)},
                json={"action": "archive", "trigger_disposition": "release",
                    "reason": "No input was dispatched; release both exact intents for current requester admission."})
            assert released.status_code == 200, released.text
            assert released.json()["status"] == "killed", released.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        legacy = await db.get(RuntimeTask, legacy_id)
        reflections = list(await db.scalars(select(ChatSession).where(ChatSession.agent_id == agent_id,
            ChatSession.session_kind == "trigger_run")))
        inputs = list(await db.scalars(select(SessionTurnInput).join(ChatSession,
            ChatSession.id == SessionTurnInput.session_id).where(ChatSession.agent_id == agent_id)))
        budget = await db.get(RuntimeBudgetRun, legacy.budget_run_id)
        observed = {"error": error, "dispatched": dispatched, "legacy_id": str(legacy_id),
            "status": legacy.status, "metadata": legacy.metadata_json, "budget_status": budget.status,
            "reflections": [str(row.id) for row in reflections], "inputs": [str(row.id) for row in inputs]}
        (tmp_path / "legacy-queued-batch-before-readmission.json").write_text(json.dumps(observed, indent=2))
        assert error is None, observed
        assert dispatched is (requesters == "same"), observed
        if requesters == "different":
            assert legacy.status == "killed" and legacy.child_session_id is None, observed
            assert legacy.metadata_json["restart_resume_blocker"] == "trigger_batch_requester_mismatch"
            assert not reflections and not inputs, observed
            rows = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.id.in_(trigger_ids))))
            assert all(row.is_enabled and row.fire_count == 0 and row.last_fired_at is None
                and "_fire_inflight" not in row.config for row in rows)
        else:
            assert legacy.status == "completed" and len(reflections) == len(inputs) == 1, observed

    await trigger_daemon._tick()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assert (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
        wrappers = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.task_type == "trigger", RuntimeTask.id != legacy_id)))
        assert len(wrappers) == (2 if requesters == "different" else 0)
        claims = await RuntimeTaskClaimService(db=db, worker_id="readmitted-scheduled-worker",
            task_types=["trigger"], lease_seconds=60).claim_available(batch_size=2)
        assert {row.id for row in claims} == {row.id for row in wrappers}
    for claim in claims:
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(claim.id))
    delivered = wrappers if requesters == "different" else [legacy]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        for prior in delivered:
            current = await db.get(RuntimeTask, prior.id)
            assert current.status == "completed", current.metadata_json
            ids = current.metadata_json["trigger_ids"]
            assert {expected_owners[value] for value in ids} == {current.root_user_id}
            budget = await db.get(RuntimeBudgetRun, current.budget_run_id)
            assert budget.root_user_id == current.root_user_id and budget.root_runtime_task_id == current.id
            assert budget.root_session_id == current.root_session_id and current.root_session_id is not None
            session = await db.get(ChatSession, uuid.UUID(current.child_session_id))
            assert session.user_id == current.root_user_id
            rows = list(await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == session.id)))
            assert len(rows) == 1
            row = rows[0]
            command = await db.get(SessionCommand, row.command_id)
            assert command.principal_id == current.root_user_id
            references, complete = await native_trigger_input_sources(db, tenant_id=tenant_id, agent_id=agent_id,
                row=row, command=command)
            assert complete and len(references) == 1
            content = json.dumps(row.content_parts_json)
            assert all((marker in content) == (identity in ids) for identity, marker in markers.items())
        rows = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.id.in_(trigger_ids))))
        assert all(row.fire_count == 1 and "_fire_inflight" not in row.config for row in rows)
    for prior in [legacy, *wrappers]:
        assert not await trigger_daemon.execute_claimed_trigger_runtime_task(str(prior.id))
    await trigger_daemon._tick()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        current_ids = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.task_type == "trigger")))
        assert current_ids == {legacy_id, *(row.id for row in wrappers)}
