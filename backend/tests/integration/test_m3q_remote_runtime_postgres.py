"""Explicit runtime selection precedes any remote employee execution."""

import json
import sys
from uuid import UUID, uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration import test_m3o_remote_a2a_tasks_postgres as original
from tests.integration.test_m3o_remote_a2a_tasks_postgres import configure
from tests.services.test_remote_a2a_transport import remote_server

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_completed_remote_employee_history_refuses_an_incompatible_reader_downgrade(
    remote_work, owner_sessionmaker
):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    seed, _api, session_id = remote_work
    path = Path(__file__).resolve().parents[2] / "alembic/versions/remote_a2a_runtime_1020.py"
    spec = importlib.util.spec_from_file_location("remote_runtime_downgrade_contract", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with owner_sessionmaker() as db:
        transaction = await db.begin()
        try:
            # Isolate the history gate from the independent active-selection
            # gate. Roll back every fixture change, including on regression.
            await db.execute(text("UPDATE agents SET agent_type='native'"))
            await db.execute(text("UPDATE runtime_tasks SET metadata_json=metadata_json::jsonb #- '{remote_a2a,dispatch_kind}' WHERE task_type='remote_a2a'"))
            db.add(RuntimeTask(id=uuid4(), tenant_id=seed["tenant"], task_type="remote_a2a", status="completed",
                parent_agent_id=seed["agent"], child_agent_id=seed["agent"], root_user_id=seed["owner"],
                parent_session_id=str(session_id), metadata_json={"remote_a2a": {"dispatch_kind": "employee"}}))
            await db.flush()
            def downgrade(session):
                migration.op = Operations(MigrationContext.configure(session.connection()))
                migration.downgrade()
            with pytest.raises(RuntimeError, match="history"):
                await db.run_sync(downgrade)
            assert await db.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='remote_a2a_connections' AND column_name='runtime_authorization'")) == 1
        finally:
            await transaction.rollback()


def selection(seed, **changes):
    return {"expected_user_id": str(seed["owner"]), "expected_tenant_id": str(seed["tenant"]),
        "expected_version": 1, "expected_runtime_revision": None, "request_id": str(uuid4()),
        "selected_runtime": "remote_a2a", "uncontrolled_effects_acknowledged": True,
        "unknown_cost_acknowledged": True, **changes}


@pytest.mark.parametrize("previous", ["native", "local_agent"])
async def test_explicit_remote_selection_is_idempotent_preserves_native_configuration_and_never_calls_rpc(
    remote_work, app_user_sessionmaker, previous, monkeypatch
):
    seed, api, _session_id = remote_work
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        agent.agent_type = previous
        await db.commit()
        models = (agent.primary_model_id, agent.fallback_model_id)
        assert agent.agent_type == previous
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        assert (await api.get(path)).json()["runtime_activation"] == "not_activated"
        body = selection(seed)
        activated = await api.post(path + "/runtime", json=body)
        assert activated.status_code == 200, activated.text
        assert activated.json()["runtime_activation"] == "activated"
        assert activated.json()["runtime_revision"] == 1
        again = await api.post(path + "/runtime", json=body)
        assert again.status_code == 200 and again.json()["runtime_revision"] == 1
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            assert agent.agent_type == "remote_a2a" and (agent.primary_model_id, agent.fallback_model_id) == models
            from app.services.model_resolution import resolve_agent_runtime_models
            from app.services.web_chat_runtime import start_web_chat_run
            from app.models.chat_session import ChatSession
            from fastapi import HTTPException
            assert (await resolve_agent_runtime_models(db, agent, override_model_id=uuid4())).status == "remote_runtime_selected"
            with pytest.raises(HTTPException) as unavailable:
                await start_web_chat_run(db=db, agent=agent, user=await db.get(User, seed["owner"]),
                    session=await db.get(ChatSession, _session_id), content="Do not invoke a native model")
            assert unavailable.value.status_code == 409 and unavailable.value.detail["code"] == "remote_a2a_entrypoint_unavailable"
        from app.runtime import invoker
        from types import SimpleNamespace
        def no_native_client(**_kwargs):
            raise AssertionError("A selected remote executor must never silently invoke the saved native model")
        monkeypatch.setattr(invoker, "create_llm_client", no_native_client)
        config = await invoker._resolve_runtime_config(seed["agent"])
        assert config.executor_unavailable and config.tenant_resolution_error is None, config
        result = await invoker.invoke_agent(invoker.AgentInvocationRequest(agent_id=seed["agent"], user_id=seed["owner"],
            model=SimpleNamespace(provider="openai", model="fake-native", api_key="synthetic", base_url=None,
                max_output_tokens=None), messages=[{"role": "user", "content": "Do not change executor"}],
            agent_name="Remote employee", role_description="Remote executor"))
        assert result.terminal_reason == "executor_unavailable", result
        from app.services import employee_readiness, runtime_task_worker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        import asyncio
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            ready = await employee_readiness.start_check(db, user=await db.get(User, seed["owner"]),
                agent_id=seed["agent"], team_id=None, request_id=uuid4())
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            claimed = await RuntimeTaskClaimService(db=db, worker_id="remote-readiness",
                task_types=("readiness_probe",)).claim_available()
            task = next(task for task in claimed if task.id == UUID(ready["id"]))
        assert runtime_task_worker._dispatch_claimed_task(task)
        _kind, process = runtime_task_worker._DISPATCHED_TASKS[task.id.hex]
        await asyncio.wait_for(process, 30)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, task.id, populate_existing=True)
            checks = {check["key"]: check for check in task.metadata_json["checks"]}
            assert task.status == "completed" and checks["remote_runtime_authorization"]["status"] == "passed"
            assert checks["model_configuration"]["status"] == "not_applicable"
            assert checks["external_runtime"]["status"] == "unverified"
        assert len(remote.requests) == 1

        restored = await api.post(path + "/runtime", json=selection(seed, selected_runtime="previous",
            expected_runtime_revision=1))
        assert restored.status_code == 200 and restored.json()["runtime_activation"] == "not_activated", restored.text
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            assert agent.agent_type == previous and (agent.primary_model_id, agent.fallback_model_id) == models
        assert len(remote.requests) == 1


