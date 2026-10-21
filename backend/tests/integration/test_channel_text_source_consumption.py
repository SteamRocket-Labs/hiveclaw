"""Private native result text consumes the current authority of its frozen inputs."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.source_read_purpose import source_recipient_read
from app.database import pin_rls_tenant_context
from app.models.channel_config import ChannelConfig
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionRunOutcome, SessionTurnInput
from app.models.storage_blob import StorageBlobRef
from app.models.tenant_channel_config import TenantChannelConfig  # noqa: F401 (register principal FK target)
from app.models.user import User
from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.external_principal_service import bind_authenticated_self_channel_principal
from app.services.feishu_service import FeishuService
from app.services.session_model_inputs import _model_dependencies
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
RESULT = "PRIVATE_NATIVE_RESULT_DERIVED_FROM_ORIGINAL"


@pytest.mark.parametrize("source_state", ["active", "withdrawn"])
@pytest.mark.parametrize("projection", ["initial", "recovery"])
async def test_private_native_result_text_rechecks_its_frozen_source(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_state, projection,  # noqa: F811
):
    import app.runtime.invoker as invoker
    from app.api import upload
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_human_input import revise_unbound_human_input
    from app.services.session_terminal_outcome import commit_terminal_outcome
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.services.web_chat_runtime import execute_web_chat_run

    _, client = original_api
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Read the uploaded original and report its content.")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", seed["data_root"])
    for module, name in ((agent_tools, "WORKSPACE_ROOT"), (agent_context, "PERSISTENT_DATA"),
        (agent_context, "TOOL_WORKSPACE")):
        monkeypatch.setattr(module, name, seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(db, agent, personality="Read authorized uploaded sources.",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-original-provider-only")
        model.max_input_tokens = 256_000
        await db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(str(seed["user_id"]), "member", str(seed["tenant_id"]))}
    uploaded = await client.post("/chat/upload", headers=headers, files={"file": ("source.txt", b"PRIVATE_NATIVE_INPUT")},
        data={"agent_id": str(seed["agent_id"]), "skip_personal_kb": "true"})
    assert uploaded.status_code == 200, uploaded.text
    original = uploaded.json()
    original_id = uuid.UUID(original["original_attachment"]["id"])
    calls = []

    async def text_transport(self, app_id, _secret, recipient, *args, **kwargs):
        calls.append({"recipient": recipient, "args": args, "kwargs": kwargs})
        return {"data": {"message_id": "synthetic-native-result-ack"}}

    monkeypatch.setattr(FeishuService, "send_message", text_transport)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        frame = await resolve_session_mutation_authority(db, user=user, agent_id=seed["agent_id"],
            session_id=seed["session_id"], action="mutate_session_input")
        pending = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.session_id == seed["session_id"]))
        await revise_unbound_human_input(db, authority=frame, input_id=pending.id, expected_revision=1,
            content_parts=[*pending.content_parts_json, {"type": "attachment_ref", "attachment": {
                "name": original["filename"], "path": original["workspace_path"],
                "original_attachment": original["original_attachment"]}}], user=user)
        config = ChannelConfig(id=uuid.uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            channel_type="feishu", app_id="synthetic-private-result", app_secret="synthetic-channel-secret", is_configured=True)
        db.add(config)
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
            provider_subject_id="ou-private-native-owner", user_id=user.id, actor_user_id=user.id)
        session = await db.get(ChatSession, seed["session_id"])
        target = {"channel": "feishu", "receive_id": "ou-private-native-owner",
            "receive_id_type": "open_id", "chat_type": "p2p", "session_id": str(session.id),
            "external_principal_id": str(bound.principal.id)}
        session.delivery_target_json = target if projection == "initial" else {}
        (await db.get(StorageBlobRef, original_id)).legal_hold = True
        await db.commit()
    (seed["agent_root"] / original["workspace_path"]).unlink()
    arguments = {"path": original["workspace_path"], "original_attachment_id": str(original_id)}
    model_client = ScriptedLLMClient([
        {"tool_calls": [{"id": "private-original-read", "type": "function", "function": {
            "name": "fs_read", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"},
        {"content": RESULT, "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model_client)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(model_client.requests) == 2, model_client.requests
    assert any(m.get("role") == "tool" and "PRIVATE_NATIVE_INPUT" in str(m.get("content"))
        for m in model_client.requests[-1]["messages"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed"
        outcome_id = uuid.UUID(task.metadata_json["session_v2_outcome"]["outcome_id"])
        if projection == "recovery":
            assert await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task.id)) is None
            (await db.get(ChatSession, seed["session_id"])).delivery_target_json = target
            await commit_terminal_outcome(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                session_id=seed["session_id"], run_id=task.id, outcome_id=outcome_id)
            await db.commit()
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == seed["run_id"]))
        assert outbox is not None and RESULT in outbox.text_content and not outbox.artifact_ids_json
        assert outbox.metadata_json["source"] == ("session_terminal_outcome" if projection == "initial"
            else "session_terminal_outcome_recovery")
        result = await db.get(SessionModelResult, (await db.get(SessionRunOutcome, outcome_id)).terminal_result_id)
        dependencies, complete = _model_dependencies(result)
        assert complete and {"source_kind": "original_attachment", "source_id": str(original_id),
            "source_version": original["original_attachment"]["content_sha256"]} in dependencies
        with source_recipient_read(session_id=seed["session_id"], user_id=user.id):
            await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=user)
        frozen_snapshot, outbox_id, result_id = dict(result.model_request_snapshot_json), outbox.id, result.id
    if source_state == "withdrawn":
        revoked = await client.post(f"/chat/attachments/{original_id}/withdraw", headers=headers,
            json={"expected_content_sha256": original["original_attachment"]["content_sha256"]})
        assert revoked.status_code == 200, revoked.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            with source_recipient_read(session_id=seed["session_id"], user_id=user.id):
                with pytest.raises(ContentInputReceiptError) as oracle:
                    await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=user)
            assert oracle.value.status_code == 403
    counts = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        worker_id="private-native-source", limit=100)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        outbox = await db.get(ChannelDeliveryOutbox, outbox_id)
        observed = {"provider": provider[0], "source_state": source_state, "projection": projection, "text_calls": calls,
            "status": outbox.status, "last_error": outbox.last_error, "counts": counts}
        (tmp_path / "private-native-text-source.json").write_text(json.dumps(observed, indent=2))
        assert len(calls) == int(source_state == "active"), observed
        assert outbox.status == ("delivered" if source_state == "active" else "dead_letter"), observed
        if source_state == "active":
            assert RESULT in json.dumps(calls)
        assert (await db.get(StorageBlobRef, original_id)).legal_hold
        assert (await db.get(SessionModelResult, result_id)).model_request_snapshot_json == frozen_snapshot
