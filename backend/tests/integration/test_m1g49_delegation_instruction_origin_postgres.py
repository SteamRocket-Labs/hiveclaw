"""Trace a delegated instruction to its actual canonical parent tool call."""

import json
from contextlib import asynccontextmanager
import platform
import sys
from uuid import uuid4, uuid5

import pytest

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS context gate; Linux delivery is deferred.")]


async def _start_feishu_creation_work(seed, app_user_sessionmaker, monkeypatch):
    from sqlalchemy import select
    from app.api import feishu
    from app.database import pin_rls_tenant_context
    from app.models.channel_config import ChannelConfig
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, ChannelIngressInboxService
    from app.services.channel_provider_receipts import feishu_message_scope
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.direct_employee_creation import _human_request
    from app.services.hr_creation_service import HrCreationConflict
    from app.models.external_principal import ExternalPrincipal

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        prior = await db.get(RuntimeTask, seed["run_id"])
        prior.status = "completed"
        config = ChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], channel_type="feishu",
            app_id="synthetic-creation-app", app_secret="synthetic-creation-secret", is_configured=True)
        db.add(config)
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
            provider_subject_id="ou_creation_owner", user_id=seed["user_id"], actor_user_id=seed["user_id"])
        principal_id, installation = bound.principal.id, str(config.id)
        await db.commit()

    async def profile(_agent_id, **_kwargs):
        return {"open_id": "ou_creation_owner", "name": "Creation Owner"}

    async def send(*_args, **_kwargs):
        return {"data": {"message_id": "synthetic-creation-message"}}

    async def no_card(*_args, **_kwargs):
        raise RuntimeError("Synthetic CardKit unavailable; use the existing message fallback")

    monkeypatch.setattr(feishu, "_resolve_feishu_sender_profile", profile)
    monkeypatch.setattr(feishu.feishu_service, "send_message", send)
    monkeypatch.setattr(feishu.feishu_service, "create_card_entity", no_card)
    monkeypatch.setattr(feishu.feishu_service, "patch_message", send)
    body = {"header": {"event_type": "im.message.receive_v1", "event_id": "synthetic-create-event"},
        "event": {"sender": {"sender_id": {"open_id": "ou_creation_owner"}},
            "message": {"message_type": "text", "chat_type": "p2p", "message_id": "synthetic-create-message",
                "content": json.dumps({"text": "Create a research employee and request the authorized report."})}}}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        receipt = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=installation, provider_event_id="synthetic-create-event",
            handler_key="feishu.event", body=body, metadata={"feishu_message_scope": feishu_message_scope(config)})
    stats = await ChannelIngressInboxService(session_factory=app_user_sessionmaker).drain_once(worker_id="creation-ingress")
    assert stats["processed"] == 1 and stats["retried"] == 0, stats
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        ingress = await db.get(ChannelIngressEvent, receipt.event_id)
        assert ingress.external_principal_id == principal_id, ingress.processing_receipt_json
        row = await db.get(SessionTurnInput, receipt.event_id)
        # The bounded live fast path may defer; drive the real durable dispatcher too.
        await recover_admitted_session_inputs_once(db, worker_id="creation-ingress-dispatch", tenant_id=seed["tenant_id"],
            admission_ids=(uuid5(row.command_id, "input-admission:revision:1"),))
        session = await db.get(ChatSession, row.session_id)
        run = await db.scalar(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.parent_session_id == str(session.id),
            RuntimeTask.metadata_json["session_v2_input_id"].astext == str(row.id)))
        assert run is not None
        command = await db.get(SessionCommand, row.command_id)
        assert session.external_principal_id == principal_id and session.actor_type == "external_principal"
        assert command.principal_type == "external_principal" and command.principal_id == principal_id
        assert session.user_id == run.root_user_id == seed["user_id"]
        principal = await db.get(ExternalPrincipal, principal_id)
        for field, changed in (("status", "revoked"), ("linked_user_id", None)):
            before = getattr(principal, field)
            setattr(principal, field, changed)
            await db.flush()
            with pytest.raises(HrCreationConflict) as denied:
                await _human_request(db, tenant_id=seed["tenant_id"], user_id=seed["user_id"],
                    agent_id=seed["agent_id"], session_id=session.id, run_id=run.id, input_id=row.id)
            assert denied.value.code == "source_execute_denied"
            setattr(principal, field, before)
            await db.flush()
        ingress.installation_ref = "another-installation"
        await db.flush()
        with pytest.raises(HrCreationConflict) as wrong_installation:
            await _human_request(db, tenant_id=seed["tenant_id"], user_id=seed["user_id"],
                agent_id=seed["agent_id"], session_id=session.id, run_id=run.id, input_id=row.id)
        assert wrong_installation.value.code == "direct_human_input_required"
        ingress.installation_ref = installation
        await db.commit()
        seed.update(run_id=run.id, session_id=session.id, turn_id=run.metadata_json["turn_id"],
            feishu_principal_id=principal_id, feishu_ingress_id=ingress.id)
        return command.id


