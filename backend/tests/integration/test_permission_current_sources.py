"""Permission effects retain the current authority of their captured inputs."""

import json
import uuid

import pytest
from sqlalchemy import func, select, text

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_business_task_text_sources import (
    original_api, provider, delivered, s3_server,  # noqa: F401
    test_business_task_text_rechecks_its_actual_original_source as business_source,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("provider", ["filesystem"], indirect=True)
@pytest.mark.parametrize("withdraw_at", ["before_response", "before_effect", "unchanged"])
async def test_permission_rechecks_original_sources_before_releasing_effect(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, withdraw_at,  # noqa: F811
):
    async def inspect_wait(**context):
        from app.api import chat_sessions
        from app.services import agent_tools, runtime_task_worker
        from app.services.session_model_inputs import _model_dependencies

        client, seed = context["client"], context["seed"]
        factory, run_id = context["db_factory"], context["run_id"]
        async with factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            role = (await db.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).one()
            assert tuple(role) == (False, False)
            run = await db.get(RuntimeTask, run_id)
            invocation = await db.scalar(select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == run_id, SessionToolInvocation.tool_name == "fs_write"))
            model = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.run_id == run_id,
                SessionModelResult.provider_request_id == invocation.provider_request_id))
            dependencies, complete = _model_dependencies(model)
            original = context["original"]["original_attachment"]
            assert complete and {
                "source_kind": "original_attachment", "source_id": original["id"],
                "source_version": original["content_sha256"],
            } in dependencies
            assert run.status == "suspended" and invocation.effect_state == "prepared_not_started"
            invocation_id, permission_id, child_id = invocation.id, invocation.permission_item_id, run.child_session_id

        client._transport.app.include_router(chat_sessions.router)

        async def withdraw():
            response = await client.post(f"/chat/attachments/{original['id']}/withdraw",
                headers=context["headers"], json={"expected_content_sha256": original["content_sha256"]})
            assert response.status_code == 200, response.text

        execute = agent_tools.execute_session_permission_tool
        observed_fence = []

        async def execute_with_current_source(*args, **kwargs):
            callback = kwargs["pre_effect_callback"]

            async def before_effect(payload):
                if withdraw_at == "before_effect":
                    await withdraw()
                observed_fence.append(payload["tool_call_id"])
                await callback(payload)

            return await execute(*args, **{**kwargs, "pre_effect_callback": before_effect})

        async def no_wakeup(**_kwargs):
            pass

        monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute_with_current_source)
        monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
        if withdraw_at == "before_response":
            await withdraw()
        path = f"/agents/{seed['agent_id']}/sessions/{child_id}/permissions/{permission_id}/resolve"
        response = await client.post(path, headers=context["headers"], json={"action": "allow_once"})
        assert response.status_code == 200, response.text
        output = seed["agent_root"] / "workspace/business-report.md"
        observation = {"withdraw_at": withdraw_at, "receipt": response.json(),
            "effect_fences": observed_fence, "output_exists": output.exists()}
        (tmp_path / "permission-current-source.json").write_text(json.dumps(observation, indent=2))
        assert len(observed_fence) == 1
        if withdraw_at == "unchanged":
            assert output.read_text() == context["artifact_body"]
        else:
            assert not output.exists(), observation
        assert len(context["model_client"].requests) == 2
        assert response.json()["run_id"] == str(run_id) and response.json()["run_status"] == "resumable"
        assert response.json()["result_event_id"] is not None
        replay = await client.post(path, headers=context["headers"], json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert replay.json()["result_event_id"] == response.json()["result_event_id"]
        assert len(observed_fence) == 1
        async with factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            invocation = await db.get(SessionToolInvocation, invocation_id)
            event = await db.get(ChatTranscriptEvent, uuid.UUID(response.json()["result_event_id"]))
            assert invocation.result_event_id == event.id
            assert invocation.effect_state == ("effect_committed" if withdraw_at == "unchanged" else "prepared_not_started")
            assert await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
                ChatTranscriptEvent.invocation_id == invocation_id, ChatTranscriptEvent.item_kind == "tool_result")) == 1
            if withdraw_at != "unchanged":
                payload = event.metadata_json["v2_payload"]
                assert payload["outcome"] == "aborted"
                assert json.loads(payload["content"])["reason_code"] == "approved_tool_execution_not_released"

    await business_source(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, artifact_body="# Report\nPRIVATE_BUSINESS_INPUT\n",
        permission_handler=inspect_wait)