@pytest.mark.parametrize("change,status", [
    ({"uncontrolled_effects_acknowledged": False}, 409), ({"unknown_cost_acknowledged": False}, 409),
    ({"required_controls": ["hard_spend_limit"]}, 409), ({"expected_runtime_revision": 1}, 409),
    ({"expected_version": 2}, 409), ({"extra_authority": True}, 422),
    ({"actor_role": "member"}, 403), ({"inactive_tenant": True}, 403),
])
async def test_remote_runtime_selection_requires_current_explicit_administrator_authority(
    remote_work, owner_sessionmaker, app_user_sessionmaker, change, status
):
    from app.models.tenant import Tenant

    seed, api, _session_id = remote_work
    change = dict(change)
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        async with owner_sessionmaker() as db:
            if "actor_role" in change:
                user = await db.get(User, seed["owner"])
                user.role = change.pop("actor_role")
            if change.pop("inactive_tenant", False):
                tenant = await db.get(Tenant, seed["tenant"])
                tenant.is_active = False
            await db.commit()
        response = await api.post(path + "/runtime", json=selection(seed, **change))
        assert response.status_code == status, response.text
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert (await db.get(Agent, seed["agent"])).agent_type == "local_agent"
        assert len(remote.requests) == 1


@pytest.mark.parametrize("flow", ["completed", "unknown", "cancel", "source_changed", "grant_changed", "budget_denied", "budget_stopped", "budget_orphan", "budget_race", "recipient_denied", "recipient_granted", "recipient_revoked", "recipient_retry", "recipient_retry_revoked", "recipient_retry_budget", "recipient_retry_runtime", "source_hex_session", "source_principal_lost", "executor_mismatch", "unselected_remote"])
async def test_governed_employee_delegation_uses_real_remote_worker_and_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, flow
):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app import database
    from app.api import agents
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.kernel.engine import _context_message, _mid_run_items_to_user_messages
    from app.runtime.ccplus_contracts import permission_profile_snapshot
    from app.services import agent_tools, runtime_task_worker, session_model_round
    from app.services.web_chat_runtime import _persist_tool_call
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.session_v2 import SessionCommand
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from datetime import datetime, timezone, timedelta
    import hashlib
    from sqlalchemy import select
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    source = await _seed_run(owner_sessionmaker, tmp_path)
    target_id = uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, source["user_id"])
        user.role = "org_admin"
        parent = await db.get(Agent, source["agent_id"])
        db.add(Agent(id=target_id, tenant_id=source["tenant_id"], creator_id=user.id, owner_user_id=user.id,
            name="Remote employee", primary_model_id=parent.primary_model_id, status="idle"))
        db.add(CapabilityPolicy(tenant_id=source["tenant_id"], capability="agent.message.send", allowed=True,
            requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=source["tenant_id"], capability="agent.async_task.modify", allowed=True,
            requires_approval=False))
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(source["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", source["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async def no_wake(**_kwargs):
        pass
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wake)
    budget_service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    budget = await budget_service.create_run(RuntimeBudgetRunCreate(tenant_id=source["tenant_id"],
        root_run_kind="web_chat_turn", root_run_key=str(source["run_id"]), root_runtime_task_id=source["run_id"],
        root_session_id=str(source["session_id"]), root_agent_id=source["agent_id"], root_user_id=source["user_id"],
        max_delegations=0 if flow == "budget_denied" else 2, max_background_tasks=2))
    async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, source["run_id"])
        run.budget_run_id = budget.id
        await db.commit()
    app = FastAPI()
    app.include_router(agents.router)
    async def scoped_db():
        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
            yield db
    app.dependency_overrides[database.get_db] = scoped_db
    seed = {"agent": target_id, "owner": source["user_id"], "tenant": source["tenant_id"]}
    with remote_server() as remote:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local",
            headers={"Authorization": "Bearer " + create_access_token(str(source["user_id"]), "org_admin", str(source["tenant_id"]))}) as api:
            path = await configure(seed, api, remote)
            if flow != "unselected_remote":
                assert (await api.post(path + "/runtime", json=selection(seed))).status_code == 200
        arguments = {"agent_name": "Remote employee", "target_agent_id": str(target_id), "message": "Return the complete authorized report."}
        if flow != "source_hex_session":
            arguments["execution_target"] = "local_agent" if flow == "executor_mismatch" else "remote_a2a"
        from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section
        collaborators = build_a2a_collaborators_section({"same_owner_agents": [{"id": str(target_id),
            "name": "Remote employee", "agent_type": "native" if flow == "unselected_remote" else "remote_a2a"}]})
        assert ("executor=remote_a2a" in collaborators) is (flow != "unselected_remote")
        document_id = uuid4()
        private_body = "SYNTHETIC PRIVATE CUSTOMER REPORT"
        document_hash = hashlib.sha256(private_body.encode()).hexdigest()
        private_source = flow.startswith("recipient_")
        if private_source:
            arguments["message"] += " " + private_body
            private_path = source["data_root"] / "private-fixture.md"
            private_path.write_text(private_body)
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                db.add(KnowledgeDocument(id=document_id, tenant_id=source["tenant_id"], scope_type="person",
                    scope_id=source["user_id"], owner_user_id=source["user_id"], source_kind="text",
                    source_sha256=document_hash, title="Private fixture", status="ready", sensitivity="PL2_pii",
                    canonical_md_path=str(private_path), canonical_md_sha256=document_hash,
                    created_by_user_id=source["user_id"]))
                await db.commit()
        scope = {"tenant_id": source["tenant_id"], "agent_id": source["agent_id"], "session_id": source["session_id"],
            "run_id": source["run_id"], "turn_id": source["turn_id"], "round_index": 1}
        call_id = "remote-employee-delegation"
        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
            inputs = await session_model_round.bind_round_inputs(db, **scope)
            messages = [_context_message(role="system", content="Use the authorized tools.", kind="platform_guidance", unbound=()),
                *_mid_run_items_to_user_messages(inputs)]
            if private_source:
                messages.append(_context_message(role="system", content=private_body, kind="retrieved_context",
                    sources=[{"source_kind": "knowledge_document", "source_id": str(document_id),
                        "source_version": document_hash}], unbound=()))
            provider_id = await session_model_round.prepare_model_request(db, **scope, messages=messages,
                tools=[], provider="openai", model="fake-4.1")
            await session_model_round.seal_model_response(db, **scope, provider_request_id=provider_id,
                response={"content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": "delegate_to_agent", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"})
            await session_model_round.commit_sealed_model_round(db, **scope, provider_request_id=provider_id)
            await db.commit()
        event = {"name": "delegate_to_agent", "args": arguments, "tool_call_id": call_id,
            "provider_request_id": provider_id, "runtime_task_id": str(source["run_id"])}
        await _persist_tool_call(agent_id=source["agent_id"], user_id=source["user_id"], session_id=str(source["session_id"]),
            data={**event, "status": "running"})
        async def pre_effect(_payload):
            await _persist_tool_call(agent_id=source["agent_id"], user_id=source["user_id"], session_id=str(source["session_id"]),
                data={**event, "status": "effect_started"})
        if flow in {"budget_orphan", "budget_race"}:
            reserve = RuntimeBudgetService.reserve
            async def interrupted_handoff(service, reservation):
                decision = await reserve(service, reservation)
                if flow == "budget_orphan":
                    from app.services.runtime_budget_service import RuntimeBudgetSettlement
                    await service.settle(RuntimeBudgetSettlement(budget_run_id=reservation.budget_run_id,
                        reservation_key=reservation.reservation_key, runtime_task_id=reservation.runtime_task_id,
                        reason="Reconciled missing task before a late producer resumed"))
                else:
                    await service.cancel_run(tenant_id=source["tenant_id"], budget_run_id=reservation.budget_run_id,
                        reason="Root stopped between reservation and task persistence", actor_user_id=source["user_id"])
                return decision
            monkeypatch.setattr(RuntimeBudgetService, "reserve", interrupted_handoff)
        result = await agent_tools.execute_tool("delegate_to_agent", arguments, agent_id=source["agent_id"],
            user_id=source["user_id"], session_id=source["session_id"].hex if flow == "source_hex_session" else str(source["session_id"]),
            runtime_task_id=str(source["run_id"]),
            turn_id=source["turn_id"], tool_call_id=call_id, pre_effect_callback=pre_effect,
            permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)
        if flow in {"budget_denied", "budget_orphan", "budget_race", "executor_mismatch", "unselected_remote"}:
            expected = "runtime_budget_denied" if flow == "budget_denied" else "remote_a2a_selected_runtime_mismatch" if flow in {"executor_mismatch", "unselected_remote"} else "remote_a2a_budget_reservation_unavailable"
            assert expected in str(result), str(result)
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                assert not list(await db.scalars(select(RuntimeTask).where(RuntimeTask.task_type == "remote_a2a")))
            assert len(remote.requests) == 1
            return
        assert str(result).lstrip().startswith("{"), str(result)
        returned = json.loads(str(result))
        assert returned.get("execution_target") == "remote_a2a", returned
        assert "task_id" in returned, json.dumps(returned)
        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(returned["task_id"]))
            assert task.task_type == "remote_a2a" and task.parent_agent_id == source["agent_id"]
            assert task.root_user_id == source["user_id"] and task.parent_session_id == str(source["session_id"])
            assert task.root_runtime_task_id == source["run_id"]
            assert task.metadata_json["remote_a2a"]["instruction_origin"]
            assert task.budget_run_id == budget.id and task.budget_admission_status == "reserved"
            if flow in {"recipient_granted", "recipient_revoked"}:
                from app.models.chat_session import ChatSession
                child = await db.get(ChatSession, UUID(task.child_session_id))
                assert child.agent_id == target_id and child.runtime_task_id == task.id
                db.add(KnowledgeGrant(tenant_id=source["tenant_id"], scope_type="person", scope_id=source["user_id"],
                    resource_type="document", resource_id=document_id, document_id=document_id,
                    grantee_type="agent", grantee_id=target_id, permission="read", requester_user_id=source["user_id"],
                    session_id=task.child_session_id, purpose="a2a_delegation", delegation_id=str(task.id),
                    sensitivity_ceiling="PL2_pii", created_by_user_id=source["user_id"],
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    revoked_at=datetime.now(timezone.utc) if flow == "recipient_revoked" else None))
                await db.commit()
        assert len(remote.requests) == 1
        async def tool(name, args):
            value = await agent_tools.execute_tool(name, args, agent_id=source["agent_id"], user_id=source["user_id"],
                session_id=str(source["session_id"]), runtime_task_id=str(source["run_id"]), turn_id=source["turn_id"],
                permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)
            assert str(value).lstrip().startswith("{"), str(value)
            return json.loads(str(value))
        checked = await tool("check_async_task", {"task_id": returned["task_id"]})
        assert checked["execution_target"] == "remote_a2a" and checked["status"] == "pending", checked
        text = "EXACT REMOTE REPORT\n" * 1500
        if flow == "grant_changed":
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                target = await db.get(Agent, target_id)
                target.agent_type = "native"
                await db.commit()
        if flow == "source_principal_lost":
            from copy import deepcopy
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(returned["task_id"]))
                metadata = deepcopy(task.metadata_json)
                metadata["remote_a2a"]["source_read_principal"].pop("purpose")
                task.metadata_json = metadata
                await db.commit()
        remote.reply = lambda request: ({"task": {"id": "employee-report", "status": {
            "state": "TASK_STATE_WORKING" if flow in {"cancel", "budget_stopped"} else "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "report", "parts": [{"filename": "report.txt", "text": text}]}]}}
            if request["method"] == "SendMessage" else {"id": "employee-report", "status": {"state": "TASK_STATE_CANCELED"}})
        if flow == "unknown":
            from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
            rpc = RemoteA2AEndpoint.rpc
            async def lost_reply(endpoint, *args, **kwargs):
                await rpc(endpoint, *args, **kwargs)
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lost_reply)
        await original.execute(seed, returned["task_id"], app_user_sessionmaker)
        checked = await tool("check_async_task", {"task_id": returned["task_id"]})
        assert checked["result_refs"] and "observation" not in checked, checked
        if flow == "source_principal_lost":
            assert checked["status"] == "needs_reconciliation" and len(remote.requests) == 1, checked
            page = await tool("read_runtime_result", {"result_ref": checked["result_refs"][0]})
            assert page["status"] in {"source_access_denied", "source_unavailable"} and "content" not in page, page
            return
        assert checked["status"] == ("needs_reconciliation" if (flow in {"unknown", "grant_changed", "recipient_denied", "recipient_revoked"} or flow.startswith("recipient_retry"))
            else "suspended" if flow in {"cancel", "budget_stopped"} else "completed"), checked
        page = await tool("read_runtime_result", {"result_ref": checked["result_refs"][0], "limit": 12000})
        assert page["status"] == "ok" and page["dependency_coverage"]["complete"] is True, page
        assert page["remote_internal_source_coverage"] == "unknown" and page["remote_business_acceptance"] == "unverified"
        if (flow in {"unknown", "grant_changed", "recipient_denied", "recipient_revoked"} or flow.startswith("recipient_retry")):
            assert page["status"] == "ok" and checked["status"] == "needs_reconciliation"
            assert len(remote.requests) == (2 if flow == "unknown" else 1)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local",
                headers={"Authorization": "Bearer " + create_access_token(str(source["user_id"]), "org_admin", str(source["tenant_id"]))}) as api:
                retry = {"expected_user_id": str(source["user_id"]), "expected_tenant_id": str(source["tenant_id"]),
                    "expected_version": 1, "operation_id": str(uuid4()), "action": "retry"}
                retry_path = path + "/tasks/" + returned["task_id"] + "/operations"
                if flow == "unknown":
                    assert "retry" not in checked["recovery_actions"]
                    refused = await api.post(retry_path, json=retry)
                    assert refused.status_code == 409, refused.text
                    assert len(remote.requests) == 2
                if flow.startswith("recipient_retry"):
                    assert "retry" in checked["recovery_actions"]
                    async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                        task = await db.get(RuntimeTask, UUID(returned["task_id"]))
                        child_session_id = task.child_session_id
                        grant = KnowledgeGrant(tenant_id=source["tenant_id"], scope_type="person", scope_id=source["user_id"],
                            resource_type="document", resource_id=document_id, document_id=document_id,
                            grantee_type="agent", grantee_id=target_id, permission="read", requester_user_id=source["user_id"],
                            session_id=child_session_id, purpose="a2a_delegation", delegation_id=str(task.id),
                            sensitivity_ceiling="PL2_pii", created_by_user_id=source["user_id"],
                            expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
                        db.add(grant)
                        await db.commit()
                        grant_id = grant.id
                    if flow == "recipient_retry_budget":
                        await budget_service.cancel_run(tenant_id=source["tenant_id"], budget_run_id=budget.id,
                            reason="Stop before retry", actor_user_id=source["user_id"])
                    if flow == "recipient_retry_runtime":
                        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                            (await db.get(Agent, target_id)).agent_type = "native"
                            await db.commit()
                    queued = await api.post(retry_path, json=retry)
                    if flow in {"recipient_retry_budget", "recipient_retry_runtime"}:
                        assert queued.status_code == 409 and len(remote.requests) == 1, queued.text
                        return
                    assert queued.status_code == 200 and queued.json()["id"] == returned["task_id"], queued.text
                    assert queued.json()["child_session_id"] == child_session_id
                    assert (await api.post(retry_path, json=retry)).json()["status"] == "pending"
                    if flow == "recipient_retry_revoked":
                        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                            (await db.get(KnowledgeGrant, grant_id)).revoked_at = datetime.now(timezone.utc)
                            await db.commit()
                    await original.execute(seed, returned["task_id"], app_user_sessionmaker)
                    repaired = await tool("check_async_task", {"task_id": returned["task_id"]})
                    assert repaired["status"] == ("needs_reconciliation" if flow == "recipient_retry_revoked" else "completed"), repaired
                    assert len(remote.requests) == (1 if flow == "recipient_retry_revoked" else 2)
                    if flow == "recipient_retry":
                        assert remote.requests[-1][3]["params"]["message"]["parts"][0]["text"] == arguments["message"]
                        await budget_service.reconcile_orphaned_reservations()
                        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                            used = await db.get(RuntimeBudgetRun, budget.id)
                            assert used.used_delegations == used.used_background_tasks == 1
            return
        if private_source:
            assert remote.requests[-1][3]["params"]["message"]["parts"][0]["text"] == arguments["message"]
        payload = page["content"]
        while not page["complete"]:
            page = await tool("read_runtime_result", {"result_ref": checked["result_refs"][0], "limit": 12000,
                "offset": page["next_offset"], "expected_sha256": page["sha256"]})
            assert page["status"] == "ok", page
            payload += page["content"]
        saved = json.loads(json.loads(payload)["summary"])["remote_a2a"]
        assert saved["operation_observation"]["task"]["artifacts"][0]["parts"][0]["text"] == text
        if flow == "source_changed":
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == source["session_id"],
                    SessionCommand.namespace == "human_input"))
                command.request_json = {**command.request_json, "content_parts": [{"type": "text", "text": "Changed input"}]}
                await db.commit()
            denied = await tool("read_runtime_result", {"result_ref": checked["result_refs"][0]})
            assert denied["status"] in {"source_access_denied", "source_unavailable"}, denied
            assert "content" not in denied
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local",
                headers={"Authorization": "Bearer " + create_access_token(str(source["user_id"]), "org_admin", str(source["tenant_id"]))}) as api:
                response = await api.get(path + "/tasks/" + returned["task_id"])
                assert response.status_code == 200 and response.json()["content_status"] == "source_unavailable", response.text
                assert "observation" not in response.json() and "operations" not in response.json()
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                from app.services.remote_a2a_artifacts import import_packet
                from app.services.content_input_receipts import ContentInputReceiptError
                from app.models.runtime_result import RuntimeResultObject
                record = await db.get(RuntimeResultObject, UUID(checked["result_refs"][0].split("/")[2]))
                with pytest.raises(ContentInputReceiptError):
                    await import_packet(db, tenant_id=source["tenant_id"], result_id=record.id, sha256=record.sha256,
                        user=await db.get(User, source["user_id"]))
            return
        if flow == "budget_stopped":
            await budget_service.cancel_run(tenant_id=source["tenant_id"], budget_run_id=budget.id,
                reason="Stop new remote work without claiming external cancellation", actor_user_id=source["user_id"])
            stopped = await tool("check_async_task", {"task_id": returned["task_id"]})
            assert stopped["status"] == "needs_reconciliation" and stopped["remote_state"] == "TASK_STATE_WORKING", stopped
            assert "cancel" in stopped["recovery_actions"] and len(remote.requests) == 2
        if flow in {"cancel", "budget_stopped"}:
            cancelled = await tool("cancel_async_task", {"task_id": returned["task_id"]})
            assert cancelled["status"] == "pending", cancelled
            await original.execute(seed, returned["task_id"], app_user_sessionmaker)
            checked = await tool("check_async_task", {"task_id": returned["task_id"]})
            assert checked["status"] == "killed" and len(remote.requests) == 3, checked
        else:
            delivery = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id="employee-return")
            async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
                delivered = list(await db.scalars(select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.tenant_id == source["tenant_id"],
                    RuntimeNotificationOutbox.parent_session_id == source["session_id"],
                    RuntimeNotificationOutbox.source_kind == "remote_a2a",
                    RuntimeNotificationOutbox.source_run_id.startswith(returned["task_id"] + ":"),
                    RuntimeNotificationOutbox.status == "delivered")))
                assert len(delivered) == 1, delivery
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == UUID(returned["task_id"])))
                assert artifact is not None and artifact.agent_id == source["agent_id"] and artifact.session_id == source["session_id"]
                assert (source["data_root"] / str(source["agent_id"]) / artifact.path).read_text() == text
                followups = await session_model_round.bind_round_inputs(db, **{**scope, "round_index": 2})
                followup_messages = _mid_run_items_to_user_messages(followups)
                assert checked["result_refs"][0] in "\n".join(message.content for message in followup_messages), followup_messages
                parent = await db.get(RuntimeTask, source["run_id"])
                assert parent.status == "running" and parent.child_agent_id == source["agent_id"]
                await db.commit()
            assert len(remote.requests) == 2
        await budget_service.reconcile_orphaned_reservations()
        async with tenant_scoped_session(source["tenant_id"], session_factory=app_user_sessionmaker) as db:
            used = await db.get(RuntimeBudgetRun, budget.id)
            assert used.used_delegations == used.used_background_tasks == 1
