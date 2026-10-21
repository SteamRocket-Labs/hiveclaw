"""Scheduled local work keeps the actual selected runner and original work."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sys
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3a_local_employee_control_postgres import local_work  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("delivery,narrowing", [
    ("same_session", None), ("reflection", None),
    ("same_session", "generation"), ("reflection", "generation"),
    ("same_session", "budget"), ("reflection", "lifecycle"),
    ("same_session", "source_changed"), ("same_session", "late_report"),
    ("same_session", "pause"), ("same_session", "control"), ("reflection", "foreign_context"),
    ("same_session", "reclaim"), ("reflection", "reclaim"),
    ("same_session", "unsupported_model"), ("reflection", "unsupported_tools"), ("same_session", "unsupported_workdir"),
])
async def test_local_routine_needs_no_cloud_model_and_reports_in_its_original_work(
    local_work, app_user_sessionmaker, scoped_runtime_factories, monkeypatch, tmp_path, delivery, narrowing,  # noqa: F811
):
    from app import database
    from app.api import autonomy, local_agent_channel, plans, runtime_budgets, triggers, websocket
    from app.core.security import create_access_token
    from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelSession
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.local_bridge import LocalAgentBridgeConnection
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services import local_bridge_service, runtime_task_worker, trigger_daemon
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    seed = local_work
    real_tenant_session, real_factory = database.tenant_scoped_session, database.async_session

    @asynccontextmanager
    async def scoped():
        async with real_tenant_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
            yield db

    @asynccontextmanager
    async def tenant_scoped(tenant_id=None, **kwargs):
        kwargs.pop("session_factory", None)
        async with real_tenant_session(tenant_id, session_factory=app_user_sessionmaker, **kwargs) as db:
            yield db

    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("app."):
            continue
        for name, value in list(vars(module).items()):
            if value is real_factory:
                monkeypatch.setattr(module, name, scoped)
            elif value is real_tenant_session:
                monkeypatch.setattr(module, name, tenant_scoped)
    scoped_runtime_factories()
    cloud_requests = []

    async def no_cloud_model(*args, **kwargs):
        cloud_requests.append(True)
        raise AssertionError("A selected local employee must never fall back to a cloud model")

    async def no_background_wakeup(**kwargs):
        pass

    monkeypatch.setattr(websocket, "call_llm", no_cloud_model)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_background_wakeup)
    token, other_token = local_bridge_service.generate_bridge_token(), local_bridge_service.generate_bridge_token()
    async with scoped() as db:
        for context, secret in ((seed["selected"], token), (seed["other"], other_token)):
            connection = await db.get(LocalAgentBridgeConnection, context.connection_id)
            connection.token_hash = local_bridge_service.hash_secret(secret)
        user = await db.get(User, seed["user_id"])
        foreign_session_id = None
        if narrowing in {"control", "foreign_context"}:
            user.role = "org_admin"
        if narrowing == "foreign_context":
            foreign_user = User(id=uuid.uuid4(), tenant_id=seed["tenant_id"], username=uuid.uuid4().hex,
                email=uuid.uuid4().hex + "@private.test", password_hash="test-only-not-a-login",
                display_name="Private source owner", role="member")
            db.add(foreign_user)
            await db.flush()
            foreign_session = ChatSession(id=uuid.uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                user_id=foreign_user.id, title="Another person's private work", audience_kind="private")
            db.add(foreign_session)
            foreign_session_id = foreign_session.id
        jwt = create_access_token(str(user.id), user.role, str(user.tenant_id))
        original = await db.get(LocalAgentChannelSession, seed["work_id"])
        chat_session_id = original.chat_session_id
        await db.commit()
    app = FastAPI()
    for router in (triggers.router, plans.router, autonomy.router, local_agent_channel.router, runtime_budgets.router):
        app.include_router(router, prefix="/api")

    async def request_db():
        async with scoped() as db:
            yield db
            await db.commit()

    app.dependency_overrides[database.get_db] = request_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local-routine.test",
        headers={"Authorization": "Bearer " + jwt}) as owner, AsyncClient(
        transport=ASGITransport(app=app), base_url="http://local-routine.test",
        headers={"Authorization": "Bearer " + token}) as runner:
        initial = await runner.get("/api/local-bridge/channel/poll")
        assert initial.status_code == 200, initial.text
        assert [message["id"] for message in initial.json()["messages"]] == [seed["message_id"]]
        completed = await runner.post("/api/local-bridge/channel/report", json={"session_id": str(seed["work_id"]),
            "message_id": seed["message_id"], "status": "completed", "output": "The original local work is recorded."})
        assert completed.status_code == 200, completed.text
        endpoint = f"/api/agents/{seed['agent_id']}"
        explicit_controls = {"model_id": str(uuid.uuid4())} if narrowing == "unsupported_model" else (
            {"toolset": ["shell_execute"]} if narrowing == "unsupported_tools" else (
            {"workdir": "/required/job/workspace"} if narrowing == "unsupported_workdir" else {}))
        draft = await owner.post(endpoint + "/triggers", json={"name": "local_stock_check", "type": "interval",
            "config": {"minutes": 30, **explicit_controls, **({"context_from": ["session:" + str(foreign_session_id)]} if foreign_session_id else {}), **({"delivery": "same_session", "source_session_id": str(chat_session_id)} if delivery == "same_session" else {})},
            "reason": "ROUTINE_LOCAL_CHECK: inspect current local stock; retain nonactionable results in this work.",
            "is_enabled": False})
        assert draft.status_code == 201, draft.text
        trigger_id = draft.json()["id"]
        review = await owner.post(endpoint + "/plan-recommendations", json={"session_id": str(chat_session_id),
            "original_request": "Enable this local stock check within its existing authority and resource limits.",
            "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert review.status_code == 201, review.text
        declined = await owner.post(endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
        assert declined.status_code == 200, declined.text
        enabled = await owner.patch(endpoint + "/triggers/" + trigger_id, json={"is_enabled": True,
            "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
        assert enabled.status_code == 200, enabled.text
        fired = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
        if explicit_controls:
            assert fired["fired"] is False and fired["reason"] == "local_agent_trigger_control_unavailable", fired
            assert cloud_requests == []
            return
        assert fired["fired"], fired
        wrapper_id = uuid.UUID(fired["runtime_task_id"])
        async with scoped() as db:
            claim = await RuntimeTaskClaimService(db=db, worker_id="local-routine-test", task_types=["trigger"],
                lease_seconds=60).claim_available(batch_size=1)
            assert [task.id for task in claim] == [wrapper_id]
        if narrowing == "reclaim":
            class WorkerLost(BaseException):
                pass

            original_terminal = trigger_daemon._update_trigger_runtime_task

            async def lose_worker_before_wrapper_commit(*args, **kwargs):
                if kwargs.get("status") == "completed":
                    raise WorkerLost()
                return await original_terminal(*args, **kwargs)

            with monkeypatch.context() as lost_worker:
                lost_worker.setattr(trigger_daemon, "_update_trigger_runtime_task", lose_worker_before_wrapper_commit)
                with pytest.raises(WorkerLost):
                    await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
            waiting = await runner.get("/api/local-bridge/channel/poll")
            assert waiting.status_code == 200 and waiting.json()["messages"] == [], waiting.text
            async with scoped() as db:
                queued_messages = list(await db.scalars(select(LocalAgentChannelMessage).where(
                    LocalAgentChannelMessage.source_agent_id == seed["agent_id"],
                    LocalAgentChannelMessage.id != uuid.UUID(seed["message_id"]))))
                assert len(queued_messages) == 1 and queued_messages[0].delivery_attempt_count == 0
                retained_message_id, retained_replay_key = queued_messages[0].id, queued_messages[0].replay_key
                wrapper = await db.get(RuntimeTask, wrapper_id)
                wrapper.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            async with scoped() as db:
                reclaimed = await RuntimeTaskClaimService(db=db, worker_id="local-routine-reclaimed", task_types=["trigger"],
                    lease_seconds=60).claim_available(batch_size=1)
                assert [task.id for task in reclaimed] == [wrapper_id]
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
        await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
        assert cloud_requests == []
        if narrowing == "foreign_context":
            async with scoped() as db:
                messages = list(await db.scalars(select(LocalAgentChannelMessage).where(
                    LocalAgentChannelMessage.source_agent_id == seed["agent_id"],
                    LocalAgentChannelMessage.id != uuid.UUID(seed["message_id"]))))
                assert messages == []
                wrapper = await db.get(RuntimeTask, wrapper_id)
                assert wrapper.status == "failed"
                assert "source" in wrapper.result_summary.lower()
            assert (await runner.get("/api/local-bridge/channel/poll")).json()["messages"] == []
            assert cloud_requests == []
            return
        async with scoped() as db:
            pending = list(await db.scalars(select(LocalAgentChannelMessage).where(
                LocalAgentChannelMessage.source_agent_id == seed["agent_id"],
                LocalAgentChannelMessage.id != uuid.UUID(seed["message_id"]))))
            assert len(pending) == 1 and pending[0].status == "pending"
            message_id, message_hash = str(pending[0].id), pending[0].request_hash
            target_local_session_id = pending[0].session_id
            target_local = await db.get(LocalAgentChannelSession, target_local_session_id)
            target_chat_session_id = target_local.chat_session_id
            if delivery == "same_session":
                assert target_local_session_id == seed["work_id"] and target_chat_session_id == chat_session_id
            else:
                assert target_local_session_id != seed["work_id"] and target_chat_session_id != chat_session_id
                reflection = await db.get(ChatSession, target_chat_session_id)
                assert reflection.listed_surface == "task_updates" and reflection.runtime_task_id == wrapper_id
                assert reflection.session_kind == "trigger_run" and reflection.runtime_source == "trigger"
            assert "ROUTINE_LOCAL_CHECK" in pending[0].content
            assert (await db.get(RuntimeTask, wrapper_id)).status == "completed"
            if narrowing == "reclaim":
                assert pending[0].id == retained_message_id and pending[0].replay_key == retained_replay_key
        async def work_status():
            overview = await owner.get(endpoint + "/autonomy/overview")
            assert overview.status_code == 200, overview.text
            entry = next(row for row in overview.json()["triggers"] if row["id"] == trigger_id)
            return entry["last_attempt"]["work_execution"]

        queued = await work_status()
        assert queued["status"] == "queued" and queued["session_id"] == str(target_chat_session_id)
        assert queued["usage_status"] == "unknown" and queued["budget_scope"] == "platform_dispatch"
        resolved = await owner.get(endpoint + f"/local-agent/sessions/{target_chat_session_id}")
        assert resolved.status_code == 200 and resolved.json()["id"] == str(target_local_session_id), resolved.text
        async def revoke_authority():
            from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority

            async with scoped() as db:
                revoked = await quarantine_transferred_trigger_authority(db, agent_id=seed["agent_id"],
                    previous_owner_id=seed["user_id"])
                assert revoked == [uuid.UUID(trigger_id)]
                await db.commit()

        if narrowing == "pause":
            paused = await owner.patch(endpoint + "/triggers/" + trigger_id, json={"is_enabled": False})
            assert paused.status_code == 200, paused.text
        if narrowing in {"generation", "budget", "lifecycle", "source_changed", "control"}:
            if narrowing == "generation":
                await revoke_authority()
            elif narrowing == "control":
                required = await owner.post("/api/runtime-budgets/policies", json={
                    "name": "Local routines require a real spend limit", "scope_type": "agent_trigger",
                    "agent_id": str(seed["agent_id"]), "trigger_id": trigger_id,
                    "max_background_tasks": 1, "policy_json": {"local_agent_required_controls": ["hard_spend_limit"]}})
                assert required.status_code == 201, required.text
                assert required.json()["policy_json"]["local_agent_required_controls"] == ["hard_spend_limit"]
            else:
                async with scoped() as db:
                    if narrowing == "budget":
                        wrapper = await db.get(RuntimeTask, wrapper_id)
                        budget = await db.get(RuntimeBudgetRun, wrapper.budget_run_id)
                        budget.status = "cancelled"
                    elif narrowing == "lifecycle":
                        (await db.get(Agent, seed["agent_id"])).status = "stopped"
                    else:
                        (await db.get(LocalAgentChannelMessage, uuid.UUID(message_id))).content += " Changed stored bytes."
                    await db.commit()
            blocked = await runner.get("/api/local-bridge/channel/poll")
            assert blocked.status_code == 200 and blocked.json()["messages"] == [], blocked.text
            async with scoped() as db:
                held = await db.get(LocalAgentChannelMessage, uuid.UUID(message_id))
                assert held.status == "rejected" and held.delivery_attempt_count == 0 and held.delivered_at is None
            assert cloud_requests == []
            return
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local-routine.test",
            headers={"Authorization": "Bearer " + other_token}) as other:
            assert (await other.get("/api/local-bridge/channel/poll")).json()["messages"] == []
        delivered = await runner.get("/api/local-bridge/channel/poll")
        assert delivered.status_code == 200, delivered.text
        assert [message["id"] for message in delivered.json()["messages"]] == [message_id]
        dispatched = await work_status()
        assert dispatched["status"] == "delivered" and dispatched["usage_status"] == "unknown"
        if narrowing == "late_report":
            await revoke_authority()
            async with scoped() as db:
                message = await db.get(LocalAgentChannelMessage, uuid.UUID(message_id))
                message.delivery_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            expired = await runner.get("/api/local-bridge/channel/poll")
            assert expired.status_code == 200 and expired.json()["messages"] == [], expired.text
            assert (await work_status())["status"] == "needs_reconciliation"
        progress = await runner.post("/api/local-bridge/channel/events", json={"session_id": str(target_local_session_id),
            "message_id": message_id, "type": "text", "payload": {"text": "Checked the current local record."}})
        assert progress.status_code == 201, progress.text
        reported = await runner.post("/api/local-bridge/channel/report", json={"session_id": str(target_local_session_id),
            "message_id": message_id, "status": "completed", "output": "Stock is above the agreed minimum; no action is needed."})
        assert reported.status_code == 200 and reported.json()["receipt"]["request_hash"] == message_hash, reported.text
        assert (await runner.get("/api/local-bridge/channel/poll")).json()["messages"] == []
        view = await owner.get(endpoint + "/autonomy/overview")
        assert view.status_code == 200, view.text
        entry = next(row for row in view.json()["triggers"] if row["id"] == trigger_id)
        execution = entry["last_attempt"]["work_execution"]
        assert execution["status"] == "completed" and execution["session_id"] == str(target_chat_session_id)
        timeline = await owner.get(f"/api/local-agents/sessions/{target_local_session_id}/timeline")
        assert timeline.status_code == 200, timeline.text
        returned = [event for event in timeline.json()["events"] if event["message_id"] == message_id]
        assert any(event["type"] == "text" and event["payload"]["text"] == "Checked the current local record." for event in returned)
        assert any(event["type"] == "result" and event["payload"]["output"] == "Stock is above the agreed minimum; no action is needed." for event in returned)
        (tmp_path / "local-routine-result.json").write_text(reported.text)
