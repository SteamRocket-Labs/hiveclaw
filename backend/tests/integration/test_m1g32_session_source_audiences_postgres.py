"""Session-source receipts retain exact versions and current native audiences."""

import uuid
from types import SimpleNamespace

import pytest

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionCommand, SessionToolInvocation
from app.models.user import User
from app.services.content_input_receipts import (
    ContentInputReceiptError, latest_workspace_receipt,
    validate_browser_input_dependencies, validate_runtime_input_dependencies,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create, _read
from tests.integration.test_m1g29_workspace_input_evidence_postgres import _native_read
from tests.services.test_session_input_control_v2 import _authority, _accepted_input

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(params=["session_command", "session_tool_invocation"])
async def session_source(request, native_workspace, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    n = native_workspace
    event_id = None
    if request.param == "session_command":
        async with owner_sessionmaker() as db:
            authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
            accepted = await _accepted_input(db, authority=authority, session_id=n.session,
                run_id=n.run, kind="steer_current_turn", content="Authenticated private input")
            source = await db.get(SessionCommand, accepted.command_id)
            source_id, version = source.id, source.request_hash
    else:
        from app.services.session_permission_runtime import _execution_evidence
        from app.services.session_semantic_history import _event_payload
        from app.services.web_chat_runtime import _persist_tool_call

        path = "workspace/source.md"
        await _create(n, path, "Private tool source")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            invocation, args = await _native_read(db, n, path)
        trace = {}
        result = await n.service.execute("read_file", args, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
            emit_runtime_hooks=False, trace_metadata_sink=trace)
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": "read_file", "args": args, "status": "done", "result": str(result),
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, invocation.id)
            event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
            assert event is not None
            source_id, version, event_id = invocation.id, _event_payload(event)["content_hash"], event.id
    dependency = {"source_kind": request.param, "source_id": str(source_id), "source_version": version}
    runtime = ToolExecutionRequest(tool_name="read_file", arguments={"path": "workspace/derived.md"},
        context=ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
            session_id=str(n.session), runtime_task_id=str(n.run), workspace=n.root))
    return SimpleNamespace(n=n, dependency=dependency, request=runtime, event_id=event_id)


async def test_current_source_can_be_consumed_by_real_browser_and_native_readers(
    session_source, owner_sessionmaker,
):
    s, path, body = session_source, "workspace/derived.md", "Derived within the original private audience"
    await _create(s.n, path, body)
    async with owner_sessionmaker() as db:
        receipt = await latest_workspace_receipt(db, s.n.tenant, s.n.agent, path)
        receipt.dependencies_json = [s.dependency]
        await db.commit()
    response = await s.n.client.get(f"/agents/{s.n.agent}/files/content", params={"path": path})
    assert response.status_code == 200, response.text
    assert response.json()["content"] == body
    assert body in await _read(s.n, "read_file", path)


async def test_browser_admin_inspection_cannot_authorize_another_workers_prompt(
    session_source, owner_sessionmaker, app_user_sessionmaker,
):
    s = session_source
    async with owner_sessionmaker() as db:
        admin = await db.get(User, s.n.foreign)
        admin.role = "org_admin"
        admin_session = uuid.uuid4()
        db.add(ChatSession(id=admin_session, tenant_id=s.n.tenant, agent_id=s.n.agent, user_id=admin.id))
        await db.commit()
    s.request.context.user_id = s.n.foreign
    s.request.context.session_id = str(admin_session)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        admin = await db.get(User, s.n.foreign)
        await validate_browser_input_dependencies(db, s.n.tenant, admin, [s.dependency])
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        assert error.value.status_code == 403


async def test_reference_version_cannot_fall_back_to_current_session_content(session_source, app_user_sessionmaker):
    s = session_source
    dependency = {**s.dependency, "source_version": "f" * 64}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [dependency])
        assert error.value.status_code == 403


async def test_changed_canonical_source_is_not_accepted_under_its_old_version(
    session_source, owner_sessionmaker, app_user_sessionmaker,
):
    s = session_source
    async with owner_sessionmaker() as db:
        if s.event_id:
            event = await db.get(ChatTranscriptEvent, s.event_id)
            metadata = dict(event.metadata_json)
            metadata["v2_payload"] = {**metadata["v2_payload"], "content": "UNVERIFIED_RESULT_REWRITE"}
            event.metadata_json = metadata
        else:
            command = await db.get(SessionCommand, uuid.UUID(s.dependency["source_id"]))
            command.target_json = {**command.target_json, "rewritten_without_hash": True}
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        assert error.value.status_code == 403


@pytest.mark.parametrize("change", ["transfer", "deactivate"])
async def test_current_authority_is_rechecked_after_a_previous_success(
    session_source, owner_sessionmaker, app_user_sessionmaker, change,
):
    s = session_source
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        await db.commit()
        async with owner_sessionmaker() as writer:
            if change == "transfer":
                (await writer.get(Agent, s.n.agent)).owner_user_id = s.n.foreign
            else:
                (await writer.get(User, s.n.user)).is_active = False
            await writer.commit()
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        assert error.value.status_code == 403