@pytest.mark.parametrize("input_case", ["first_task_child_missing_file", "first_task_parent_missing_file", "first_task_parent_legacy_workspace_read", "complete", "unbound", "revoked", "first_task", "first_task_unbound", "first_task_revoked", "first_task_refined", "first_task_delivery", "first_task_parent_model", "first_task_refined_native", "first_task_native_creation", "first_task_fresh_employee", "first_task_slow_provider", "first_task_saved_reopen", "first_task_question", "first_task_question_after_write", "first_task_feishu_question", "first_task_feishu_quoted_question", "first_task_feishu_question_source_changed", "first_task_feishu_question_body_changed", "first_task_feishu_question_already_answered", "first_task_feishu_question_projection_missing", "first_task_feishu_question_recipient_changed", "first_task_feishu_question_legacy_status", "first_task_feishu_permission_allow", "first_task_feishu_permission_deny", "first_task_feishu_permission_cancel", "first_task_feishu_permission_unknown", "first_task_feishu_permission_expired", "first_task_feishu_permission_source_changed", "delegation_permission_allow", "first_task_recall", "first_task_ledger", "first_task_ledger_unknown", "first_task_parent_status", "first_task_parent_status_unknown", "first_task_child_command", "first_task_child_code", "first_task_child_command_then_write", "first_task_child_code_then_write", "first_task_creator_task_inventory", "first_task_creator_readiness_feedback", "first_task_creator_readiness_resume", "first_task_parent_creation_result", "first_task_parent_historical_creation_result", "first_task_parent_historical_creation_unknown", "first_task_parent_result_denial", "first_task_parent_public_directory", "first_task_child_readback", "first_task_child_glob", "first_task_workspace_listing", "first_task_workspace_listing_alias", "first_task_completion_sweep", "first_task_parent_wait_legacy", "first_task_parent_code_legacy", "first_task_parity_list", "first_task_parity_ledger", "first_task_parity_ledger_unknown", "first_task_context", "first_task_discovery", "first_task_unverified_memory", "first_task_explicit_unverified_memory", "first_task_feishu_permission_channel_allow", "first_task_feishu_permission_channel_deny", "first_task_feishu_permission_channel_unknown", "first_task_feishu_permission_channel_legacy", "first_task_feishu_permission_channel_scope", "first_task_feishu_permission_channel_feedback_retry", "first_task_feishu_progress"])
async def test_governed_delegation_preserves_its_parent_instruction_origin(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, input_case, migrated_pg_url,
):
    import app.database as database
    from app.agents import orchestrator
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.services.agent_manager import AgentManager
    from app.runtime.ccplus_contracts import permission_profile_snapshot
    from app.services import agent_tools, session_model_round
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import _persist_tool_call

    parent_missing_file_case = input_case == "first_task_parent_missing_file"
    child_missing_file_case = input_case == "first_task_child_missing_file"
    legacy_workspace_read_case = input_case == "first_task_parent_legacy_workspace_read"
    glob_case = input_case == "first_task_child_glob"
    creator_resume_case = input_case == "first_task_creator_readiness_resume"
    creator_readiness_case = creator_resume_case or input_case == "first_task_creator_readiness_feedback"
    creator_task_inventory_case = glob_case or creator_readiness_case or input_case == "first_task_creator_task_inventory"
    sequential_child_case = creator_task_inventory_case or input_case in {"first_task_child_command_then_write", "first_task_child_code_then_write"}
    sequential_code_case = creator_task_inventory_case or input_case == "first_task_child_code_then_write"
    completion_sweep_case = input_case == "first_task_completion_sweep"
    legacy_code_case = input_case == "first_task_parent_code_legacy"
    legacy_wait_case = legacy_code_case or input_case == "first_task_parent_wait_legacy"
    workspace_listing_case = legacy_workspace_read_case or legacy_wait_case or input_case in {"first_task_workspace_listing", "first_task_workspace_listing_alias"}
    listing_tool = "fs_list" if input_case == "first_task_workspace_listing_alias" else "list_files"
    parity_list_case = input_case == "first_task_parity_list"
    parity_sequence_case = input_case in {"first_task_parity_ledger", "first_task_parity_ledger_unknown"}
    parity_unknown = input_case == "first_task_parity_ledger_unknown"
    child_readback_case = child_missing_file_case or parent_missing_file_case or creator_task_inventory_case or sequential_child_case or completion_sweep_case or workspace_listing_case or input_case in {"first_task_child_readback", "first_task_parity_list", "first_task_parity_ledger"}
    if legacy_wait_case:
        import platform

        if platform.system() != "Darwin":
            pytest.skip("Native macOS process gate; Linux delivery is deferred.")
        assert platform.system() == "Darwin"
        for key, value in {"HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox", "HIVE_CODE_SANDBOX_MODE": "auto",
            "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0"}.items():
            monkeypatch.setenv(key, value)
    if child_readback_case:
        input_case = "first_task_parent_status"
    if parity_unknown:
        input_case = "first_task_ledger_unknown"
    public_directory_case = input_case == "first_task_parent_public_directory"
    if public_directory_case:
        input_case = "first_task_parent_status"
    result_denial_case = input_case == "first_task_parent_result_denial"
    if result_denial_case:
        input_case = "first_task_child_command"
    historical_creation_case = input_case in {"first_task_parent_historical_creation_result", "first_task_parent_historical_creation_unknown"}
    historical_creation_unknown = input_case == "first_task_parent_historical_creation_unknown"
    creation_result_case = input_case == "first_task_parent_creation_result" or historical_creation_case
    if creation_result_case:
        input_case = "first_task_parent_status"
    child_command_case = sequential_child_case or input_case in {"first_task_child_command", "first_task_child_code"}
    child_tool = "execute_code" if sequential_code_case or input_case == "first_task_child_code" else "run_command"
    if child_command_case:
        import platform

        if platform.system() != "Darwin":
            pytest.skip("Native macOS process gate; Linux delivery is deferred.")
        assert platform.system() == "Darwin"
        for key, value in {"HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox", "HIVE_CODE_SANDBOX_MODE": "auto",
            "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0"}.items():
            monkeypatch.setenv(key, value)
        input_case = "first_task_parent_status"
    parent_status_unknown = input_case == "first_task_parent_status_unknown"
    parent_status_case = input_case in {"first_task_parent_status", "first_task_parent_status_unknown"}
    if parent_status_case:
        input_case = "first_task_ledger"
    slow_first_task = input_case == "first_task_slow_provider"
    if slow_first_task:
        input_case = "first_task_fresh_employee"
    first_task = input_case.startswith("first_task")
    progress_case = input_case == "first_task_feishu_progress"
    permission_case = input_case.startswith("first_task_feishu_permission_") or input_case == "delegation_permission_allow"
    question_after_write = input_case == "first_task_question_after_write"
    feishu_creation = input_case.startswith("first_task_feishu_")
    if feishu_creation:
        monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "https://hive.synthetic.test")
    saved_reopen = input_case == "first_task_saved_reopen"
    memory_case = input_case in {"first_task_unverified_memory", "first_task_explicit_unverified_memory"}
    context_case = input_case == "first_task_context"
    recall_case = input_case == "first_task_recall"
    ledger_unknown_case = input_case == "first_task_ledger_unknown"
    ledger_case = input_case in {"first_task_ledger", "first_task_ledger_unknown"}
    discovery_case = input_case in {"first_task_discovery", "first_task_context", "first_task_recall", "first_task_ledger", "first_task_ledger_unknown"}
    native_creation = feishu_creation or input_case == "delegation_permission_allow" or input_case in {"first_task_native_creation", "first_task_fresh_employee", "first_task_saved_reopen", "first_task_question", "first_task_question_after_write"}
    parent_model_case = feishu_creation or input_case in {"first_task_parent_model", "first_task_refined_native", "first_task_native_creation", "first_task_fresh_employee", "first_task_saved_reopen", "first_task_question", "first_task_question_after_write"}
    delivery_case = input_case == "first_task_delivery" or parent_model_case
    native_refined = feishu_creation or input_case in {"first_task_refined_native", "first_task_native_creation", "first_task_fresh_employee", "first_task_saved_reopen", "first_task_question", "first_task_question_after_write"}
    native_creation = native_creation or memory_case or discovery_case
    parent_model_case = parent_model_case or memory_case or discovery_case
    delivery_case = delivery_case or memory_case or discovery_case
    native_refined = native_refined or memory_case or discovery_case
    refined_input = input_case == "first_task_refined"
    unbound_input = input_case in {"unbound", "first_task_unbound", "first_task_explicit_unverified_memory"} or ledger_unknown_case
    revoked_input = input_case in {"revoked", "first_task_revoked"}
    parent_complete = not unbound_input
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt=(
        "Create a research employee and have it produce the authorized report." if first_task
        else "Delegate the authorized report to the worker."))
    receiver = uuid4()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(db, parent, personality="NATIVE_PARENT_CONTEXT",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        if first_task:
            from app.models.tenant_setting import TenantSetting

            db.add(TenantSetting(tenant_id=seed["tenant_id"], key="default_model_id", value={"model_id": str(model.id)}))
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.employee.create", allowed=True,
                requires_approval=False))
        else:
            db.add(Agent(id=receiver, tenant_id=seed["tenant_id"], name="Origin worker", creator_id=seed["user_id"],
                owner_user_id=seed["user_id"], primary_model_id=model.id))
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.message.send", allowed=True,
            requires_approval=False))
        if public_directory_case:
            from app.models.agent import AgentPermission
            from app.models.user import User

            peer_owner, peer_id, public_permission_id = uuid4(), uuid4(), uuid4()
            db.add(User(id=peer_owner, tenant_id=seed["tenant_id"], username=f"directory-{peer_owner.hex}",
                email=f"{peer_owner.hex}@directory.test", password_hash="synthetic", display_name="Directory peer owner"))
            await db.flush()
            db.add(Agent(id=peer_id, tenant_id=seed["tenant_id"], name="Public synthetic teammate",
                creator_id=peer_owner, owner_user_id=peer_owner, primary_model_id=model.id))
            await db.flush()
            db.add(AgentPermission(id=public_permission_id, tenant_id=seed["tenant_id"], agent_id=peer_id,
                scope_type="company", access_level="use"))
        if child_command_case or legacy_wait_case:
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"],
                capability="workspace.code.execute" if legacy_code_case or child_tool == "execute_code" else "workspace.command.execute",
                allowed=True, requires_approval=False))
        if ledger_case:
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.task.track", allowed=True,
                requires_approval=False))
        if glob_case or legacy_workspace_read_case:
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.read", allowed=True,
                requires_approval=False))
        if not permission_case:
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write", allowed=True,
                requires_approval=False))
        if context_case:
            peer = Agent(id=uuid4(), tenant_id=seed["tenant_id"], name="Existing local teammate",
                role_description="Writes synthetic reports", creator_id=seed["user_id"],
                owner_user_id=seed["user_id"], primary_model_id=model.id)
            db.add(peer)
            await db.flush()
            await AgentManager().initialize_agent_files(db, peer, personality="SYNTHETIC_PEER_CONTEXT",
                original_author_id=seed["user_id"])
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    # Bind the authority reader to this fixture's application-role database.
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from uuid import UUID
    from sqlalchemy import select
    import app.runtime.invoker as invoker
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionModelResult
    from app.services import agent_context, agent_manager
    from app.services.delegation_input_origin import load_delegation_instruction_origin
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    if first_task:
        from app.models.chat_session import ChatSession
        from app.models.runtime_task import RuntimeTask
        from app.models.session_v2 import SessionCommand, SessionTurnInput, SessionInputAdmission
        from app.models.user import User
        from app.services.session_input_admission import run_user_prompt_admission
        from app.services.session_input_dispatch import _start_input_runtime
        from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            prior = await db.get(RuntimeTask, seed["run_id"])
            prior.status = "completed"
            user = await db.get(User, seed["user_id"])
            authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed["agent_id"],
                session_id=seed["session_id"], action="mutate_session_input")
            input_id = uuid4()
            await accept_human_input(db, authority=authority, intent={"kind": "start_turn", "input_id": str(input_id),
                "idempotency_key": str(input_id), "session_id": str(seed["session_id"]),
                "content_parts": [{"type": "text", "text": "Create a research employee and request the authorized report." if first_task
                    else "Delegate the authorized report to the worker."}],
                "runtime_metadata": {"source": "session_input_api"}})
            await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="creation-origin-admission")
            row = await db.get(SessionTurnInput, input_id)
            creation_command_id = row.command_id
            started = await _start_input_runtime(db, row=row, command=await db.get(SessionCommand, row.command_id),
                agent=await db.get(Agent, seed["agent_id"]), user=user,
                session=await db.get(ChatSession, seed["session_id"]), successor=False)
            admission = await db.get(SessionInputAdmission, uuid5(row.command_id, "input-admission:revision:1"))
            admission.dispatch_state, admission.dispatch_receipt_json = "dispatched", started
            from uuid import UUID

            seed["run_id"], seed["turn_id"] = UUID(started["run_id"]), started["turn_id"]
            run = await db.get(RuntimeTask, seed["run_id"])
            run.status = "running"
            await db.commit()
    if input_case == 'delegation_permission_allow':
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api.chat_sessions import router
        from app.core.security import get_current_user
        from app.database import get_db
        from app.models.chat_session import ChatSession
        from app.models.user import User
        from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
        from app.services.session_input_dispatch import recover_admitted_session_inputs_once

        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
            user = await db.get(User, seed["user_id"])
            # Start the actual API journey in a fresh work session; the seed's
            # synthetic running turn has no terminal boundary to acknowledge.
            seed["session_id"] = uuid4()
            db.add(ChatSession(id=seed["session_id"], agent_id=seed["agent_id"],
                tenant_id=seed["tenant_id"], user_id=user.id, source_channel="web"))
            await db.commit()
        async def original_work_db():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                yield db
        original_app = FastAPI()
        original_app.include_router(router, prefix="/api")
        original_app.dependency_overrides[get_db] = original_work_db
        original_app.dependency_overrides[get_current_user] = lambda: user
        original_input = uuid4()
        async with AsyncClient(transport=ASGITransport(app=original_app), base_url="http://original.local") as api:
            profile = await api.patch(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/permissions/profile",
                json={"permission_mode": "default"})
            assert profile.status_code == 200, profile.text
            admitted = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/inputs", json={
                "kind": "start_turn", "input_id": str(original_input), "idempotency_key": str(original_input),
                "content_parts": [{"type": "text", "text": "Delegate the authorized report to the worker."}]})
            assert admitted.status_code == 200 and admitted.json()["admission_state"] == "admitted", admitted.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            row = await db.get(SessionTurnInput, original_input)
            admission_id = uuid5(row.command_id, "input-admission:revision:1")
            await recover_admitted_session_inputs_once(db, tenant_id=seed["tenant_id"],
                worker_id="original-work-admission", admission_ids=(admission_id,))
            admission = await db.get(SessionInputAdmission, admission_id, populate_existing=True)
            assert admission.dispatch_state == "dispatched", admission.dispatch_receipt_json
            seed["run_id"] = UUID(admission.dispatch_receipt_json["run_id"])
            seed["turn_id"] = admission.dispatch_receipt_json["turn_id"]
    if feishu_creation:
        creation_command_id = await _start_feishu_creation_work(seed, app_user_sessionmaker, monkeypatch)
    if memory_case:
        from app.memory.retriever import MemoryRetriever
        from app.models.chat_session import ChatSession
        from tests.memory.test_retrieval_pipeline import _write_t2_package

        prior_session = uuid4()
        async with owner_sessionmaker() as db:
            db.add(ChatSession(id=prior_session, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                user_id=seed["user_id"], audience_kind="private"))
            await db.commit()
        _write_t2_package(seed["data_root"], seed["agent_id"], session_id=str(prior_session),
            summary="OLD_UNVERIFIED_CREATION_MEMORY")
        old_memory_path = f"memory/t2/sessions/{prior_session}/segments/seg-1"
        async def select_memory(_self, *, items, **_kwargs):
            return [item.metadata["selection_candidate_id"] for item in items], "Inspect available prior work"
        monkeypatch.setattr(MemoryRetriever, "_select_with_model", select_memory)
    class SyntheticMemoryTransport:
        async def stream(self, **_kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(content=json.dumps({"summary": "Synthetic first-task session summary."}))
        async def close(self):
            pass
    instruction = "Produce the authorized report."
    tool_name = "create_employee_from_request" if first_task else "delegate_to_agent"
    arguments = ({"name": "Origin worker", "role_description": "Prepare referenced reports",
        "core_outputs": ["Report"], "boundaries": "Draft only; never send externally", "first_task": instruction}
        if first_task else {"agent_name": "Origin worker", "target_agent_id": str(receiver), "message": instruction})
    scope = {"tenant_id": seed["tenant_id"], "agent_id": seed["agent_id"], "session_id": seed["session_id"],
        "run_id": seed["run_id"], "turn_id": seed["turn_id"], "round_index": 1}
    call_id = "parent-delegation-call"
    if not native_creation:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            inputs = await session_model_round.bind_round_inputs(db, **scope)
            from app.kernel.engine import _mid_run_items_to_user_messages
            messages = [_context_message(role="system", content="Use the authorized tools.", kind="platform_guidance",
                unbound=() if parent_complete else ("controlled_parent_source_unbound",)),
                *_mid_run_items_to_user_messages(inputs)]
            provider_id = await session_model_round.prepare_model_request(db, **scope, messages=messages,
                tools=[], provider="openai", model="fake-4.1")
            await session_model_round.seal_model_response(db, **scope, provider_request_id=provider_id,
                response={"content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": tool_name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"})
            await session_model_round.commit_sealed_model_round(db, **scope, provider_request_id=provider_id)
            await db.commit()
        event = {"name": tool_name, "args": arguments, "tool_call_id": call_id,
            "provider_request_id": provider_id, "runtime_task_id": str(seed["run_id"])}
        await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            data={**event, "status": "running"})
    queued = {}
    dispatches = []
    native_dispatch = orchestrator.delegate_async
    async def capture_dispatch(**kwargs):
        dispatches.append(kwargs)
        queued.update(kwargs)
        queued["trace_id"] = kwargs.get("trace_id") or "origin-probe"
        handle = (await native_dispatch(**kwargs) if first_task or permission_case else orchestrator.AsyncDelegationHandle(
            task_id=uuid4().hex, trace_id="origin-probe", target_name="Origin worker", status="queued"))
        queued["task_id"] = handle.task_id
        return handle
    monkeypatch.setattr(orchestrator, "delegate_async", capture_dispatch)
    async def pre_effect(_payload):
        await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            data={**event, "status": "effect_started"})
    async def no_wakeup(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    if ledger_unknown_case:
        from app.services.agent_work_ledger import append_agent_work_ledger_finding

        append_agent_work_ledger_finding(agent_id=seed["agent_id"], session_id=str(seed["session_id"]),
            finding_type="finding", summary="LEGACY_PRIVATE_LEDGER_WITHOUT_NATIVE_RECEIPT")
    if workspace_listing_case:
        from app.models.chat_session import ChatSession
        from app.services.workspace_resource_authority import register_workspace_path

        prior_work = uuid4()
        prior_path = "workspace/prior-unverified-report.md"
        prior_body = "UNVERIFIED_PRIOR_REPORT_BODY"
        (seed["agent_root"] / prior_path).write_text(prior_body)
        async with owner_sessionmaker() as db:
            db.add(ChatSession(id=prior_work, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
                user_id=seed["user_id"], audience_kind="private"))
            await db.flush()
            await register_workspace_path(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], path=prior_path,
                owner_user_id=seed["user_id"], root_session_id=prior_work, source="synthetic_previous_work")
            await db.commit()
    if native_creation:
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.models.session_v2 import SessionToolInvocation
        from app.services.session_semantic_history import _event_payload
        from app.services.web_chat_runtime import execute_web_chat_run

        creator_client = ScriptedLLMClient([
            {"tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name,
                "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"},
            {"content": "The research employee and explicit first task are queued.", "finish_reason": "stop"}])
        if discovery_case:
            creator_client.script.insert(0, {"tool_calls": [
                {"id": f"creator-discovery-{index}-{name}-{args.get('ref', '')}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args)}}
                for index, (name, args) in enumerate(([(listing_tool, {"path": "workspace"})] if workspace_listing_case else [("task_list", {})] if parity_list_case else [
                    ("track_todo", {"action": "add", "title": "Create the research employee"}),
                    ("track_todo", {"action": "add", "title": "Start the explicit first task once"}),
                    ("track_todo", {"action": "add", "title": "Consume the returned report"}),
                    ("track_todo", {"action": "update", "title": "Create the research employee", "status": "in_progress"}),
                    ("record_finding", {"type": "finding", "summary": "The request provides synthetic report facts."}),
                    ("read_ledger", {}),
                    ("report_progress", {"message": "Preparing the authorized research employee."})]
                    if ledger_case else [("search_memory", {"query": "Create a research employee", "scope": "sessions"})]
                    if recall_case else [("read_context_resource", {"ref": "a2a-collaborators"}),
                    ("read_context_resource", {"ref": "organization"})] if context_case else [
                    ("read_ledger", {}), ("list_files", {"path": "workspace"}),
                    ("tool_search", {"query": "list_employees"})]))
            ], "finish_reason": "tool_calls"})
        if creator_task_inventory_case:
            creator_client.script[0]["tool_calls"].append({"id": "creator-task-inventory", "type": "function",
                "function": {"name": "list_async_tasks", "arguments": "{}"}})
        if legacy_workspace_read_case:
            creator_client.script[1:1] = [
                {"tool_calls": [{"id": "creator-old-workspace-read", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": prior_path})}}], "finish_reason": "tool_calls"},
                {"tool_calls": [{"id": f"creator-after-denial-todo-{index}", "type": "function", "function": {
                    "name": "track_todo", "arguments": json.dumps({"action": "add", "title": title})}}
                    for index, title in enumerate(("Create the employee", "Start the explicit first task once",
                        "Consume the returned report"))], "finish_reason": "tool_calls"},
            ]
        if creator_readiness_case:
            creator_client.script.insert(2, {"tool_calls": [{"id": "creator-early-first-task", "type": "function",
                "function": {"name": "delegate_to_agent", "arguments": "{}"}}], "finish_reason": "tool_calls"})
            native_readiness_stream = creator_client.stream

            async def continue_first_task_readiness(**kwargs):
                if len(creator_client.requests) == 2:
                    created = next(message for message in kwargs["messages"]
                        if (message.get("tool_call_id") if isinstance(message, dict) else message.tool_call_id) == call_id)
                    body = json.loads(created["content"] if isinstance(created, dict) else created.content)
                    creator_client.script[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({
                        "first_task_draft_id": body["blueprint_id"], "agent_name": arguments["name"],
                        "message": instruction})
                return await native_readiness_stream(**kwargs)

            monkeypatch.setattr(creator_client, "stream", continue_first_task_readiness)
        if legacy_wait_case:
            creator_client.script[0]["tool_calls"].append({"id": "creator-wait", "type": "function", "function": {
                "name": "execute_code" if legacy_code_case else "run_command",
                "arguments": json.dumps({"language": "python", "code": "print('waited')", "timeout": 10}
                    if legacy_code_case else {"command": "echo waited", "timeout": 10})}})
        if parity_sequence_case:
            def parity_call(identity, name, arguments):
                return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}

            creator_client.script[0] = {"tool_calls": [
                parity_call("creator-parity-empty-list", "task_list", {}),
                parity_call("creator-parity-missing-get", "task_get", {"task_id": str(uuid4())}),
                parity_call("creator-parity-create", "task_create", {"subject": "Create the research employee"}),
            ], "finish_reason": "tool_calls"}
            creator_client.script.insert(1, {"tool_calls": [
                parity_call("creator-parity-get", "task_get", {}),
                parity_call("creator-parity-update", "task_update", {"status": "in_progress"}),
                parity_call("creator-parity-list", "task_list", {}),
            ], "finish_reason": "tool_calls"})
            native_parity_stream = creator_client.stream

            async def continue_parity_ledger(**kwargs):
                if len(creator_client.requests) == 1:
                    created = next(message for message in kwargs["messages"]
                        if (message.get("tool_call_id") if isinstance(message, dict) else message.tool_call_id) == "creator-parity-create")
                    body = json.loads(created["content"] if isinstance(created, dict) else created.content)
                    task_identity = body["task"]["id"]
                    for call in creator_client.script[0]["tool_calls"]:
                        if call["function"]["name"] in {"task_get", "task_update"}:
                            args = json.loads(call["function"]["arguments"])
                            call["function"]["arguments"] = json.dumps({**args, "task_id": task_identity})
                return await native_parity_stream(**kwargs)

            monkeypatch.setattr(creator_client, "stream", continue_parity_ledger)
        if input_case == "first_task_explicit_unverified_memory":
            creator_client.script.insert(0, {"tool_calls": [{"id": "read-old-memory", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": old_memory_path + "/summary.md"})}}],
                "finish_reason": "tool_calls"})
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: creator_client)
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: SyntheticMemoryTransport())
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            run.status, run.claimed_by, run.claim_version = "running", "native-creator-worker", 1
            run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        creator_fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="native-creator-worker", claim_version=1)
        try:
            await execute_web_chat_run(seed["run_id"])
        finally:
            reset_runtime_task_fence(creator_fence)
        (tmp_path / "native-creator-model-requests.json").write_text(json.dumps(creator_client.requests, default=str, indent=2))
        if input_case == "first_task_explicit_unverified_memory":
            assert len(creator_client.requests) == 3
            assert "OLD_UNVERIFIED_CREATION_MEMORY" not in json.dumps(creator_client.requests[0]["messages"])
            assert "OLD_UNVERIFIED_CREATION_MEMORY" in json.dumps(creator_client.requests[1]["messages"])
        elif memory_case:
            visible = json.dumps(creator_client.requests[0]["messages"])
            assert old_memory_path in visible and "source_unverified" in visible
            assert "OLD_UNVERIFIED_CREATION_MEMORY" not in visible
        if workspace_listing_case:
            listed = json.dumps(creator_client.requests[1]["messages"])
            assert "prior-unverified-report.md" in listed and prior_body not in listed
            assert (seed["agent_root"] / prior_path).read_text() == prior_body
        if legacy_workspace_read_case:
            after_read = json.dumps(creator_client.requests[2]["messages"])
            assert prior_body not in after_read and "workspace_content_unverified" in after_read
            assert "regenerate" in after_read
        if discovery_case:
            if creator_readiness_case:
                assert len(creator_client.requests) == 4
            elif parity_sequence_case:
                assert len(creator_client.requests) == 4
            elif legacy_workspace_read_case:
                assert len(creator_client.requests) == 5
            else:
                assert len(creator_client.requests) == 3
        if ledger_unknown_case:
            assert "LEGACY_PRIVATE_LEDGER_WITHOUT_NATIVE_RECEIPT" in json.dumps(creator_client.requests[1]["messages"])
        if input_case != "first_task_explicit_unverified_memory" and not discovery_case:
            assert len(creator_client.requests) == 2 and "NATIVE_PARENT_CONTEXT" in json.dumps(creator_client.requests[0]["messages"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            if legacy_wait_case:
                from app.models.invocation_span import InvocationSpan

                wait = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"],
                    SessionToolInvocation.provider_tool_use_id == "creator-wait"))
                proof = await db.get(InvocationSpan, uuid5(wait.id, "workspace-output-evidence"))
                inputs = proof.metadata_json["workspace_execution_evidence"]["execution_inputs"]
                assert inputs == {"schema": "hive.workspace_execution_inputs.v1", "observed": True,
                    "sources": [], "unbound": []}, inputs
                result = _event_payload(await db.get(ChatTranscriptEvent, wait.result_event_id))["content"]
                assert "waited" in result and "workspace_input_denials" in result and prior_body not in result
                from app.services.employee_creation_sources import _owned_context_source
                from app.services.session_model_inputs import tool_result_source_hash

                event_payload = _event_payload(await db.get(ChatTranscriptEvent, wait.result_event_id))
                source = {"source_kind": "session_tool_invocation", "source_id": str(wait.id),
                    "source_version": tool_result_source_hash(event_payload)}
                async def wait_published():
                    return await _owned_context_source(db, dependency=source, agent_id=seed["agent_id"],
                        tenant_id=seed["tenant_id"], session_id=seed["session_id"], user_id=seed["user_id"])
                assert await wait_published()
                control = await db.begin_nested()
                proof.metadata_json = {**proof.metadata_json, "workspace_execution_evidence": {
                    **proof.metadata_json["workspace_execution_evidence"], "execution_inputs": {
                        **inputs, "unbound": [prior_path]}}}
                await db.flush()
                assert not await wait_published()
                await control.rollback()
                assert await wait_published()
            invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"],
                SessionToolInvocation.provider_tool_use_id == call_id))
            assert invocation is not None and invocation.result_event_id is not None
            result = _event_payload(await db.get(ChatTranscriptEvent, invocation.result_event_id))["content"]
            if first_task:
                from app.services.employee_creation_sources import creation_tool_result_bound

                assert (await db.get(RuntimeTask, seed["run_id"])).status == "completed"
                if input_case == "first_task_explicit_unverified_memory" or ledger_unknown_case:
                    assert not await creation_tool_result_bound(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        invocation=invocation, content=result)
                else:
                    assert await creation_tool_result_bound(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        invocation=invocation, content=result), result
                assert not await creation_tool_result_bound(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    invocation=invocation, content=json.dumps({**json.loads(result), "provisioning_task_id": str(uuid4())}))
            creating_rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
            if input_case == "first_task_explicit_unverified_memory" or ledger_unknown_case:
                ordered = sorted(creating_rounds, key=lambda row: session_model_round._root_round_index_from_id(row.round_id))
                if parity_sequence_case:
                    assert len(ordered) == 4
                else:
                    assert len(ordered) == 3
                assert ordered[0].model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]
                assert not ordered[1].model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]
            elif discovery_case:
                if creator_readiness_case:
                    assert len(creating_rounds) == 4 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
                elif parity_sequence_case:
                    assert len(creating_rounds) == 4 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
                elif legacy_workspace_read_case:
                    assert len(creating_rounds) == 5 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
                else:
                    assert len(creating_rounds) == 3 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
            else:
                assert len(creating_rounds) == 2 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
            if creator_readiness_case:
                from types import SimpleNamespace
                from app.services.employee_creation_sources import _owned_context_source
                from app.services.employee_first_task import first_task_preflight_result_bound
                from app.services.session_model_inputs import _origin_dependencies

                created = json.loads(result)
                assert created["first_task_autostart"] is False
                assert created["provisioning"]["first_task"] == {"status": "waiting_for_employee",
                    "auto_dispatch": True, "authorization": "original_human_request"}
                assert "already authorized" in created["creation_flow"]["instruction"]
                early = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == seed["run_id"],
                    SessionToolInvocation.provider_tool_use_id == "creator-early-first-task"))
                early_payload = _event_payload(await db.get(ChatTranscriptEvent, early.result_event_id))
                assert early.effect_state == "failed" and "employee_not_ready" in early_payload["content"]
                assert not list(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"],
                    RuntimeTask.parent_agent_id == seed["agent_id"], RuntimeTask.task_type == "delegation")))
                deps, complete = await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    ref=f"session-tool-result:{early.id}")
                source = next(item for item in deps if item["source_id"] == str(early.id))
                assert complete
                scope = {"agent_id": seed["agent_id"], "tenant_id": seed["tenant_id"],
                    "session_id": seed["session_id"], "user_id": seed["user_id"]}
                assert await _owned_context_source(db, dependency=source, **scope)
                assert not await _owned_context_source(db, dependency={**source, "source_version": "0" * 64}, **scope)
                assert not await _owned_context_source(db, dependency=source, **{**scope, "session_id": uuid4()})
                fields = {key: getattr(early, key) for key in ("tenant_id", "session_id", "run_id", "tool_name",
                    "effect_state", "provider_tool_use_id", "provider_arguments_json", "effective_arguments_json",
                    "args_hash", "effective_args_hash", "receipt_ref")}
                def check(call, payload=early_payload, content=early_payload["content"]):
                    return first_task_preflight_result_bound(
                        agent_id=seed["agent_id"], invocation=call, payload=payload, content=content)
                assert check(SimpleNamespace(**fields))
                for key, value in {"effect_state": "needs_reconciliation", "session_id": uuid4(), "run_id": uuid4(),
                    "tenant_id": uuid4(), "provider_tool_use_id": "another-call", "effective_args_hash": "0" * 64}.items():
                    assert not check(SimpleNamespace(**{**fields, key: value}))
                assert not check(SimpleNamespace(**fields), payload={**early_payload, "outcome": "unknown"})
                assert not check(SimpleNamespace(**fields), content=early_payload["content"].replace(
                    'hive.first_task_preflight.v1', 'hive.first_task_preflight.forged'))
            if workspace_listing_case:
                from copy import deepcopy
                from app.models.invocation_span import InvocationSpan
                from app.models.organization_identity import ContentInputReceipt
                from app.services.content_input_receipts import workspace_artifact_ref
                from app.services.session_model_inputs import _origin_dependencies

                listing = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == seed["run_id"], SessionToolInvocation.tool_name == listing_tool))
                listed_event = await db.get(ChatTranscriptEvent, listing.result_event_id)
                pointer = _event_payload(listed_event)["metadata"]["native_directory_read_receipt"]
                assert pointer["status"] == "verified"
                evidence_id = UUID(pointer["evidence_ref"].split(":", 1)[1])
                captured = await db.get(InvocationSpan, evidence_id)
                original_proof = deepcopy(captured.metadata_json)
                ref = f"session-tool-result:{listing.id}"
                assert (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
                async with owner_sessionmaker() as writer:
                    evidence = await writer.get(InvocationSpan, evidence_id)
                    evidence.metadata_json = {"native_directory_read_evidence": {
                        **original_proof["native_directory_read_evidence"], "tool_result_hash": "0" * 64}}
                    await writer.commit()
                assert not (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
                async with owner_sessionmaker() as writer:
                    (await writer.get(InvocationSpan, evidence_id)).metadata_json = original_proof
                    await writer.commit()
                assert (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
                assert await db.scalar(select(ContentInputReceipt.id).where(ContentInputReceipt.tenant_id == seed["tenant_id"],
                    ContentInputReceipt.carrier_ref == workspace_artifact_ref(seed["agent_id"], prior_path))) is None
            if legacy_workspace_read_case:
                from app.services.content_input_receipts import ContentInputReceiptError, load_native_read_denial_evidence
                from app.services.employee_creation_sources import _owned_context_source
                from app.services.session_model_inputs import tool_result_source_hash

                read = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"],
                    SessionToolInvocation.provider_tool_use_id == "creator-old-workspace-read"))
                read_event = await db.get(ChatTranscriptEvent, read.result_event_id)
                read_payload = _event_payload(read_event)
                denial = read_payload["metadata"]["native_read_denial_receipt"]
                assert denial["status"] == "verified"
                assert prior_body not in json.dumps(creator_client.requests)
                read_model = await db.scalar(select(SessionModelResult).where(
                    SessionModelResult.run_id == seed["run_id"], SessionModelResult.provider_request_id == read.provider_request_id))
                kwargs = {"agent_id": seed["agent_id"], "invocation": read, "model_result": read_model,
                    "arguments": read.effective_arguments_json or read.provider_arguments_json,
                    "content_hash": read_payload["content_hash"],
                    "capture": {"status": "captured", "receipt_ref": denial["evidence_ref"]}}
                assert await load_native_read_denial_evidence(db, **kwargs) == denial
                for changed in ({"agent_id": uuid4()}, {"content_hash": "0" * 64},
                    {"arguments": {"path": "workspace/another.md"}}):
                    with pytest.raises(ContentInputReceiptError):
                        await load_native_read_denial_evidence(db, **{**kwargs, **changed})
                fields = {key: getattr(read, key) for key in ("id", "tenant_id", "session_id", "run_id", "tool_name",
                    "authority_snapshot_hash")}
                with pytest.raises(ContentInputReceiptError):
                    await load_native_read_denial_evidence(db, **{**kwargs,
                        "invocation": SimpleNamespace(**{**fields, "session_id": uuid4()})})
                assert (await load_native_read_denial_evidence(db, **{**kwargs, "capture": None}))["status"] == "unavailable"
                ref = f"session-tool-result:{read.id}"
                assert (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
                dependency = {"source_kind": "session_tool_invocation", "source_id": str(read.id),
                    "source_version": tool_result_source_hash(read_payload)}
                scope = {"tenant_id": seed["tenant_id"], "agent_id": seed["agent_id"],
                    "session_id": seed["session_id"], "user_id": seed["user_id"]}
                assert await _owned_context_source(db, dependency=dependency, **scope)
                assert not await _owned_context_source(db, dependency={**dependency, "source_version": "0" * 64}, **scope)
                assert not await _owned_context_source(db, dependency=dependency, **{**scope, "session_id": uuid4()})
                before = deepcopy(read_event.metadata_json)
                payload = deepcopy(before)
                payload["v2_payload"]["metadata"].pop("native_read_denial_receipt")
                async with owner_sessionmaker() as writer:
                    (await writer.get(ChatTranscriptEvent, read_event.id)).metadata_json = payload
                    await writer.commit()
                await db.refresh(read_event)
                assert not (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
                async with owner_sessionmaker() as writer:
                    (await writer.get(ChatTranscriptEvent, read_event.id)).metadata_json = before
                    await writer.commit()
                await db.refresh(read_event)
                assert (await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], ref=ref))[1]
    else:
        execution_trace = {}
        from app.core.execution_context import clear_execution_identity, get_execution_identity, set_delegated_user_identity, set_execution_identity

        previous_identity = get_execution_identity()
        if permission_case:
            set_delegated_user_identity(seed["user_id"], "Original requester", channel="web")
        try:
            result = await agent_tools.execute_tool(tool_name, arguments, agent_id=seed["agent_id"],
                user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]),
                turn_id=scope["turn_id"], tool_call_id=call_id, pre_effect_callback=pre_effect,
                permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False,
                trace_metadata_sink=execution_trace)
        finally:
            if previous_identity is None:
                clear_execution_identity()
            else:
                set_execution_identity(previous_identity)
        if permission_case:
            from app.services.session_permission_runtime import _execution_evidence

            evidence = _execution_evidence(execution_trace)
            assert evidence["status"] == "settled" and evidence["tool_decision"]["outcome"] == "allow", str(result)
            await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
                data={**event, "status": "done", "result": str(result), "tool_execution_evidence": evidence})
    if first_task:
        from uuid import UUID
        from app.models.hr_creation import HrCreationDraft
        from app.models.runtime_task import RuntimeTask
        from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

        creation = json.loads(str(result))
        assert creation.get("draft_status") == "confirmed", creation
        if delivery_case and not native_creation:
            from app.services.session_permission_runtime import _execution_evidence

            evidence = _execution_evidence(execution_trace)
            assert evidence["status"] == "settled" and evidence["tool_decision"]["outcome"] == "allow"
            await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
                data={**event, "status": "done", "result": str(result), "tool_execution_evidence": evidence})
        job_id = UUID(creation["provisioning_task_id"])
        async with owner_sessionmaker() as db:
            job = await db.get(RuntimeTask, job_id)
            job.status, job.claimed_by, job.claim_version = "running", "creation-origin-worker", 1
            await db.commit()
        async def no_refinement(*_args, **_kwargs):
            if refined_input or native_refined:
                from types import SimpleNamespace

                return SimpleNamespace(provider="openai", model="synthetic-refiner", api_key="synthetic", base_url=None, max_output_tokens=17001), "synthetic_refiner"
            return None, "synthetic_transport"
        monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
        if refined_input:
            from app.tools.handlers.hr import _refine_soul_inputs

            async def same_bytes_without_model_io(**kwargs):
                return await _refine_soul_inputs(**{**kwargs, "model_config": {}})
            monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", same_bytes_without_model_io)
        refinement_calls = []
        if native_refined:
            async def refinement_transport(**kwargs):
                assert kwargs["usage_source"] == "hr_soul_refine"
                refinement_calls.append(kwargs)
                return {"choices": [{"message": {"content": json.dumps({
                    "personality": "REFINED_CONTEXT: Analyze sources carefully and deliver referenced draft reports.",
                    "quality_standards": ["Retain source references in each report."]})}}]}
            monkeypatch.setattr("app.services.llm_client.chat_complete", refinement_transport)
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: SyntheticMemoryTransport())
        assert await execute_claimed_hr_provisioning(job_id) == "completed"
        assert await execute_claimed_hr_provisioning(job_id) == "completed"
        if native_refined:
            assert len(refinement_calls) == 1 and "Prepare referenced reports" in refinement_calls[0]["messages"][0]["content"]
            assert refinement_calls[0]["max_tokens"] == 17001
        async with owner_sessionmaker() as db:
            draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
            receiver = draft.created_agent_id
            assert draft.preview_json["creation_authorization"]["creation_origin"]
            if native_refined:
                assert "REFINED_CONTEXT" in (seed["data_root"] / str(receiver) / "soul.md").read_text()
    assert queued, str(result)
    assert len(dispatches) == 1
    message = queued["conversation_messages"][0]
    assert message["content"] == instruction
    assert isinstance(message.get("a2a_instruction_origin"), dict), message

    task_id = UUID(queued.pop("task_id"))
    if first_task or permission_case:
        from app.services.runtime_task_service import get_runtime_task_record

        request = await orchestrator._build_delegation_request_from_runtime_record(await get_runtime_task_record(task_id.hex))
        assert request is not None and request.conversation_messages == [message]
    else:
        request = orchestrator.AgentDelegationRequest(**queued, runtime_task_id=str(task_id),
            execution_identity=ExecutionIdentityRef(identity_type="delegated_user", identity_id=seed["user_id"]))
        request.execution_receipt = orchestrator._build_delegation_execution_receipt(request,
            task_id=str(task_id), trace_id="origin-probe", status="running")
    async with owner_sessionmaker() as db:
        target = await db.get(Agent, receiver)
        if first_task or permission_case:
            if not first_task:
                await AgentManager().initialize_agent_files(db, target, personality="NATIVE_RECEIVER_CONTEXT",
                    original_author_id=seed["user_id"])
            child = await db.get(RuntimeTask, task_id)
            child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "origin-worker", 1, 1
            child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        else:
            await AgentManager().initialize_agent_files(db, target, personality="NATIVE_RECEIVER_CONTEXT",
                original_author_id=seed["user_id"])
            db.add(RuntimeTask(id=task_id, task_type="delegation", status="running", tenant_id=seed["tenant_id"],
            parent_agent_id=seed["agent_id"], child_agent_id=receiver, parent_session_id=str(seed["session_id"]),
            child_session_id=request.session_id, root_session_id=str(seed["session_id"]), root_user_id=seed["user_id"],
            root_runtime_task_id=seed["run_id"], claimed_by="origin-worker", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                metadata_json=orchestrator._build_runtime_task_metadata(request, task_id=str(task_id))))
        if revoked_input:
            parent = await db.get(Agent, seed["agent_id"])
            parent.deactivated_at = datetime.now(timezone.utc)
        await db.commit()
    report_path = "workspace/delegated-report.md"
    child_commands = ([{"id": "child-calculate", "type": "function", "function": {
        "name": child_tool, "arguments": json.dumps({"language": "python", "code": "print(120 + 90 + 150)",
            "timeout": 10} if child_tool == "execute_code" else {
            "command": "python3 -c 'print(120 + 90 + 150)'", "timeout": 10})}}] if child_command_case else [])
    client = ScriptedLLMClient([{"tool_calls": [*child_commands, {"id": "child-report-write", "type": "function", "function": {
        "name": "write_file", "arguments": json.dumps({"path": report_path, "content": "DELEGATED_REPORT"})}}],
        "finish_reason": "tool_calls"}, {"content": "Report generated.", "finish_reason": "stop"}])
    if child_readback_case:
        client.script.insert(1, {"tool_calls": [{"id": "child-report-read", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": report_path})}}], "finish_reason": "tool_calls"})
    if sequential_child_case:
        # The next model round consumes the calculation before authoring its file.
        client.script[1]["tool_calls"].insert(0, client.script[0]["tool_calls"].pop())
    if glob_case:
        client.script[0]["tool_calls"].append({"id": "child-absent-glob", "type": "function", "function": {
            "name": "glob_search", "arguments": json.dumps({"pattern": report_path})}})
        client.script[1]["tool_calls"].append({"id": "child-existing-glob", "type": "function", "function": {
            "name": "glob_search", "arguments": json.dumps({"root": "workspace", "pattern": "*.md"})}})
    if workspace_listing_case:
        client.script[0]["tool_calls"].insert(0, {"id": "child-empty-directory", "type": "function", "function": {
            "name": "list_files", "arguments": json.dumps({"path": "workspace"})}})
    if parity_list_case or parity_sequence_case:
        client.script[0]["tool_calls"].insert(0, {"id": "child-parity-list", "type": "function", "function": {
            "name": "task_list", "arguments": "{}"}})
    if child_missing_file_case:
        client.script.insert(0, {"tool_calls": [{"id": "child-missing-report-read", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": report_path})}}], "finish_reason": "tool_calls"})
    if slow_first_task:
        import asyncio

        native_stream = client.stream
        async def delayed_stream(**kwargs):
            if not client.requests:
                await asyncio.sleep(31)
            return await native_stream(**kwargs)
        monkeypatch.setattr(client, "stream", delayed_stream)
    from app.services import content_input_receipts
    native_writer = content_input_receipts.record_native_workspace_receipts
    observed = {}
    async def observe_writer(*args, **kwargs):
        observed["context"] = kwargs["context"]
        result = await native_writer(*args, **kwargs)
        observed["status"] = result
        return result
    monkeypatch.setattr(content_input_receipts, "record_native_workspace_receipts", observe_writer)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    if progress_case:
        from tests.integration.original_work_progress_helpers import (
            PROGRESS, PRIVATE_REASONING, observe_first_task_progress, assert_first_task_progress,
            assert_completed_progress,
        )

        client.script[0].update(content=PROGRESS, reasoning_content=PRIVATE_REASONING)
        observe_first_task_progress(seed=seed, task_id=task_id, draft_id=creation["blueprint_id"],
            factory=app_user_sessionmaker, owner_factory=owner_sessionmaker, monkeypatch=monkeypatch, observations=observed)
        token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
        try:
            assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
        finally:
            reset_runtime_task_fence(token)
        assert_first_task_progress(observed)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(RuntimeTask, task_id)
            assert child.status == "completed" and child.child_session_id == request.session_id
        assert len(client.requests) == 2 and (seed["data_root"] / str(receiver) / report_path).read_text() == "DELEGATED_REPORT"
        await assert_completed_progress(seed=seed, factory=app_user_sessionmaker,
            draft_id=creation["blueprint_id"], observations=observed)
        return
    if permission_case:
        from app.models.session_v2 import SessionToolInvocation

        decision = input_case.removeprefix("first_task_feishu_permission_") if first_task else "allow"
        channel_permission = decision.startswith("channel_")
        decision = decision.removeprefix("channel_")
        legacy_permission = decision == "legacy"
        scope_permission = decision == "scope"
        feedback_retry = decision == "feedback_retry"
        if legacy_permission or scope_permission or feedback_retry:
            decision = "allow"
        expected_summary = "Report generated." if decision == "allow" else "The write was not authorized; no report was created."
        if decision != "allow":
            client.script[-1]["content"] = expected_summary
        token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
        try:
            await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
        finally:
            reset_runtime_task_fence(token)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(RuntimeTask, task_id)
            pending = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task_id,
                SessionToolInvocation.permission_state == "waiting")))
            assert child.status == "suspended" and child.completed_at is None, (child.status, child.result_summary, child.metadata_json)
            assert len(pending) == 1 and pending[0].effect_state == "prepared_not_started"
            assert pending[0].result_event_id is None and len(client.requests) == 1
            permission_id = pending[0].permission_item_id
            invocation_id, permission_provider_id = pending[0].id, pending[0].provider_request_id
            if first_task:
                from app.models.channel_delivery_outbox import ChannelDeliveryOutbox

                notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
                    ChannelDeliveryOutbox.runtime_task_id == task_id,
                    ChannelDeliveryOutbox.metadata_json["source"].astext == "original_work_permission")))
                assert len(notices) == 1
                assert notices[0].prompt_key == str(permission_id) and notices[0].delivery_kind == "interactive_prompt"
                assert notices[0].session_id == seed["session_id"] and notices[0].user_id == seed["user_id"]
                if legacy_permission:
                    from app.models.chat_transcript_event import ChatTranscriptEvent
                    from app.services.original_work_permission_notices import _original_work, permission_notice_text
                    from app.services.session_user_question import question_payload

                    event = await db.scalar(select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.invocation_id == invocation_id, ChatTranscriptEvent.item_kind == "tool_permission",
                        ChatTranscriptEvent.lifecycle == "waiting"))
                    _, record, _, _ = await _original_work(db, child)
                    notices[0].metadata_json = {k: v for k, v in notices[0].metadata_json.items() if k != "interaction_schema"}
                    notices[0].text_content = permission_notice_text(child, record, question_payload(event)["permission_request"])
                    assert "/allow" not in notices[0].text_content
                    await db.commit()
        assert not (seed["data_root"] / str(receiver) / report_path).exists()
        if first_task:
            from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService, _claimed
            from app.services.content_input_receipts import ContentInputReceiptError
            from app.services.feishu_service import FeishuService

            service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
            @asynccontextmanager
            async def permission_notice_session(_operation):
                from app.database import tenant_scoped_session

                async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as scoped:
                    yield scoped
            monkeypatch.setattr(service, "_worker_session", permission_notice_session)
            published = []
            expected_text = notices[0].text_content
            async def permission_transport(_self, _app_id, _secret, recipient, message_type, content, **kwargs):
                text = json.loads(content)["text"]
                if text == expected_text:
                    published.append((recipient, message_type, kwargs["receive_id_type"], text))
                return {"data": {"message_id": f"synthetic-permission-notice-{len(published)}"}}
            monkeypatch.setattr(FeishuService, "send_message", permission_transport)
            if decision == "allow":
                claimed_notice = _claimed(notices[0])
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    for corrupted in [replace(claimed_notice, text=claimed_notice.text + "\nUNATTESTED_BYTES"),
                        replace(claimed_notice, delivery_target={**claimed_notice.delivery_target, "receive_id": "another-recipient"}),
                        replace(claimed_notice, metadata={k: v for k, v in claimed_notice.metadata.items() if k != "source"}),
                        replace(claimed_notice, metadata={**claimed_notice.metadata, "invocation_id": str(uuid4())})]:
                        with pytest.raises(ContentInputReceiptError):
                            await service._authorize_native_result_text(db, corrupted)
                assert published == []
            drained = await service.drain_once(worker_id="first-task-permission-notice")
            assert len(published) == 1 and drained["delivered"] >= 1, drained
            assert published[0][:3] == ("ou_creation_owner", "text", "open_id")
            assert report_path in published[0][3] and "此提示不代表任务完成" in published[0][3]
            assert f"https://hive.synthetic.test/agents/{seed['agent_id']}?session_id={seed['session_id']}#chat" in published[0][3]
            assert (await service.drain_once(worker_id="first-task-permission-replay"))["claimed"] == 0
        if decision != "cancel":
            current = await orchestrator.check_async_delegation(task_id.hex, parent_agent_id=seed["agent_id"])
            assert current["status"] == "suspended", current
            foreign = await orchestrator.check_async_delegation(task_id.hex, parent_agent_id=uuid4())
            assert foreign["status"] == "forbidden" and foreign["result"] is None
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api.chat_sessions import router
        from app.core.security import get_current_user
        from app.database import get_db
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.delegation_session_runtime import suspend_delegation_for_question
        from app.models.user import User

        if not first_task:
            async with owner_sessionmaker() as db:
                user = await db.get(User, seed["user_id"])
        permission_calls = []
        execute_permission = agent_tools.execute_session_permission_tool
        async def actual_permission(*args, **kwargs):
            permission_calls.append(kwargs)
            return await execute_permission(*args, **kwargs)
        monkeypatch.setattr(agent_tools, "execute_session_permission_tool", actual_permission)
        async with owner_sessionmaker() as db:
            other = User(id=uuid4(), username=uuid4().hex, email=f"{uuid4().hex}@synthetic.invalid", password_hash="synthetic",
                display_name="Another company administrator", tenant_id=user.tenant_id, role="org_admin", is_active=True)
            db.add(other)
            await db.commit()

        async def permission_db():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                yield db
        app = FastAPI()
        app.include_router(router, prefix="/api")
        from app.api.hr_creation import router as creation_router

        app.include_router(creation_router, prefix="/api")
        app.dependency_overrides[get_db] = permission_db
        app.dependency_overrides[get_current_user] = lambda: user
        url = f"/api/agents/{receiver}/sessions/{request.session_id}/permissions/{permission_id}/resolve"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://permission.local") as api:
            if first_task:
                draft_url = f"/api/agents/{seed['agent_id']}/hr-creation-drafts/{creation['blueprint_id']}"
                original = await api.get(draft_url)
                assert original.status_code == 200, original.text
                work = original.json()["first_task_work"]
                assert work["status"] == "suspended" and len(work.get("permissions", [])) == 1, original.text
                permission_view = work["permissions"][0]
                assert permission_view["can_resolve"] is True and permission_view["session_id"] == request.session_id
                assert permission_view["request"]["permission_request_id"] == str(permission_id)
                assert permission_view["request"]["runtime_task_id"] == str(task_id)
                assert permission_view["request"]["arguments"] == {"path": report_path, "content": "DELEGATED_REPORT"}
                if decision == "allow":
                    soul = seed["data_root"] / str(receiver) / "soul.md"
                    original_soul = soul.read_bytes()
                    try:
                        soul.write_bytes(original_soul + b"\nChanged before original-work permission read.\n")
                        changed = await api.get(draft_url)
                        assert changed.status_code == 200, changed.text
                        assert changed.json()["first_task_work"]["permissions"] == []
                        assert changed.json()["first_task_work"]["status"] == "unavailable"
                    finally:
                        soul.write_bytes(original_soul)
            ordinary = await api.post(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs", json={
                "kind": "start_turn", "input_id": str(uuid4()), "idempotency_key": str(uuid4()),
                "content_parts": [{"type": "text", "text": "Unrelated work"}]})
            assert ordinary.status_code == 409 and ordinary.json()["detail"]["code"] == "session_read_only"
            app.dependency_overrides[get_current_user] = lambda: other
            try:
                if first_task:
                    hidden = await api.get(draft_url)
                    assert hidden.status_code in {403, 404}, hidden.text
                denied = await api.post(url, json={"action": "allow_once"})
                assert denied.status_code in {403, 404}, denied.text
                assert not (seed["data_root"] / str(receiver) / report_path).exists()
            finally:
                app.dependency_overrides[get_current_user] = lambda: user
            wrong_agent = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{request.session_id}/permissions/{permission_id}/resolve",
                json={"action": "allow_once"})
            assert wrong_agent.status_code == 404, wrong_agent.text
            if decision == "cancel":
                stopped = await orchestrator.cancel_async_delegation(task_id.hex, parent_agent_id=seed["agent_id"], force=True)
                assert stopped["status"] == "killed", stopped
                late = await api.post(url, json={"action": "allow_once"})
                assert late.status_code == 409 and not permission_calls and len(client.requests) == 1, late.text
                if first_task:
                    from tests.integration.original_work_permission_helpers import claim_permission_reply, dispatch_permission_rejection

                    reply_service, reply_item = await claim_permission_reply(factory=app_user_sessionmaker,
                        tenant_id=seed["tenant_id"], notice_id=notices[0].id, text="/allow")
                    rejected = await dispatch_permission_rejection(factory=app_user_sessionmaker, tenant_id=seed["tenant_id"],
                        notice_id=notices[0].id, item=reply_item, exact_work=True)
                    assert rejected.get("permission_status") == "rejected" and not permission_calls, rejected
                    assert await reply_service._mark_processed(item=reply_item, worker_id="permission-quote", receipt=rejected)
                if first_task:
                    assert (await api.get(draft_url)).json()["first_task_work"]["permissions"] == []
                assert not (seed["data_root"] / str(receiver) / report_path).exists()
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    assert (await db.get(RuntimeTask, task_id)).status == "killed"
                    assert await RuntimeTaskClaimService(db=db, worker_id="cancelled-permission", task_types=("delegation",)).claim_available(batch_size=10) == []
                return
            if decision == "unknown":
                from app.services import session_tool_runtime

                async def lose_result(*_args, **_kwargs):
                    pass
                monkeypatch.setattr(session_tool_runtime, "complete_tool_invocation", lose_result)
                if channel_permission:
                    from tests.integration.original_work_permission_helpers import resolve_permission_reply

                    quoted = await resolve_permission_reply(factory=app_user_sessionmaker,
                        tenant_id=seed["tenant_id"], notice_id=notices[0].id, text="/allow")
                    assert quoted["permission_receipt"]["run_status"] == "needs_reconciliation" and not quoted["permission_receipt"]["retryable"]
                for _ in range(2):
                    uncertain = await api.post(url, json={"action": "allow_once"})
                    assert uncertain.status_code == 200 and uncertain.json()["run_status"] == "needs_reconciliation", uncertain.text
                    assert uncertain.json()["retryable"] is False
                assert len(permission_calls) == 1 and len(client.requests) == 1
                if first_task:
                    assert (await api.get(draft_url)).json()["first_task_work"]["permissions"] == []
                assert (seed["data_root"] / str(receiver) / report_path).read_text() == "DELEGATED_REPORT"
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    assert await RuntimeTaskClaimService(db=db, worker_id="unknown-permission", task_types=("delegation",)).claim_available(batch_size=10) == []
                return
            if decision == "expired":
                from app.services.session_permission_runtime import expire_stale_session_permission_requests

                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    (await db.get(SessionToolInvocation, invocation_id)).permission_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                    await db.commit()
                    assert await expire_stale_session_permission_requests(db=db) == 1
                    invocation_row = await db.get(SessionToolInvocation, invocation_id, populate_existing=True)
                    assert invocation_row.permission_state == "expired" and invocation_row.result_event_id is not None
                    assert (await db.get(RuntimeTask, task_id)).status == "resumable"
                late = await api.post(url, json={"action": "allow_once"})
                assert late.status_code == 410 and late.json()["detail"]["code"] == "tool_permission_request_expired", late.text
                assert not permission_calls
                if first_task:
                    from tests.integration.original_work_permission_helpers import claim_permission_reply, dispatch_permission_rejection

                    reply_service, reply_item = await claim_permission_reply(factory=app_user_sessionmaker,
                        tenant_id=seed["tenant_id"], notice_id=notices[0].id, text="/allow")
                    rejected = await dispatch_permission_rejection(factory=app_user_sessionmaker, tenant_id=seed["tenant_id"],
                        notice_id=notices[0].id, item=reply_item, exact_work=True)
                    assert rejected.get("permission_status") == "rejected" and not permission_calls, rejected
                    assert await reply_service._mark_processed(item=reply_item, worker_id="permission-quote", receipt=rejected)
            else:
                soul = seed["data_root"] / str(receiver) / "soul.md"
                original_soul = soul.read_bytes()
                if decision == "source_changed":
                    soul.write_bytes(original_soul + b"\nChanged before the approved effect.\n")
                if channel_permission:
                    from tests.integration.original_work_permission_helpers import resolve_permission_reply

                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        runs_before_quote = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"])))
                    if decision == "allow" and not legacy_permission:
                        from copy import deepcopy
                        from app.models.external_principal import ExternalPrincipal
                        from tests.integration.original_work_permission_helpers import claim_permission_reply, dispatch_permission_rejection

                        for changed in (("source",) if feedback_retry else ("scope",) if scope_permission else ("binding", "source", "receipt", "body", "request", "prose", "model")):
                            async with app_user_sessionmaker() as db:
                                await pin_rls_tenant_context(db, seed["tenant_id"])
                                saved = await db.get(ChannelDeliveryOutbox, notices[0].id)
                                saved_text, saved_meta = saved.text_content, deepcopy(saved.metadata_json)
                                saved_receipts = deepcopy(saved.delivery_receipts_json)
                                provider_reference = saved_receipts["text"]["detail"]["provider_message_id"]
                                principal = await db.get(ExternalPrincipal, saved.external_principal_id)
                                linked = principal.linked_user_id
                                if changed == "binding":
                                    principal.linked_user_id = other.id
                                elif changed == "receipt":
                                    saved.delivery_receipts_json = {**saved_receipts, "text": {**saved_receipts["text"],
                                        "detail": {k: v for k, v in saved_receipts["text"]["detail"].items() if k != "provider_message_id"}}}
                                elif changed == "scope":
                                    saved.delivery_receipts_json = {**saved_receipts, "text": {**saved_receipts["text"],
                                        "detail": {k: v for k, v in saved_receipts["text"]["detail"].items() if k != "provider_message_scope"}}}
                                elif changed == "body":
                                    saved.text_content += "\nUNATTESTED_REPLY_SCOPE"
                                elif changed == "model":
                                    saved.metadata_json = {**saved_meta, "model_result_id": str(uuid4())}
                                await db.commit()
                            if changed == "source":
                                soul.write_bytes(original_soul + b"\nChanged before quoted permission.\n")
                            try:
                                text = "/allow " + str(uuid4()) if changed == "request" else "Please continue" if changed == "prose" else "/allow"
                                reply_service, reply_item = await claim_permission_reply(factory=app_user_sessionmaker,
                                    tenant_id=seed["tenant_id"], notice_id=notices[0].id, text=text,
                                    provider_reference=provider_reference)
                                rejected = await dispatch_permission_rejection(factory=app_user_sessionmaker, tenant_id=seed["tenant_id"],
                                    notice_id=notices[0].id, item=reply_item, exact_work=changed not in {"binding", "receipt", "scope"},
                                    unknown_delivery=changed == "receipt",
                                    before_replay=(lambda: soul.write_bytes(original_soul)) if changed == "source" else None)
                                assert rejected.get("permission_status") == ("needs_reconciliation" if changed in {"receipt", "scope"} else "rejected"), (changed, rejected)
                                assert not permission_calls and len(client.requests) == 1
                                assert not (seed["data_root"] / str(receiver) / report_path).exists()
                                async with app_user_sessionmaker() as db:
                                    await pin_rls_tenant_context(db, seed["tenant_id"])
                                    assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"]))) == runs_before_quote
                                assert await reply_service._mark_processed(item=reply_item, worker_id="permission-quote", receipt=rejected)
                            finally:
                                soul.write_bytes(original_soul)
                                async with app_user_sessionmaker() as db:
                                    await pin_rls_tenant_context(db, seed["tenant_id"])
                                    restored = await db.get(ChannelDeliveryOutbox, notices[0].id)
                                    restored.text_content, restored.metadata_json, restored.delivery_receipts_json = saved_text, saved_meta, saved_receipts
                                    (await db.get(ExternalPrincipal, restored.external_principal_id)).linked_user_id = linked
                                    await db.commit()
                    quoted = await resolve_permission_reply(factory=app_user_sessionmaker,
                        tenant_id=seed["tenant_id"], notice_id=notices[0].id,
                        text="/deny" if decision == "deny" else "/allow")
                    assert quoted["permission_receipt"]["run_id"] == str(task_id)
                    assert quoted["permission_receipt"]["run_status"] == "resumable" and len(client.requests) == 1
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"]))) == runs_before_quote
                try:
                    response = await api.post(url, json={"action": "deny" if decision == "deny" else "allow_once"})
                finally:
                    soul.write_bytes(original_soul)
                assert response.status_code == 200, response.text
                receipt = response.json()
                assert receipt["run_id"] == str(task_id) and receipt["run_status"] == "resumable", receipt
                replay = await api.post(url, json={"action": "deny" if decision == "deny" else "allow_once"})
                assert replay.status_code == 200 and replay.json()["result_event_id"] == receipt["result_event_id"], replay.text
            if first_task:
                settled = await api.get(draft_url)
                assert settled.status_code == 200 and settled.json()["first_task_work"]["permissions"] == [], settled.text
        if decision == "allow":
            assert (seed["data_root"] / str(receiver) / report_path).read_text() == "DELEGATED_REPORT"
            assert len(permission_calls) == 1
            frame = permission_calls[0]["authority_frame"]
            assert frame.principal.source_agent_id == receiver and frame.principal.requester_user_id == user.id
            assert frame.parent_session_id == str(seed["session_id"]) and UUID(frame.runtime_task_id) == task_id
            from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
            from app.tools.runtime import ToolExecutionRequest
            from app.services.session_model_inputs import _model_dependencies

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                source_model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == permission_provider_id))
                sources, complete = _model_dependencies(source_model)
                assert complete
                parent_sources = [source for source in sources if source["source_kind"] == "agent_context_resource"
                    and source["source_id"].startswith(str(seed["agent_id"]) + "\n")]
                if not first_task:
                    parent_sources = [source for source in sources if source["source_kind"] == "session_command"]
                assert parent_sources
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="write_file",
                        arguments={"path": report_path, "content": "DELEGATED_REPORT"}, context=observed["context"]), parent_sources)
                assert denied.value.status_code == 403
            from tests.integration.test_delegation_permission_binding import verify_delegated_permission_binding

            await verify_delegated_permission_binding(owner=owner_sessionmaker, factory=app_user_sessionmaker,
                tenant_id=seed["tenant_id"], session_id=request.session_id, control_id=UUID(receipt["control_id"]),
                parent_agent_id=seed["agent_id"], url=migrated_pg_url, tmp_path=tmp_path)
        else:
            assert not (seed["data_root"] / str(receiver) / report_path).exists()
        # An already committed response wins over a late worker's pause boundary.
        token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
        try:
            await suspend_delegation_for_question(request, provider_request_id=permission_provider_id)
        finally:
            reset_runtime_task_fence(token)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            invocation_row = await db.get(SessionToolInvocation, invocation_id)
            assert invocation_row.effect_state == ("effect_committed" if decision == "allow" else "prepared_not_started")
            assert invocation_row.result_event_id is not None
            if decision == "source_changed":
                from app.models.chat_transcript_event import ChatTranscriptEvent
                from app.services.session_model_inputs import permission_abort_result_bound
                from app.services.session_semantic_history import _event_payload

                event = await db.get(ChatTranscriptEvent, invocation_row.result_event_id)
                content = _event_payload(event)["content"]
                assert await permission_abort_result_bound(db, invocation=invocation_row, event=event, content=content)
                forged = {**json.loads(content), "content": "UNATTESTED_WORKSPACE_BYTES"}
                assert not await permission_abort_result_bound(db, invocation=invocation_row, event=event, content=json.dumps(forged))
            assert (await db.get(RuntimeTask, task_id)).status == "resumable" and len(client.requests) == 1
            claimed = await RuntimeTaskClaimService(db=db, worker_id="permission-resume", task_types=("delegation",)).claim_available(batch_size=10)
            assert [row.id for row in claimed] == [task_id]
            version = claimed[0].claim_version
        token = set_runtime_task_fence(task_id=task_id, worker_id="permission-resume", claim_version=version)
        try:
            assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
        finally:
            reset_runtime_task_fence(token)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(RuntimeTask, task_id)
            assert child.status == "completed" and child.result_summary == expected_summary, child.metadata_json
            assert child.child_agent_id == receiver and child.child_session_id == request.session_id
            assert len(client.requests) == 2
            assert len(list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task_id)))) == 1
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task_id)))
            assert len(models) == 2 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in models)
        from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
        from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
        from app.services.web_chat_runtime import execute_web_chat_run

        processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        async def terminal_with_diagnostics(item):
            try:
                return await processor(item)
            except Exception as exc:
                (tmp_path / "permission-terminal-failure.json").write_text(json.dumps({"type": type(exc).__name__, "message": str(exc)}))
                raise
        drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="permission-result-terminal", canonical_validator=processor.validate,
            process_callback=terminal_with_diagnostics, task_types=("delegation",))
        assert drained["delivered"] == 1, drained
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="permission-result-return", limit=100)
        pages = await service.prepare_integration_pages(worker_id="permission-result-return",
            claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
        assert len(pages) == 1
        delivered = await service._deliver_page(pages[0])
        assert delivered.get("ok") is True, delivered
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            notification = await db.scalar(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(task_id), RuntimeNotificationOutbox.source_kind == "a2a_delegation"))
            continuation = await db.scalar(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
                RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))
            assert continuation is not None and continuation.parent_session_id == str(seed["session_id"])
            return_id = continuation.id
            result_ref = notification.result_ref
        if not first_task:
            from app.models.runtime_result import RuntimeResultObject
            from app.services.content_input_receipts import ContentInputReceiptError
            from app.services.runtime_result_sources import validate_runtime_result_sources
            from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

            source_request = ToolExecutionRequest(tool_name="read_runtime_result", arguments={"result_ref": result_ref},
                context=ToolExecutionContext(agent_id=seed["agent_id"], user_id=seed["user_id"],
                    tenant_id=seed["tenant_id"], workspace=seed["agent_root"], session_id=str(seed["session_id"]),
                    runtime_task_id=str(seed["run_id"])))
            async def read_sources():
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    result_object = await db.get(RuntimeResultObject, UUID(result_ref.split("/")[2]))
                    return await validate_runtime_result_sources(db, source_request, result_object)
            assert await read_sources()
            async with owner_sessionmaker() as db:
                (await db.get(Agent, receiver)).owner_user_id = other.id
                await db.commit()
            try:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await read_sources()
                assert denied.value.status_code == 403
            finally:
                async with owner_sessionmaker() as db:
                    (await db.get(Agent, receiver)).owner_user_id = seed["user_id"]
                    await db.commit()
            soul = seed["data_root"] / str(receiver) / "soul.md"
            original_soul = soul.read_bytes()
            try:
                soul.write_bytes(original_soul + b"\nChanged before consuming the returned result.\n")
                with pytest.raises(ContentInputReceiptError) as denied:
                    await read_sources()
                assert denied.value.status_code == 403
            finally:
                soul.write_bytes(original_soul)
            assert await read_sources() and len(client.requests) == 2 and len(permission_calls) == 1
        parent_client = ScriptedLLMClient([
            {"tool_calls": [{"id": "read-permission-result", "type": "function", "function": {"name": "read_runtime_result",
                "arguments": json.dumps({"result_ref": result_ref})}}], "finish_reason": "tool_calls"},
            {"content": "Reviewed the generated report.", "finish_reason": "stop"},
        ])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: parent_client)
        async with owner_sessionmaker() as db:
            continuation = await db.get(RuntimeTask, return_id)
            continuation.status, continuation.claimed_by, continuation.claim_version = "running", "permission-return-worker", 1
            continuation.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        parent_version = 1
        token = set_runtime_task_fence(task_id=return_id, worker_id="permission-return-worker", claim_version=parent_version)
        try:
            await execute_web_chat_run(return_id)
        finally:
            reset_runtime_task_fence(token)
        assert len(parent_client.requests) == 2
        returned = json.loads(next(message["content"] for message in parent_client.requests[1]["messages"]
            if message.get("tool_call_id") == "read-permission-result"))
        assert returned["status"] == "ok" and expected_summary in returned["content"], returned
        assert returned["dependency_coverage"]["complete"] is True
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (await db.get(RuntimeTask, return_id)).status == "completed"
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)))
            assert len(models) == 2
            assert all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in models)
        return
    if feishu_creation or input_case in {"first_task_question", "first_task_question_after_write"}:
        from app.models.chat_transcript_event import ChatTranscriptEvent

        client = ScriptedLLMClient([{"tool_calls": [{"id": "child-question", "type": "function", "function": {
            "name": "ask_user_question", "arguments": json.dumps({"questions": [{"question": "Which audience?",
                "header": "Audience", "options": [{"label": "Team"}, {"label": "Personal"}]}]})}}],
            "finish_reason": "tool_calls"}])
        question_calls = 2 if question_after_write else 1
        if question_after_write:
            client.script.insert(0, {"tool_calls": [{"id": "before-question-write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": report_path, "content": "DELEGATED_REPORT"})}}],
                "finish_reason": "tool_calls"})
        token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
        try:
            assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
        finally:
            reset_runtime_task_fence(token)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(RuntimeTask, task_id)
            questions = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == task_id, ChatTranscriptEvent.item_kind == "user_question")))
            assert len(questions) == 1 and questions[0].lifecycle == "waiting", (child.status, child.result_summary, child.metadata_json)
            assert child.status == "suspended", (child.status, child.result_summary, child.metadata_json)
            assert child.completed_at is None
            assert len(client.requests) == question_calls
            question_id = questions[0].item_id
            question_provider_id = (await db.get(SessionModelResult, questions[0].result_id)).provider_request_id
            if feishu_creation:
                from app.models.channel_delivery_outbox import ChannelDeliveryOutbox

                notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
                    ChannelDeliveryOutbox.runtime_task_id == task_id,
                    ChannelDeliveryOutbox.delivery_kind == "interactive_prompt")))
                assert len(notices) == 1, "The originating Feishu work has no durable pending-question notice"
                assert notices[0].session_id == seed["session_id"]
                assert notices[0].external_principal_id == seed["feishu_principal_id"]
                assert notices[0].terminal_status == "clarification_required" and notices[0].artifact_ids_json == []
                assert notices[0].prompt_key == str(question_id)
                assert f"/agents/{seed['agent_id']}?session_id={seed['session_id']}#chat" in notices[0].text_content
                assert "Which audience?" in notices[0].text_content
                assert "- Team" in notices[0].text_content and "- Personal" in notices[0].text_content
            if question_after_write:
                from app.runtime.session import SessionContext
                from app.services.delegation_context_sources import bind_native_recovery_context

                restored = SessionContext(session_id=request.session_id)
                await bind_native_recovery_context(db, task=child, session=restored)
                assert restored.metadata.get("_native_recovery_context_source") is not None
                for overrides in [{"root_user_id": uuid4()}, {"child_agent_id": uuid4()},
                    {"metadata_json": {**child.metadata_json, "native_recovery_context_source": {
                        **child.metadata_json["native_recovery_context_source"], "hash": "0" * 64}}}]:
                    altered = SimpleNamespace(**{key: getattr(child, key) for key in (
                        "id", "tenant_id", "child_agent_id", "root_user_id", "child_session_id", "metadata_json")})
                    for key, value in overrides.items():
                        setattr(altered, key, value)
                    await bind_native_recovery_context(db, task=altered, session=restored)
                    assert "_native_recovery_context_source" not in restored.metadata
        if feishu_creation:
            from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
            from app.services.feishu_service import FeishuService

            sent_notices = []
            async def send_notice(_self, _app_id, _secret, receive_id, message_type, content, **kwargs):
                notice_text = json.loads(content)["text"]
                if "此提示不代表任务完成" in notice_text:
                    sent_notices.append({"target": receive_id, "target_type": kwargs["receive_id_type"],
                        "message_type": message_type, "text": notice_text})
                return {"data": {"message_id": f"synthetic-question-notice-{len(sent_notices)}"}}
            monkeypatch.setattr(FeishuService, "send_message", send_notice)
            notice_service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
            @asynccontextmanager
            async def notice_session(_operation):
                # This journey owns one tenant; unrelated fixtures keep their pending deliveries.
                from app.database import tenant_scoped_session

                async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as scoped:
                    yield scoped
            monkeypatch.setattr(notice_service, "_worker_session", notice_session)
            if input_case in {"first_task_feishu_question_source_changed", "first_task_feishu_question_body_changed",
                "first_task_feishu_question_already_answered", "first_task_feishu_question_projection_missing",
                "first_task_feishu_question_recipient_changed"}:
                from app.api.chat_sessions import SessionHumanInputIn, submit_session_human_input

                soul = seed["data_root"] / str(receiver) / "soul.md"
                original_soul = soul.read_bytes()
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    if input_case.endswith("body_changed"):
                        saved = await db.get(ChannelDeliveryOutbox, notices[0].id)
                        saved.text_content += "\nDifferent question text."
                    elif input_case.endswith("projection_missing"):
                        saved = await db.get(ChannelDeliveryOutbox, notices[0].id)
                        saved.metadata_json = {k: v for k, v in saved.metadata_json.items() if k != "projection_kind"}
                    elif input_case.endswith("recipient_changed"):
                        saved = await db.get(ChannelDeliveryOutbox, notices[0].id)
                        saved.delivery_target_json = {**saved.delivery_target_json, "receive_id": "ou_another_recipient"}
                    elif input_case.endswith("already_answered"):
                        answer_id = uuid4()
                        admitted = await submit_session_human_input(agent_id=receiver, session_id=UUID(request.session_id),
                            current_user=user, db=db, body=SessionHumanInputIn(kind="answer_request", input_id=answer_id,
                                idempotency_key=str(answer_id), request_item_id=question_id,
                                content_parts=[{"type": "text", "text": "Audience: Team"}],
                                expected_run_id=task_id, terminal_fallback="reject"))
                        assert admitted["status"] == "queued", admitted
                    await db.commit()
                if input_case.endswith("source_changed"):
                    soul.write_bytes(original_soul + b"\nChanged before channel release.\n")
                try:
                    denied = await notice_service.drain_once(worker_id="question-release-denial")
                    assert denied["dead_lettered"] == 1 and sent_notices == [], denied
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        assert (await db.get(ChannelDeliveryOutbox, notices[0].id)).status == "dead_letter"
                    assert len(client.requests) == question_calls
                finally:
                    soul.write_bytes(original_soul)
                return
            if input_case.endswith("legacy_status"):
                from app.models.hr_creation import HrCreationDraft
                from app.services.employee_first_task import first_task_question_text

                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    saved = await db.get(ChannelDeliveryOutbox, notices[0].id)
                    draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
                    saved.metadata_json = {k: v for k, v in saved.metadata_json.items() if k != "projection_kind"}
                    saved.text_content = first_task_question_text(draft)
                    await db.commit()
                assert (await notice_service.drain_once(worker_id="legacy-status"))["delivered"] >= 1
                assert len(sent_notices) == 1 and "Which audience?" not in sent_notices[0]["text"]
                assert sent_notices[0]["target"] == "ou_creation_owner"
                return
            assert (await notice_service.drain_once(worker_id="question-notice"))["delivered"] >= 1
            assert (await notice_service.drain_once(worker_id="question-notice-replay"))["delivered"] == 0
            assert len(sent_notices) == 1 and sent_notices[0]["target"] == "ou_creation_owner"
            assert sent_notices[0]["target_type"] == "open_id" and "Which audience?" in sent_notices[0]["text"]
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                assert (await db.get(RuntimeTask, task_id)).status == "suspended"
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api.chat_sessions import router
        from app.core.security import get_current_user
        from app.database import get_db

        async def answer_db():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                yield db
        app = FastAPI()
        app.include_router(router, prefix="/api")
        from app.api.hr_creation import router as creation_router

        app.include_router(creation_router, prefix="/api")
        app.dependency_overrides[get_db] = answer_db
        app.dependency_overrides[get_current_user] = lambda: user
        answer_id = uuid4()
        body = {"kind": "answer_request", "input_id": str(answer_id), "idempotency_key": str(answer_id),
            "request_item_id": str(question_id), "content_parts": [{"type": "text", "text": "Audience: Team"}]}
        async def dispatch_answer(response, revision=1):
            from app.services.session_input_dispatch import recover_admitted_session_inputs_once

            assert response.status_code == 200 and response.json()["admission_state"] == "admitted", response.text
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                await recover_admitted_session_inputs_once(db, worker_id="answer-dispatch", tenant_id=seed["tenant_id"],
                    admission_ids=(uuid5(UUID(response.json()["command_id"]), f"input-admission:revision:{revision}"),))
                assert (await db.get(SessionTurnInput, answer_id)).status == "queued"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://question.local") as browser:
            draft_url = f"/api/agents/{seed['agent_id']}/hr-creation-drafts/{creation['blueprint_id']}"
            original = await browser.get(draft_url)
            assert original.status_code == 200, original.text
            work = original.json()["first_task_work"]
            assert work["status"] == "suspended" and work["agent_id"] == str(receiver)
            assert work["session_id"] == request.session_id and work["question"]["item_id"] == str(question_id)
            assert work["question"]["payload"]["questions"][0]["question"] == "Which audience?"
            assert work["can_answer"] is True
            if feishu_creation:
                (tmp_path / "original-work-first-task.json").write_text(json.dumps(original.json(), indent=2))
            other = SimpleNamespace(id=uuid4(), tenant_id=user.tenant_id, role=user.role,
                is_active=True, department_id=user.department_id)
            app.dependency_overrides[get_current_user] = lambda: other
            try:
                denied = await browser.get(draft_url)
                assert denied.status_code in {403, 404} and "Which audience?" not in denied.text
            finally:
                app.dependency_overrides[get_current_user] = lambda: user
            soul = seed["data_root"] / str(receiver) / "soul.md"
            original_soul = soul.read_bytes()
            try:
                soul.write_bytes(original_soul + b"\nChanged after the question.\n")
                unavailable = (await browser.get(draft_url)).json()["first_task_work"]
                assert unavailable["status"] == "unavailable" and unavailable["question"] is None
                assert unavailable["can_answer"] is False
            finally:
                soul.write_bytes(original_soul)
            ordinary = await browser.post(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs", json={
                "kind": "start_turn", "input_id": str(uuid4()), "idempotency_key": str(uuid4()),
                "content_parts": [{"type": "text", "text": "Unrelated work"}]})
            assert ordinary.status_code == 409 and ordinary.json()["detail"]["code"] == "session_read_only"
            response = await browser.post(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs", json=body)
            await dispatch_answer(response)
            replay = await browser.post(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs", json=body)
            assert replay.status_code == 200 and replay.json()["replayed"] is True, replay.text
            revised = await browser.patch(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs/{answer_id}",
                json={"content_parts": [{"type": "text", "text": "Audience: Personal"}]}, headers={"If-Match": "1"})
            await dispatch_answer(revised, revision=2)
            cancelled = await browser.delete(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs/{answer_id}",
                headers={"If-Match": "2"})
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                child = await db.get(RuntimeTask, task_id)
                assert child.status == "suspended" and len(client.requests) == question_calls
                question_id = UUID(child.metadata_json["delegation_waiting_question_item_id"])
            reopened = (await browser.get(draft_url)).json()["first_task_work"]
            assert reopened["question"]["item_id"] == str(question_id) != work["question"]["item_id"]
            if feishu_creation:
                assert (await notice_service.drain_once(worker_id="cancelled-answer-notice"))["delivered"] == 1
                assert len(sent_notices) == 2 and all("Which audience?" in row["text"] for row in sent_notices)
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    notices = list(await db.scalars(select(ChannelDeliveryOutbox).where(
                        ChannelDeliveryOutbox.runtime_task_id == task_id,
                        ChannelDeliveryOutbox.delivery_kind == "interactive_prompt")))
                    assert len(notices) == 2 and {row.prompt_key for row in notices} == {
                        work["question"]["item_id"], str(question_id)}
                    assert all(row.status == "delivered" for row in notices)
            answer_id = uuid4()
            body.update(input_id=str(answer_id), idempotency_key=str(answer_id), request_item_id=str(question_id))
            if input_case == "first_task_feishu_quoted_question":
                from app.models.audit import ChatMessage
                from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
                from app.models.channel_config import ChannelConfig
                from app.services.channel_ingress_inbox import ChannelIngressInboxService, accept_authenticated_channel_event
                from app.services.channel_provider_receipts import feishu_message_scope
                from app.services.session_input_dispatch import recover_admitted_session_inputs_once

                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    runs_before_reply = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"])))
                    notice = next(row for row in notices if row.prompt_key == str(question_id))
                reply_service = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
                monkeypatch.setattr(reply_service, "_worker_session", notice_session)

                async def claim_reply():
                    reply = {"header": {"event_type": "im.message.receive_v1", "event_id": uuid4().hex},
                        "event": {"sender": {"sender_id": {"open_id": "ou_creation_owner"}},
                            "message": {"message_type": "text", "chat_type": "p2p", "message_id": uuid4().hex,
                                "parent_id": "synthetic-question-notice-2", "content": json.dumps({"text": "Audience: Team"})}}}
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        config = await db.get(ChannelConfig, notice.channel_config_id)
                        assert config is not None
                        admitted = await accept_authenticated_channel_event(db,
                            tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], provider="feishu",
                            installation_ref=str(notice.channel_config_id), provider_event_id=reply["header"]["event_id"],
                            handler_key="feishu.event", body=reply,
                            metadata={"feishu_message_scope": feishu_message_scope(config)})
                        await db.commit()
                    items = await reply_service.claim_batch(worker_id="first-task-answer", limit=10)
                    assert [item.id for item in items] == [admitted.event_id]
                    return items[0]

                for changed in ("draft", "source", "receipt"):
                    from app.models.hr_creation import HrCreationDraft

                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
                        draft_agent = draft.created_agent_id
                        original_receipts = notice.delivery_receipts_json
                        if changed == "draft":
                            draft.created_agent_id = seed["agent_id"]
                        elif changed == "receipt":
                            current_notice = await db.get(ChannelDeliveryOutbox, notice.id)
                            current_notice.delivery_receipts_json = {**original_receipts, "text": {
                                **original_receipts["text"], "detail": {k: v for k, v in original_receipts["text"]["detail"].items()
                                    if k != "provider_message_id"}}}
                        await db.commit()
                    if changed == "source":
                        soul.write_bytes(original_soul + b"\nChanged before the quoted answer.\n")
                    rejected_reply = await claim_reply()
                    try:
                        rejected = await dispatch_channel_ingress_event(rejected_reply)
                        assert rejected.get("answer_status") == ("needs_reconciliation" if changed == "receipt" else "rejected"), (changed, rejected)
                        async with app_user_sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            assert await db.get(SessionTurnInput, rejected_reply.id) is None
                            assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"]))) == runs_before_reply
                        assert len(client.requests) == question_calls
                        assert await reply_service._mark_processed(item=rejected_reply, worker_id="first-task-answer", receipt=rejected)
                    finally:
                        soul.write_bytes(original_soul)
                        async with app_user_sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            (await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))).created_agent_id = draft_agent
                            (await db.get(ChannelDeliveryOutbox, notice.id)).delivery_receipts_json = original_receipts
                            await db.commit()
                replies = [await claim_reply()]
                answer_id = replies[0].id
                for _attempt in range(2):
                    reply_receipt = await dispatch_channel_ingress_event(replies[0])
                    assert reply_receipt.get("runtime_task_id") == str(task_id), reply_receipt
                    assert reply_receipt["session_id"] == str(seed["session_id"])
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        row = await db.get(SessionTurnInput, answer_id)
                        assert row.intent == "answer_request" and row.target_run_id == task_id
                        assert row.session_id == UUID(request.session_id) and row.request_item_id == question_id
                        await recover_admitted_session_inputs_once(db, worker_id="quoted-answer-dispatch", tenant_id=seed["tenant_id"],
                            admission_ids=(uuid5(row.command_id, "input-admission:revision:1"),))
                        assert row.status == "queued"
                        assert await db.scalar(select(ChatMessage.id).where(ChatMessage.source_ingress_event_id == answer_id)) is None
                        assert set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"]))) == runs_before_reply
                    assert len(client.requests) == question_calls
            else:
                response = await browser.post(f"/api/agents/{receiver}/sessions/{request.session_id}/inputs", json=body)
                await dispatch_answer(response)
            answered = (await browser.get(draft_url)).json()["first_task_work"]
            assert answered["question"] is None and answered["runtime_task_id"] == str(task_id)
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, task_id)
            assert child.status == "pending" and child.completed_at is None
            child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "answer-worker", 2, 2
            child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        from app.services.delegation_session_runtime import suspend_delegation_for_question

        token = set_runtime_task_fence(task_id=task_id, worker_id="answer-worker", claim_version=2)
        try:
            await suspend_delegation_for_question(request, provider_request_id=question_provider_id)
        finally:
            reset_runtime_task_fence(token)
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, task_id)
            assert child.status == "pending" and child.completed_at is None
            child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "answer-worker", 3, 3
            child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        client.script.append({"content": "Report prepared for Team.", "finish_reason": "stop"})
        token = set_runtime_task_fence(task_id=task_id, worker_id="answer-worker", claim_version=3)
        try:
            assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
        finally:
            reset_runtime_task_fence(token)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(RuntimeTask, task_id)
            assert child.status == "completed" and child.result_summary == "Report prepared for Team.", child.metadata_json
            assert len(client.requests) == question_calls + 1
            assert any("Audience: Team" in str(message.get("content")) for message in client.requests[-1]["messages"])
            if question_after_write:
                recovered_context = json.dumps(client.requests[-1]["messages"])
                assert "### Recent Writes" in recovered_context and "Wrote workspace/delegated-report.md" in recovered_context
                assert "Runtime File Change Notice" not in recovered_context
            from app.models.session_v2 import SessionToolInvocation
            tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task_id)))
            assert len(tools) == question_calls
            question_tool = next(tool for tool in tools if tool.tool_name == "ask_user_question")
            assert (await db.get(SessionTurnInput, answer_id)).status == "applied"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://question.local") as browser:
            completed = (await browser.get(draft_url)).json()["first_task_work"]
            assert completed["status"] == "completed" and completed["question"] is None
        if input_case == "first_task_feishu_quoted_question":
            replay = await dispatch_channel_ingress_event(replies[0])
            assert replay.get("runtime_task_id") == str(task_id) and len(client.requests) == question_calls + 1, replay
            assert await reply_service._mark_processed(item=replies[0], worker_id="first-task-answer", receipt=replay)
        from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
        from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
        from app.services.web_chat_runtime import execute_web_chat_run

        processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="question-result-terminal", canonical_validator=processor.validate,
            process_callback=processor, task_types=("delegation",))
        assert drained["delivered"] == 1, drained
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="question-result-return", limit=100)
        pages = await service.prepare_integration_pages(worker_id="question-result-return",
            claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
        assert len(pages) == 1
        delivered = await service._deliver_page(pages[0])
        assert delivered.get("ok") is True, delivered
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            notification = await db.scalar(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(task_id), RuntimeNotificationOutbox.source_kind == "a2a_delegation"))
            continuation = await db.scalar(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
                RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))
            assert continuation is not None and continuation.parent_session_id == str(seed["session_id"])
            return_id, result_ref = continuation.id, notification.result_ref
            from app.models.runtime_result import RuntimeResultObject
            from app.services.runtime_result_sources import load_canonical_result_sources

            result_object = await db.get(RuntimeResultObject, UUID(result_ref.split("/")[2]))
            canonical_sources = await load_canonical_result_sources(db, result_object)
            assert canonical_sources
        from app.services import runtime_result_sources
        validate_sources = runtime_result_sources._validate_input_dependencies
        async def observe_return_sources(*args, **kwargs):
            if kwargs.get("runtime_request") is not None:
                observed["return_request"] = kwargs["runtime_request"]
            return await validate_sources(*args, **kwargs)
        monkeypatch.setattr(runtime_result_sources, "_validate_input_dependencies", observe_return_sources)
        parent_client = ScriptedLLMClient([
            {"tool_calls": [{"id": "read-answered-result", "type": "function", "function": {"name": "read_runtime_result",
                "arguments": json.dumps({"result_ref": result_ref})}}], "finish_reason": "tool_calls"},
            {"content": "Reviewed the report prepared for Team.", "finish_reason": "stop"},
        ])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: parent_client)
        async with owner_sessionmaker() as db:
            continuation = await db.get(RuntimeTask, return_id)
            continuation.status, continuation.claimed_by, continuation.claim_version = "running", "question-return-worker", 1
            continuation.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        token = set_runtime_task_fence(task_id=return_id, worker_id="question-return-worker", claim_version=1)
        try:
            await execute_web_chat_run(return_id)
        finally:
            reset_runtime_task_fence(token)
        (tmp_path / "answered-parent-model-requests.json").write_text(json.dumps(parent_client.requests, default=str, indent=2))
        assert len(parent_client.requests) == 2
        returned = json.loads(next(message["content"] for message in parent_client.requests[1]["messages"]
            if message.get("tool_call_id") == "read-answered-result"))
        assert returned["status"] == "ok" and "Report prepared for Team." in returned["content"], returned
        assert returned["dependency_coverage"]["complete"] is True
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (await db.get(RuntimeTask, return_id)).status == "completed"
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)))
            assert len(models) == 2 and all(model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]
                for model in models)
            from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
            private = [ref for ref in canonical_sources[1] if ref["source_kind"] == "session_tool_invocation"
                and ref["source_id"] == str(question_tool.id)]
            answer = await db.get(SessionTurnInput, answer_id)
            private.extend(ref for ref in canonical_sources[1] if ref["source_kind"] == "session_command"
                and ref["source_id"] == str(answer.command_id))
            assert len(private) == 2
            for dependency in private:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, observed["return_request"], [dependency])
                assert denied.value.code == "content_source_access_denied"
        return
    token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
    try:
        result = await orchestrator._delegate_after_cycle_check(request, trace_id=request.trace_id or "origin-probe",
            child_session_id=request.session_id, tool_profile=orchestrator._resolve_delegation_tool_profile(request.policy.tool_profile),
            is_delegation=True)
    finally:
        reset_runtime_task_fence(token)
    if unbound_input or revoked_input:
        expected = "delegation_instruction_sources_unbound" if unbound_input else "delegation_instruction_source_access_denied"
        assert result.failed and result.terminal_reason == expected, result
        assert result.parts[0]["status"] == ("unavailable" if unbound_input else "denied")
        assert not client.requests and not (seed["data_root"] / str(receiver) / report_path).exists()
        from app.models.chat_transcript_event import ChatTranscriptEvent
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            origin = await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
                content=message["content"])
            assert origin is not None and bool(origin.unbound) is (not parent_complete) and origin.sources
            assert not list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task_id)))
            assert not list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id,
                ChatTranscriptEvent.event_type == "user_message")))
        return
    if child_readback_case:
        if child_missing_file_case:
            assert not result.failed and result.transcript_committed and len(client.requests) == 4, result
        else:
            assert not result.failed and result.transcript_committed and len(client.requests) == 3, result
        readback = next(m["content"] for m in client.requests[3 if child_missing_file_case else 2]["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == "child-report-read")
        assert "DELEGATED_REPORT" in readback and "<tool_error>" not in readback, readback
    else:
        assert not result.failed and result.transcript_committed and len(client.requests) == 2, result
    (tmp_path / "native-child-tool-results.json").write_text(json.dumps(client.requests, indent=2, default=str))
    assert (seed["data_root"] / str(receiver) / report_path).exists(), [m for m in client.requests[-1]["messages"] if m.get("role") == "tool"]
    assert (seed["data_root"] / str(receiver) / report_path).read_text() == "DELEGATED_REPORT"
    artifact_id = None
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        origin = await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
            content=message["content"])
        assert origin is not None and bool(origin.unbound) is (not parent_complete)
        command = (await db.get(SessionCommand, creation_command_id) if first_task else
            await db.scalar(select(SessionCommand).where(SessionCommand.session_id == seed["session_id"])))
        command_source = {"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}
        if native_creation:
            from app.services.session_model_inputs import _model_dependencies

            creating_model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.provider_request_id == invocation.provider_request_id))
            creating_sources, complete = _model_dependencies(creating_model)
            assert complete and list(origin.sources) == creating_sources and command_source in creating_sources
            assert {ref["source_kind"] for ref in creating_sources} >= {"agent_identity", "agent_context_resource", "session_actor_identity"}
        else:
            assert origin.sources == (command_source,)
        if first_task:
            from app.models.config_revision import ConfigRevision

            genesis = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_id == receiver,
                ConfigRevision.entity_type == "agent_hr_bootstrap"))
            assert genesis.changed_by_user_id is None and genesis.changed_by_agent_id == seed["agent_id"]
            assert genesis.content["dependencies"] == list(origin.sources)
            assert genesis.content["unbound"] == (["hr_refinement_sources_unbound"] if refined_input else [])
            if native_refined:
                captured = genesis.content["refinement"]["call"]
                assert captured["request"] == {key: refinement_calls[0][key] for key in captured["request"]}
                assert "REFINED_CONTEXT" in captured["response_content"] and "api_key" not in json.dumps(captured)
            assert not await db.scalar(select(ConfigRevision.id).where(ConfigRevision.entity_id == receiver,
                ConfigRevision.entity_type == "agent_bootstrap"))
        child_model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == task_id)
            .order_by(SessionModelResult.round_id))
        bindings = child_model.model_request_snapshot_json["input_bindings"]
        from app.models.invocation_span import InvocationSpan
        sources = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == task_id,
            InvocationSpan.span_type == "kernel_context")))
        (tmp_path / "child-source-coverage.json").write_text(json.dumps([
            {"kind": row.metadata_json["context_origin"]["kind"],
                "coverage": row.metadata_json["context_origin"]["dependency_coverage"]} for row in sources], indent=2))
        (tmp_path / "delegated-input-bindings.json").write_text(json.dumps(bindings, indent=2))
        assert bindings["status"] == "resolved" and all(entry["status"] == "bound" for entry in bindings["messages"]), bindings
        assert bindings["dependency_coverage"]["complete"] is parent_complete
        assert dict(origin.sources[0]) in bindings["dependencies"]
        from app.models.organization_identity import ContentInputReceipt
        receipt = await db.scalar(select(ContentInputReceipt).where(
            ContentInputReceipt.carrier_ref == f"{receiver}\n{report_path}"))
        (tmp_path / "native-receipt-status.json").write_text(json.dumps(observed.get("status"), indent=2, default=str))
        from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
        from app.tools.runtime import ToolExecutionRequest
        errors = []
        if parent_complete:
            for dependency in bindings["dependencies"]:
                try:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=observed["context"]), [dependency])
                except ContentInputReceiptError as exc:
                    errors.append({"source": dependency, "code": exc.code, "message": str(exc)})
        (tmp_path / "input-source-errors.json").write_text(json.dumps(errors, indent=2))
        if refined_input:
            assert receipt is None and errors and all(error["code"] == "content_source_unavailable" for error in errors)
            assert observed["status"]["status"] == "unavailable"
            return
        assert (receipt is not None) is parent_complete, (observed.get("status"), errors)
        if glob_case:
            from copy import deepcopy
            from app.services.session_model_inputs import _origin_dependencies

            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task_id)))
            assert len(models) == 3 and all(model.model_request_snapshot_json["input_bindings"][
                "dependency_coverage"]["complete"] for model in models)
            glob_calls = list(await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == task_id, SessionToolInvocation.tool_name == "glob_search")
                .order_by(SessionToolInvocation.provider_tool_use_id)))
            assert len(glob_calls) == 2
            for index, call in enumerate(glob_calls):
                event = await db.get(ChatTranscriptEvent, call.result_event_id)
                payload = _event_payload(event)
                assert ("No files matched" in payload["content"]) is (index == 0)
                pointer = payload["metadata"]["native_directory_read_receipt"]
                assert pointer["status"] == "verified"
                evidence_id = UUID(pointer["evidence_ref"].split(":", 1)[1])
                captured = await db.get(InvocationSpan, evidence_id)
                original_proof = deepcopy(captured.metadata_json)
                ref = f"session-tool-result:{call.id}"
                check = {"tenant_id": seed["tenant_id"], "agent_id": receiver, "ref": ref}
                assert (await _origin_dependencies(db, **check))[1]
                async with owner_sessionmaker() as writer:
                    proof = await writer.get(InvocationSpan, evidence_id)
                    proof.metadata_json = {"native_directory_read_evidence": {
                        **original_proof["native_directory_read_evidence"], "tool_result_hash": "0" * 64}}
                    await writer.commit()
                assert not (await _origin_dependencies(db, **check))[1]
                async with owner_sessionmaker() as writer:
                    (await writer.get(InvocationSpan, evidence_id)).metadata_json = original_proof
                    await writer.commit()
                assert (await _origin_dependencies(db, **check))[1]
        if creator_task_inventory_case:
            from app.models.invocation_span import InvocationSpan
            from app.services.employee_creation_sources import _owned_context_source

            call = await db.scalar(select(SessionToolInvocation).where(
                SessionToolInvocation.session_id == seed["session_id"],
                SessionToolInvocation.provider_tool_use_id == "creator-task-inventory"))
            source = next(item for item in origin.sources if item["source_kind"] == "session_tool_invocation"
                and item["source_id"] == str(call.id))
            scope = {"agent_id": seed["agent_id"], "tenant_id": seed["tenant_id"],
                "session_id": seed["session_id"], "user_id": seed["user_id"]}
            assert await _owned_context_source(db, dependency=source, **scope)
            assert not await _owned_context_source(db, dependency={**source, "source_version": "0" * 64}, **scope)
            assert not await _owned_context_source(db, dependency=source, **{**scope, "session_id": uuid4()})
            control = await db.begin_nested()
            proof = await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read"))
            proof.metadata_json = {**proof.metadata_json, "runtime_task_read": {
                **proof.metadata_json["runtime_task_read"], "unbound": ["task_read_sources_unbound"]}}
            await db.flush()
            assert not await _owned_context_source(db, dependency=source, **scope)
            await control.rollback()
            assert await _owned_context_source(db, dependency=source, **scope)
        if receipt is not None:
            assert dict(origin.sources[0]) in receipt.dependencies_json
            from app.models.user import User
            from app.services.content_input_receipts import validate_browser_input_dependencies
            await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]),
                receipt.dependencies_json)
            context = observed["context"]
            source = dict(origin.sources[0])
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                context=context), [source])
            for altered in [replace(context, delegation_token=None), replace(context, session_id=str(uuid4())),
                replace(context, runtime_task_id=str(seed["run_id"])),
                replace(context, delegation_token=replace(context.delegation_token, expires_at=0))]:
                for upstream_source in (origin.sources if native_creation else [source]):
                    with pytest.raises(ContentInputReceiptError) as denied:
                        await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                            context=altered), [upstream_source])
                    assert denied.value.status_code == 403 and denied.value.code == "content_source_access_denied"
            from app.services.content_input_receipts import _validate_input_dependencies
            from app.tools.handlers.knowledge import _personal_kb_runtime_principal
            principal = _personal_kb_runtime_principal(ToolExecutionRequest(tool_name="read_file", arguments={}, context=context))
            with pytest.raises(ContentInputReceiptError) as denied:
                await _validate_input_dependencies(db, seed["tenant_id"], [source], runtime_request=principal)
            assert denied.value.status_code == 403
            control = await db.begin_nested()
            bound_task = await db.get(RuntimeTask, task_id)
            bound_task.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.flush()
            with pytest.raises(ContentInputReceiptError) as denied:
                await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                    context=context), [source])
            assert denied.value.status_code == 403
            await control.rollback()
            from app.models.chat_artifact import ChatArtifact
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == receiver,
                ChatArtifact.runtime_task_id == task_id, ChatArtifact.path == report_path))
            assert artifact is not None and artifact.snapshot_json["content_input_receipt_id"] == str(receipt.id)
            artifact_id = artifact.id
            from app.models.config_revision import ConfigRevision
            # Keep the legacy identity-claim contract independent of native directory observations.
            from app.services.agent_context import load_agent_context_resource
            legacy_roster = await load_agent_context_resource(agent_id=receiver, tenant_id=seed["tenant_id"],
                resource_ref="a2a-collaborators")
            roster = {"source_kind": "agent_context_resource", "source_id": f"{receiver}\na2a-collaborators",
                "source_version": legacy_roster.sha256}
            if public_directory_case:
                # A legacy file has no receipt for foreign public identities; the native directory does.
                with pytest.raises(ContentInputReceiptError) as unavailable:
                    await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]), [roster])
                assert unavailable.value.status_code == 503
            else:
                await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]), [roster])
            control = await db.begin_nested()
            witness = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_id == seed["agent_id"],
                ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.version == 1))
            witness.changed_by_agent_id = receiver
            from sqlalchemy.exc import DBAPIError
            with pytest.raises(DBAPIError, match="config revision snapshots are immutable"):
                await db.flush()
            await control.rollback()
            control = await db.begin_nested()
            parent = await db.get(Agent, seed["agent_id"])
            parent.name = "Unwitnessed replacement label"
            await db.flush()
            with pytest.raises(ContentInputReceiptError) as denied:
                await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]), [roster])
            assert denied.value.status_code == 503
            await control.rollback()
        assert not list(await db.scalars(select(SessionCommand).where(SessionCommand.session_id == UUID(request.session_id))))
        for altered in [replace(request, owner_id=uuid4()), replace(request, parent_agent_id=uuid4()),
            replace(request, target=SimpleNamespace(id=uuid4()))]:
            assert await load_delegation_instruction_origin(db, request=altered, tenant_id=seed["tenant_id"],
                content=message["content"]) is None
        assert await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
            content="An unrelated instruction.") is None
        from copy import deepcopy
        forged = deepcopy(request.conversation_messages)
        forged[0]["a2a_instruction_origin"]["source_hash"] = "0" * 64
        assert await load_delegation_instruction_origin(db, request=replace(request, conversation_messages=forged),
            tenant_id=seed["tenant_id"], content=message["content"]) is None
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db
    async def viewer():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = viewer
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
        response = await browser.get(f"/agents/{receiver}/files/artifacts/{artifact_id}/content")
        assert response.status_code == 200 and "DELEGATED_REPORT" in response.text, response.text
    if not delivery_case:
        return
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

    marker = seed["data_root"] / str(receiver) / report_path
    if not saved_reopen:
        marker.write_text("A later local edit must not replace the delivered version.")
    written_at = marker.stat().st_mtime_ns
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.direct_invocation_terminal_boundary_processor import _validate_response_payload
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        child = await db.get(RuntimeTask, task_id)
        frontier = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id)
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        with pytest.raises(TerminalBoundaryCanonicalMismatch):
            await _validate_response_payload(db=db, task=child, agent_id=receiver, session_id=request.session_id,
                source="agent", terminal_event=frontier, payload={**result.response_complete_payload,
                    "metadata": {**result.response_complete_payload["metadata"], "final_response": "Forged final."}})
    fence = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)
    if child_command_case:
        result = next(m["content"] for m in client.requests[1]["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == "child-calculate")
        assert "360" in result and "<tool_error>" not in result, result
    if child_readback_case:
        if child_missing_file_case:
            assert len(client.requests) == 4 and marker.stat().st_mtime_ns == written_at
        else:
            assert len(client.requests) == 3 and marker.stat().st_mtime_ns == written_at
    else:
        assert len(client.requests) == 2 and marker.stat().st_mtime_ns == written_at
    if completion_sweep_case:
        from app.models.runtime_result import RuntimeResultObject

        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, task_id)
            child.metadata_json = {**child.metadata_json, "parent_agent_id": str(receiver)}
            await db.commit()
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        assert await service.reconcile_terminal_tasks_once(limit=100) == 0
        assert await service.reconcile_terminal_tasks_once(limit=100) == 0
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert not await db.scalar(select(RuntimeNotificationOutbox.id).where(
                RuntimeNotificationOutbox.tenant_id == seed["tenant_id"],
                RuntimeNotificationOutbox.source_run_id == str(task_id)))
            assert not await db.scalar(select(RuntimeResultObject.id).where(
                RuntimeResultObject.tenant_id == seed["tenant_id"], RuntimeResultObject.source_run_id == str(task_id)))
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    boundaries = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="native-first-task-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert boundaries["delivered"] == 1, boundaries
    async with owner_sessionmaker() as db:
        child = await db.get(RuntimeTask, task_id)
        assert child.status == "completed", child.result_summary
        source_run = await db.get(RuntimeTask, seed["run_id"])
        source_run.status = "completed"
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="native-first-task-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="native-first-task-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert pages
    for page in pages:
        delivered_page = await service._deliver_page(page)
        assert delivered_page.get("ok") is True
        repeated_page = await service._deliver_page(page)
        assert repeated_page["status"] == "already_delivered" and repeated_page["deduplicated"] is True
        assert repeated_page["event_id"] == delivered_page["event_id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        notification = await db.scalar(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id),
            RuntimeNotificationOutbox.source_kind == "a2a_delegation"))
        assert notification is not None and notification.parent_session_id == seed["session_id"]
        assert notification.parent_agent_id == seed["agent_id"] and notification.parent_agent_id != receiver
        returns = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext.in_([str(page.id) for page in pages]))))
        assert len(returns) == 1 and returns[0].parent_session_id == str(seed["session_id"])
        return_id = returns[0].id
        result_ref = notification.result_ref
        creation_result_ref = await db.scalar(select(RuntimeNotificationOutbox.result_ref).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"],
            RuntimeNotificationOutbox.parent_session_id == seed["session_id"],
            RuntimeNotificationOutbox.parent_agent_id == seed["agent_id"],
            RuntimeNotificationOutbox.parent_user_id == seed["user_id"],
            RuntimeNotificationOutbox.source_kind == "employee_creation")) if creation_result_case else None
        if creation_result_case:
            assert creation_result_ref
        from app.services.session_semantic_history import load_session_semantic_history

        history = await load_session_semantic_history(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            session_id=seed["session_id"], current_run_id=return_id)
        assert "DELEGATED_REPORT" not in json.dumps([item.content for item in history.messages])
    async def read_return():
        return json.loads(str(await agent_tools.execute_tool("read_runtime_result", {"result_ref": result_ref},
            agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            runtime_task_id=str(return_id), permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)))
    delivered = await read_return()
    (tmp_path / "native-parent-result-read.json").write_text(json.dumps(delivered, indent=2))
    assert delivered["status"] == "ok" and "DELEGATED_REPORT" in delivered["content"], delivered
    assert "A later local edit" not in delivered["content"] and delivered["dependency_coverage"]["complete"] is True
    returned_artifact = json.loads(delivered["content"])["artifacts"][0]
    assert returned_artifact["artifact_id"] == str(artifact_id) and not returned_artifact["legacy_current_file_fallback"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
        returned = await browser.get(f"/agents/{returned_artifact['download_agent_id']}/files/artifacts/{returned_artifact['artifact_id']}/content")
        assert returned.status_code == 200 and "DELEGATED_REPORT" in returned.text and "A later local edit" not in returned.text
    if parent_model_case:
        from app.services.web_chat_runtime import execute_web_chat_run

        if parent_status_unknown:
            async with owner_sessionmaker() as db:
                known = await db.get(RuntimeTask, task_id)
                db.add(RuntimeTask(id=uuid4(), tenant_id=known.tenant_id, task_type="delegation", status="completed",
                    parent_agent_id=known.parent_agent_id, child_agent_id=known.child_agent_id,
                    parent_session_id=known.parent_session_id, root_user_id=known.root_user_id,
                    root_session_id=known.root_session_id, delegation_chain_json=known.delegation_chain_json,
                    prompt="Unverified legacy task instruction", result_summary="Unverified legacy task result"))
                await db.commit()
        parent_path = "workspace/parent-review.md"
        parent_reads = ([
            {"id": "parent-check-task", "type": "function", "function": {
                "name": "check_async_task", "arguments": json.dumps({"task_id": task_id.hex})}},
            {"id": "parent-task-output", "type": "function", "function": {
                "name": "task_output", "arguments": json.dumps({"runtime_task_id": task_id.hex})}},
            {"id": "parent-task-list", "type": "function", "function": {
                "name": "list_async_tasks", "arguments": "{}"}},
        ] if parent_status_case else [])
        if result_denial_case or historical_creation_case:
            from app.services import runtime_result_sources

            native_result_validator = runtime_result_sources.validate_runtime_result_sources
            async def controlled_source_denial(db, request, result_object, **kwargs):
                if historical_creation_case and request is not None and request.context.tool_call_id == "parent-read-creation":
                    return False  # The original native page predates canonical creation-source support.
                if request is not None and request.context.tool_call_id == "parent-read-result-denied":
                    raise ContentInputReceiptError("content_source_access_denied", "Controlled source denial.", 403)
                return await native_result_validator(db, request, result_object, **kwargs)
            monkeypatch.setattr(runtime_result_sources, "validate_runtime_result_sources", controlled_source_denial)
            if historical_creation_unknown:
                native_canonical_loader = runtime_result_sources.load_canonical_result_sources
                async def missing_creation_producer(db, result_object):
                    if result_object.source_kind == "employee_creation":
                        return False
                    return await native_canonical_loader(db, result_object)
                monkeypatch.setattr(runtime_result_sources, "load_canonical_result_sources", missing_creation_producer)
        if result_denial_case:
            parent_reads.append({"id": "parent-read-result-denied", "type": "function", "function": {
                "name": "read_runtime_result", "arguments": json.dumps({"result_ref": result_ref})}})
        if creation_result_case:
            parent_reads.append({"id": "parent-read-creation", "type": "function", "function": {
                "name": "read_runtime_result", "arguments": json.dumps({"result_ref": creation_result_ref})}})
        if creator_resume_case:
            parent_reads.append({"id": "parent-resume-first-task", "type": "function", "function": {
                "name": "delegate_to_agent", "arguments": json.dumps({"first_task_draft_id": creation["blueprint_id"],
                    "agent_name": arguments["name"], "target_agent_id": str(receiver), "message": instruction})}})
        parent_client = ScriptedLLMClient([
            {"tool_calls": [*parent_reads, {"id": "parent-read-return", "type": "function", "function": {
                "name": "read_runtime_result", "arguments": json.dumps({"result_ref": result_ref})}}],
                "finish_reason": "tool_calls"},
            {"tool_calls": [{"id": "parent-write-review", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": parent_path,
                    "content": "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT"})}}], "finish_reason": "tool_calls"},
            {"content": "Reviewed the returned report.\nDELIVERABLE: workspace/parent-review.md", "finish_reason": "stop"},
        ])
        if parent_missing_file_case:
            parent_client.script.insert(1, {"tool_calls": [{"id": "parent-missing-report-read", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": parent_path})}}],
                "finish_reason": "tool_calls"})
            parent_client.script.insert(3, {"tool_calls": [{"id": "parent-review-readback", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": parent_path})}}],
                "finish_reason": "tool_calls"})
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: parent_client)
        async with owner_sessionmaker() as db:
            continuation = await db.get(RuntimeTask, return_id)
            continuation.status, continuation.claimed_by, continuation.claim_version = "running", "parent-return-worker", 1
            continuation.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        parent_fence = set_runtime_task_fence(task_id=return_id, worker_id="parent-return-worker", claim_version=1)
        try:
            await execute_web_chat_run(return_id)
        finally:
            reset_runtime_task_fence(parent_fence)
        (tmp_path / "native-parent-model-requests.json").write_text(json.dumps(parent_client.requests, default=str, indent=2))
        if parent_missing_file_case:
            assert len(parent_client.requests) == 5
            missing = next(m["content"] for m in parent_client.requests[2]["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "parent-missing-report-read")
            assert "File not found" in missing and "DELEGATED_REPORT" not in missing
            readback = next(m["content"] for m in parent_client.requests[4]["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "parent-review-readback")
            assert "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT" in readback and "<tool_error>" not in readback, readback
        else:
            assert len(parent_client.requests) == 3
        second_request = json.dumps(parent_client.requests[1]["messages"])
        assert "DELEGATED_REPORT" in second_request and "A later local edit" not in second_request
        assert (seed["agent_root"] / parent_path).read_text() == "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT"
        if result_denial_case:
            denial = json.loads(next(m["content"] for m in parent_client.requests[1]["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "parent-read-result-denied"))
            assert denial["status"] == "source_access_denied" and "content" not in denial
        if creation_result_case:
            creation_result = json.loads(next(m["content"] for m in parent_client.requests[1]["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "parent-read-creation"))
            assert creation_result["status"] == "ok" and "Employee creation: completed" in creation_result["content"]
            if historical_creation_case:
                assert creation_result["dependency_coverage"]["complete"] is False
            else:
                assert creation_result["dependency_coverage"]["complete"] is True
        if parent_status_unknown:
            assert "Unverified legacy task result" in second_request
            assert observed["status"]["status"] == "unavailable", observed["status"]
            assert observed["status"]["reason_code"] == "model_input_inventory_incomplete"
            return
        if historical_creation_unknown:
            assert observed["status"]["status"] == "unavailable", observed["status"]
            assert observed["status"]["reason_code"] == "model_input_inventory_incomplete"
            return
        assert observed["status"]["status"] == "captured", observed["status"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            continuation = await db.get(RuntimeTask, return_id)
            assert continuation.status == "completed", continuation.result_summary
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == seed["agent_id"],
                ChatArtifact.runtime_task_id == return_id, ChatArtifact.path == parent_path))
            assert artifact is not None and artifact.snapshot_json.get("content_input_receipt_id"), artifact
            parent_artifact_id = artifact.id
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)))
            if parent_missing_file_case:
                assert len(models) == 5 and all(model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for model in models)
            else:
                assert len(models) == 3 and all(model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for model in models)
            if creator_resume_case:
                from types import SimpleNamespace
                from app.services.employee_creation_sources import _owned_context_source
                from app.services.employee_first_task import first_task_dispatch_result_bound
                from app.services.session_model_inputs import _hash, tool_result_source_hash

                resumed = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == return_id,
                    SessionToolInvocation.provider_tool_use_id == "parent-resume-first-task"))
                payload = _event_payload(await db.get(ChatTranscriptEvent, resumed.result_event_id))
                content = payload["content"]
                body = json.loads(content)
                assert body["ok"] is True and body["runtime_task_id"] == str(task_id) and body["status"] == "completed"
                assert len(dispatches) == 1
                assert list(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"],
                    RuntimeTask.parent_agent_id == seed["agent_id"], RuntimeTask.task_type == "delegation"))) == [task_id]
                scope = {"tenant_id": seed["tenant_id"], "agent_id": seed["agent_id"]}
                assert await first_task_dispatch_result_bound(db, **scope, invocation=resumed, payload=payload, content=content)
                source = {"source_kind": "session_tool_invocation", "source_id": str(resumed.id),
                    "source_version": tool_result_source_hash(payload)}
                owned = {**scope, "session_id": seed["session_id"], "user_id": seed["user_id"]}
                assert await _owned_context_source(db, dependency=source, **owned)
                assert not await _owned_context_source(db, dependency={**source, "source_version": "0" * 64}, **owned)
                fields = {key: getattr(resumed, key) for key in ("tenant_id", "session_id", "run_id", "tool_name",
                    "effect_state", "provider_tool_use_id", "provider_arguments_json", "effective_arguments_json",
                    "args_hash", "effective_args_hash", "receipt_ref")}
                for key, value in {"effect_state": "needs_reconciliation", "session_id": uuid4(), "run_id": uuid4(),
                    "tenant_id": uuid4(), "provider_tool_use_id": "another-call", "effective_args_hash": "0" * 64}.items():
                    assert not await first_task_dispatch_result_bound(db, **scope,
                        invocation=SimpleNamespace(**{**fields, key: value}), payload=payload, content=content)
                legacy = json.dumps({key: value for key, value in body.items() if key != "native_first_task_dispatch"}, ensure_ascii=False)
                legacy_ref = f"tool-frame:{_hash(legacy)}"
                assert not await first_task_dispatch_result_bound(db, **scope,
                    invocation=SimpleNamespace(**{**fields, "receipt_ref": legacy_ref}),
                    payload={**payload, "content_or_error_ref": legacy_ref}, content=legacy)
                assert not await first_task_dispatch_result_bound(db, **scope,
                    invocation=resumed, payload={**payload, "outcome": "unknown"}, content=content)
                control = await db.begin_nested()
                changed = await db.get(RuntimeTask, task_id)
                changed.prompt = "A different first task"
                await db.flush()
                assert not await first_task_dispatch_result_bound(db, **scope, invocation=resumed, payload=payload, content=content)
                await control.rollback()
                assert await first_task_dispatch_result_bound(db, **scope, invocation=resumed, payload=payload, content=content)
            if child_readback_case:
                from app.models.invocation_span import InvocationSpan
                from app.models.runtime_result import RuntimeResultObject
                from app.models.session_v2 import SessionToolInvocation
                from app.services.runtime_result_sources import (
                    _published_owned_delegation_input, load_canonical_result_sources, validate_runtime_result_sources,
                )

                result_object = await db.get(RuntimeResultObject, UUID(result_ref.split("/")[2]))
                task, dependencies, pins = await load_canonical_result_sources(db, result_object)
                read_call = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == task_id, SessionToolInvocation.provider_tool_use_id == "child-report-read"))
                read_source = next(ref for ref in dependencies if ref["source_kind"] == "session_tool_invocation"
                    and ref["source_id"] == str(read_call.id))
                assert await _published_owned_delegation_input(db, read_source, task, receipt_ids=pins)
                assert not await _published_owned_delegation_input(db, read_source, task)
                assert not await _published_owned_delegation_input(db, read_source, task, receipt_ids={uuid4()})
                if child_missing_file_case:
                    missing_call = await db.scalar(select(SessionToolInvocation).where(
                        SessionToolInvocation.run_id == task_id, SessionToolInvocation.provider_tool_use_id == "child-missing-report-read"))
                    missing_source = next(ref for ref in dependencies if ref["source_kind"] == "session_tool_invocation"
                        and ref["source_id"] == str(missing_call.id))
                    assert await _published_owned_delegation_input(db, missing_source, task)
                    assert await _published_owned_delegation_input(db, missing_source, task, receipt_ids=pins)
                    missing_event = await db.get(ChatTranscriptEvent, missing_call.result_event_id)
                    control = await db.begin_nested()
                    metadata = json.loads(json.dumps(missing_event.metadata_json))
                    metadata["v2_payload"]["metadata"].pop("native_read_denial_receipt")
                    missing_event.metadata_json = metadata
                    await db.flush()
                    assert not await _published_owned_delegation_input(db, missing_source, task, receipt_ids=pins)
                    await control.rollback()
                    assert await _published_owned_delegation_input(db, missing_source, task, receipt_ids=pins)
                consumer = ToolExecutionRequest(tool_name="read_runtime_result", arguments={"result_ref": result_ref},
                    context=observed["context"])
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, consumer, [read_source])
                assert denied.value.status_code == 403
                control = await db.begin_nested()
                evidence = await db.get(InvocationSpan, uuid5(read_call.id, "workspace-input-evidence"))
                evidence.metadata_json = {**evidence.metadata_json, "workspace_input_evidence": {
                    **evidence.metadata_json["workspace_input_evidence"], "observed_sha256": "0" * 64}}
                await db.flush()
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_result_sources(db, consumer, result_object)
                assert denied.value.status_code == 403
                await control.rollback()
                assert await validate_runtime_result_sources(db, consumer, result_object)
                control = await db.begin_nested()
                pin = await db.get(ContentInputReceipt, next(iter(pins)), populate_existing=True)
                cyclic = {"source_kind": "workspace_artifact", "source_id": str(pin.id), "source_version": pin.content_sha256}
                pin.dependencies_json = [*pin.dependencies_json, cyclic]
                await db.flush()
                assert cyclic in (await load_canonical_result_sources(db, result_object))[1]
                from app.services.content_input_receipts import _validate_input_dependencies

                with pytest.raises(ContentInputReceiptError) as unavailable:
                    await _validate_input_dependencies(db, seed["tenant_id"], [cyclic],
                        active_workspace_receipts=frozenset(pins))
                assert unavailable.value.status_code == 503 and "cyclic" in str(unavailable.value)
                await control.rollback()
                assert await validate_runtime_result_sources(db, consumer, result_object)
            if public_directory_case:
                from app.models.invocation_span import InvocationSpan
                from app.services.agent_directory_sources import owned_directory_source
                from app.services.runtime_result_sources import load_canonical_result_sources, validate_runtime_result_sources
                from app.models.runtime_result import RuntimeResultObject

                result_object = await db.get(RuntimeResultObject, UUID(result_ref.split("/")[2]))
                _task, dependencies, _receipts = await load_canonical_result_sources(db, result_object)
                directory_sources = [ref for ref in dependencies if ref["source_kind"] == "agent_directory_observation"]
                child_directory = None
                for ref in directory_sources:
                    if (await db.get(InvocationSpan, UUID(ref["source_id"]))).agent_id == receiver:
                        child_directory = ref
                        break
                assert child_directory is not None
                witness = await db.get(InvocationSpan, UUID(child_directory["source_id"]))
                proof = witness.metadata_json["agent_directory_observation"]
                assert any(entry["section"] == "public_agents" and entry["id"] == str(peer_id)
                    for entry in proof["inventory"])
                assert await owned_directory_source(db, dependency=child_directory, agent_id=receiver,
                    tenant_id=seed["tenant_id"], session_id=UUID(request.session_id), user_id=seed["user_id"])
                assert not await owned_directory_source(db, dependency=child_directory, agent_id=receiver,
                    tenant_id=seed["tenant_id"], session_id=UUID(request.session_id), user_id=peer_owner)
                control = await db.begin_nested()
                await db.delete(await db.get(AgentPermission, public_permission_id))
                await db.flush()
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_result_sources(db, ToolExecutionRequest(tool_name="read_runtime_result",
                        arguments={"result_ref": result_ref}, context=observed["context"]), result_object)
                assert denied.value.status_code == 403
                await control.rollback()
            parent_receipt = await db.get(ContentInputReceipt, UUID(artifact.snapshot_json["content_input_receipt_id"]))
            source = next(ref for ref in parent_receipt.dependencies_json if ref["source_kind"] == "runtime_result")
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                context=observed["context"]), [source])
            for altered_source, altered_context in [({**source, "source_version": "0" * 64}, observed["context"]),
                (source, replace(observed["context"], session_id=str(uuid4())))]:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=altered_context), [altered_source])
                assert denied.value.status_code == 403
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            review = await browser.get(f"/agents/{seed['agent_id']}/files/artifacts/{parent_artifact_id}/content")
            assert review.status_code == 200 and review.json()["content"] == "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT" and review.json()["uses_snapshot"], review.text

    if input_case in {"first_task_fresh_employee", "first_task_saved_reopen"}:
        if saved_reopen:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
                baseline = await browser.get(f"/agents/{receiver}/files/content", params={"path": report_path})
                assert baseline.status_code == 200, baseline.text
                saved = await browser.put(f"/agents/{receiver}/files/content", params={"path": report_path},
                    json={"content": "USER_EDITED_SAVED_REPORT", "expected_current_hash": baseline.json()["content_hash"]})
                assert saved.status_code == 200 and saved.json()["content_revision"] > 1, saved.text
                original = await browser.get(f"/agents/{receiver}/files/artifacts/{artifact_id}/content")
                assert original.status_code == 200 and original.json()["content"] == "DELEGATED_REPORT", original.text
        fresh_session_id, fresh_input_id = uuid4(), uuid4()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            fresh_session = ChatSession(id=fresh_session_id, tenant_id=seed["tenant_id"], agent_id=receiver,
                user_id=seed["user_id"], source_channel="web")
            db.add(fresh_session)
            await db.flush()
            user = await db.get(User, seed["user_id"])
            authority = await resolve_session_mutation_authority(db, user=user, agent_id=receiver,
                session_id=fresh_session_id, action="mutate_session_input")
            await accept_human_input(db, authority=authority, intent={"kind": "start_turn", "input_id": str(fresh_input_id),
                "idempotency_key": str(fresh_input_id), "session_id": str(fresh_session_id),
                "content_parts": [{"type": "text", "text": "Create a new draft report in this session."}],
                "runtime_metadata": {"source": "session_input_api"}})
            await run_user_prompt_admission(db, authority=authority, input_id=fresh_input_id, worker_id="fresh-employee-admission")
            admitted = await db.get(SessionTurnInput, fresh_input_id)
            started = await _start_input_runtime(db, row=admitted, command=await db.get(SessionCommand, admitted.command_id),
                agent=await db.get(Agent, receiver), user=user, session=fresh_session, successor=False)
            fresh_run_id = UUID(started["run_id"])
            fresh_run = await db.get(RuntimeTask, fresh_run_id)
            fresh_run.status, fresh_run.claimed_by, fresh_run.claim_version = "running", "fresh-employee-worker", 1
            fresh_run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        fresh_path = "workspace/fresh-report.md"
        fresh_client = ScriptedLLMClient([
            *([{"tool_calls": [{"id": "fresh-employee-read", "type": "function", "function": {"name": "read_file",
                "arguments": json.dumps({"path": report_path})}}], "finish_reason": "tool_calls"}] if saved_reopen else []),
            {"tool_calls": [{"id": "fresh-employee-write", "type": "function", "function": {"name": "write_file",
                "arguments": json.dumps({"path": fresh_path, "content": "FRESH_EMPLOYEE_REPORT"})}}], "finish_reason": "tool_calls"},
            {"content": "Fresh report prepared.\nDELIVERABLE: workspace/fresh-report.md", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: fresh_client)
        fresh_fence = set_runtime_task_fence(task_id=fresh_run_id, worker_id="fresh-employee-worker", claim_version=1)
        try:
            await execute_web_chat_run(fresh_run_id)
        finally:
            reset_runtime_task_fence(fresh_fence)
        (tmp_path / "fresh-employee-model-requests.json").write_text(json.dumps(fresh_client.requests, default=str, indent=2))
        assert len(fresh_client.requests) == (3 if saved_reopen else 2) and "REFINED_CONTEXT" in json.dumps(fresh_client.requests[0]["messages"])
        if saved_reopen:
            assert "USER_EDITED_SAVED_REPORT" in json.dumps(fresh_client.requests[1]["messages"])
        assert observed["status"]["status"] == "captured", observed["status"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (await db.get(RuntimeTask, fresh_run_id)).status == "completed"
            fresh_artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == fresh_run_id,
                ChatArtifact.agent_id == receiver, ChatArtifact.path == fresh_path))
            assert fresh_artifact is not None and fresh_artifact.snapshot_json.get("content_input_receipt_id")
            fresh_artifact_id = fresh_artifact.id
            fresh_rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == fresh_run_id)))
            assert len(fresh_rounds) == (3 if saved_reopen else 2) and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in fresh_rounds)
            for upstream_source in [ref for ref in origin.sources if ref["source_kind"] in {"agent_identity", "session_command"}]:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=observed["context"]), [upstream_source])
                assert denied.value.status_code == 403
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            fresh_content = await browser.get(f"/agents/{receiver}/files/artifacts/{fresh_artifact_id}/content")
            assert fresh_content.status_code == 200 and fresh_content.json()["content"] == "FRESH_EMPLOYEE_REPORT", fresh_content.text
        if saved_reopen:
            saved_source = next(ref for row in fresh_rounds for ref in row.model_request_snapshot_json["input_bindings"]["dependencies"]
                if ref["source_kind"] == "workspace_artifact")
            async with owner_sessionmaker() as db:
                original_artifact = await db.get(ChatArtifact, artifact_id)
                original_snapshot = dict(original_artifact.snapshot_json)
                original_artifact.snapshot_json = {**original_snapshot, "content_input_receipt_id": str(uuid4())}
                await db.commit()
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                with pytest.raises(ContentInputReceiptError) as unavailable:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=observed["context"]), [saved_source])
                assert unavailable.value.status_code == 503
            async with owner_sessionmaker() as db:
                (await db.get(ChatArtifact, artifact_id)).snapshot_json = original_snapshot
                await db.commit()
        async with owner_sessionmaker() as db:
            parent = await db.get(Agent, seed["agent_id"])
            parent.deactivated_at = datetime.now(timezone.utc)
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            profile_source = next(ref for ref in fresh_rounds[0].model_request_snapshot_json["input_bindings"]["dependencies"]
                if ref["source_kind"] == "agent_identity")
            with pytest.raises(ContentInputReceiptError) as denied:
                await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                    context=observed["context"]), [profile_source])
            assert denied.value.status_code == 403
            if saved_reopen:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=observed["context"]), [saved_source])
                assert denied.value.status_code == 403
        async with owner_sessionmaker() as db:
            parent = await db.get(Agent, seed["agent_id"])
            parent.deactivated_at = None
            await db.commit()
        assert len(dispatches) == 1 and len(refinement_calls) == 1 and len(client.requests) == 2
    from app.models.hr_creation import HrCreationDraft
    from copy import deepcopy

    if native_refined:
        from app.models.hr_creation import HrProvisioningStep
        from app.services.config_versioning import canonical_content_hash

        async with owner_sessionmaker() as db:
            step = await db.scalar(select(HrProvisioningStep).where(HrProvisioningStep.draft_id == UUID(creation["blueprint_id"]),
                HrProvisioningStep.step_key == "model"))
            original_receipt = deepcopy(step.receipt_json)
            altered_receipt = deepcopy(original_receipt)
            call = altered_receipt["refinement"]["call"]
            call["request"]["messages"][0]["content"] += " Forged extra source."
            call["sha256"] = canonical_content_hash({key: value for key, value in call.items() if key != "sha256"})
            step.receipt_json = altered_receipt
            await db.commit()
        rejected_refinement = await read_return()
        assert rejected_refinement["status"] == "source_unavailable" and "DELEGATED_REPORT" not in json.dumps(rejected_refinement)
        async with owner_sessionmaker() as db:
            step = await db.get(HrProvisioningStep, step.id)
            step.receipt_json = original_receipt
            await db.commit()
        assert (await read_return())["status"] == "ok" and len(refinement_calls) == 1
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
        accepted = deepcopy(draft.preview_json)
        changed = deepcopy(accepted)
        changed["creation_authorization"]["creation_origin"]["source_hash"] = "0" * 64
        draft.preview_json = changed
        await db.commit()
    altered_binding = await read_return()
    assert altered_binding["status"] == "source_unavailable" and "DELEGATED_REPORT" not in json.dumps(altered_binding)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
        draft.preview_json = accepted
        await db.commit()
    assert (await read_return())["status"] == "ok"
    soul = seed["data_root"] / str(receiver) / "soul.md"
    soul.write_text(soul.read_text() + "\nChanged after first-task delivery.\n")
    stale = await read_return()
    assert stale["status"] == "source_access_denied" and stale["reason_code"] == "content_source_changed", stale
    if child_readback_case:
        if child_missing_file_case:
            assert "DELEGATED_REPORT" not in json.dumps(stale) and len(client.requests) == 4
        else:
            assert "DELEGATED_REPORT" not in json.dumps(stale) and len(client.requests) == 3
    else:
        assert "DELEGATED_REPORT" not in json.dumps(stale) and len(client.requests) == 2
    if parent_model_case:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            stale_review = await browser.get(f"/agents/{seed['agent_id']}/files/artifacts/{parent_artifact_id}/content")
            assert stale_review.status_code == 403 and "DELEGATED_REPORT" not in stale_review.text
        if parent_missing_file_case:
            assert len(parent_client.requests) == 5
        else:
            assert len(parent_client.requests) == 3
