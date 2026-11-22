"""Regressions for exact sources when sealed model requests are projected."""

from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.kernel.engine import _context_message, _dicts_to_llm_messages
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.services.content_input_receipts import _input_row, group_source_read
from app.services.employee_creation_sources import _owned_context_source
from app.services.llm_client import LLMMessage
from app.services.session_model_inputs import _origin_dependencies, capture_context_message_origins, tool_result_source_hash
from app.services.session_model_round import prepare_model_request
from app.services.session_semantic_history import _event_payload, load_session_semantic_history
from app.services.web_chat_runtime import conversation_from_history_messages
from tests.integration.source_withholding_helpers import legacy_model_requests
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, shared_memory  # noqa: F401
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_memory_result_proof_binds_the_model_id_across_turns(
    shared_memory, app_user_sessionmaker, owner_sessionmaker,  # noqa: F811
):
    n = shared_memory.n
    content, invocation = await memory_call(n, app_user_sessionmaker, "search_memory", {"query": "release", "scope": "facts"})
    assert "ORCHID_PRIVATE_MARKER" in content
    later_run = uuid4()
    later_turn = f"turn-{later_run.hex}"
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, n.run)).status = "completed"
        db.add(RuntimeTask(id=later_run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
            child_session_id=str(n.session), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=later_run, prompt="Continue the release work", metadata_json={"turn_id": later_turn}))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        proof = await db.get(InvocationSpan, uuid5(invocation.id, "explicit-memory-tool-result"))
        assert model.state in {"sealed", "round_committed"}
        assert model.id != invocation.id
        assert proof.metadata_json["memory_result"]["model_result_id"] == str(model.id)

        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=later_run, turn_id=later_turn, round_index=1, messages=messages, tools=[],
            provider="synthetic", model="local-reference")
        later = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = later.model_request_snapshot_json
        entry = next(item for item in snapshot["input_bindings"]["messages"]
            if item.get("source_ref") == f"session-tool-result:{invocation.id}")
        assert entry["status"] == "bound"
        assert any(source["source_kind"] == "explicit_memory" for source in entry["dependencies"])
        assert snapshot["wire_request"]["messages"][entry["message_index"]]["content"] == content


async def test_creation_seed_accepts_a_task_list_result_in_preflight(native_workspace, app_user_sessionmaker):  # noqa: F811
    n = native_workspace
    _content, call = await memory_call(n, app_user_sessionmaker, "list_async_tasks", {})
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        proof = await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read"))
        assert proof.metadata_json["runtime_task_read"]["unbound"] == []
        invocation = await db.get(type(call), call.id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        dependency = {"source_kind": "session_tool_invocation", "source_id": str(call.id),
            "source_version": tool_result_source_hash(_event_payload(event))}
        arguments = dict(dependency=dependency, agent_id=n.agent, tenant_id=n.tenant, session_id=n.session, user_id=n.user)
        assert await _owned_context_source(db, **arguments)
        with group_source_read(batch_inputs=True, cache_authority=False):
            assert await _owned_context_source(db, **arguments)


async def test_legacy_model_recovery_uses_the_input_row_projection(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tenant, agent, session, run, _bound = await _bound_input(owner_sessionmaker)
    content = "Exact frozen instructions recorded before source bindings."
    turn = f"turn-{run.hex}"
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        with legacy_model_requests(monkeypatch):
            request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
                run_id=run, turn_id=turn, round_index=1, messages=[LLMMessage(role="system", content=content)],
                tools=[], provider="synthetic", model="local-reference")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=turn, round_index=1, provider_request_id=request, content="Ready to continue.")
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        assert model.model_request_snapshot_json["input_bindings"]["dependency_coverage"] == {"complete": False}
        model_id = model.id
        witness = _context_message(role="system", kind="frozen_prompt", unbound=(), content=content)
        await capture_context_message_origins(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, result_id=uuid4(), messages=[witness])
        await db.commit()

    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        ref = f"session-model-result:{model_id}"
        assert await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=ref) == ([], True)
        with group_source_read(batch_inputs=True, source_catalog=True):
            retained = await _input_row(db, SessionModelResult, model_id)
            assert await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=ref,
                origin_rows={ref: retained}) == ([], True)
