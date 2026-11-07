"""An unverified recall projection cannot discard the caller's bound history."""

import json
from dataclasses import replace
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.session_v2 import SessionModelResult
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import (
    memory_call,
    new_owner_work,
    shared_memory,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("peer_session", [False, True])
@pytest.mark.parametrize("canonical_run", [False, True])
async def test_native_a2a_memory_keeps_reader_scope_without_a_personal_kb_grant_token(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, peer_session, canonical_run,
):
    from app.core.execution_context import ExecutionPrincipal
    from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, load_workspace_authority_scope
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal
    from app.tools.handlers.memory import load_memory, search_memory
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n = shared_memory.n
    session_id = n.session
    if peer_session:
        from app.models.agent import Agent
        from app.services.agent_pair_session import find_or_create_agent_pair_session

        # Exercise the peer side of the canonical pair, independently of UUID ordering.
        other_agent = uuid.UUID(int=n.agent.int - 1)
        async with owner_sessionmaker() as db:
            db.add(Agent(id=other_agent, tenant_id=n.tenant, name="Other owned bot",
                creator_id=n.user, owner_user_id=n.user, sponsor_user_id=n.user))
            await db.flush()
            pair = await find_or_create_agent_pair_session(db, source_agent_id=other_agent,
                target_agent_id=n.agent, tenant_id=n.tenant, owner_user_id=n.user,
                source_agent_name="Other owned bot", target_agent_name="Memory bot",
                root_session_id=n.session)
            assert pair.peer_agent_id == n.agent
            session_id = pair.id
            await db.commit()
    context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        workspace=n.root, session_id=str(session_id), runtime_task_id=str(n.run) if canonical_run else None,
        authority_frame_required=True, execution_principal=ExecutionPrincipal(
            tenant_id=n.tenant, source_agent_id=n.agent, requester_user_id=n.user,
            root_session_id=str(n.session), root_runtime_task_id=str(n.run),
            origin="a2a_delegation", delegation_chain=(f"agent:{n.agent}",)))
    request = ToolExecutionRequest("load_memory", {"ids": [shared_memory.entry_id]}, context)
    # Generic receipt reads do not acquire personal-KB delegation grants.
    with pytest.raises(ValueError, match="personal_kb_delegation_binding_missing"):
        _personal_kb_runtime_principal(request)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        principal, reader = await _runtime_input_actor(db, n.tenant, request)
        assert reader.id == n.user and principal.agent_id == n.agent
        scope = await load_workspace_authority_scope(db, reader, agent_id=n.agent, session_id=session_id)
        assert scope.root_session_id == n.session
        from app.models.user import User

        foreign_reader = await db.get(User, n.foreign)
        with pytest.raises(WorkspaceAuthorityError, match="session owned by the requester"):
            await load_workspace_authority_scope(db, foreign_reader, agent_id=n.agent, session_id=session_id)
        if peer_session:
            from app.models.chat_session import ChatSession

            pair = await db.get(ChatSession, session_id)
            pair.source_channel = "web"
            await db.flush()
            with pytest.raises(WorkspaceAuthorityError, match="session owned by the requester"):
                await load_workspace_authority_scope(db, reader, agent_id=n.agent, session_id=session_id)
            pair.source_channel = "agent"
            await db.flush()
        if not canonical_run:
            from app.services.explicit_memory_authority import _writer_input

            with pytest.raises(ContentInputReceiptError, match="exact sealed memory invocation"):
                await _writer_input(db, request)
        with pytest.raises(ContentInputReceiptError):
            await _runtime_input_actor(db, n.tenant,
                ToolExecutionRequest("load_memory", request.arguments, replace(context, user_id=n.foreign)))
        with pytest.raises(ContentInputReceiptError):
            await _runtime_input_actor(db, n.tenant, ToolExecutionRequest("load_memory", request.arguments,
                replace(context, user_id=n.foreign, execution_principal=replace(
                    context.execution_principal, requester_user_id=n.foreign))))
    content = await load_memory(n.agent, request.arguments, execution_context=context)
    assert "ORCHID_PRIVATE_MARKER" in content, content
    content = await search_memory(n.agent, {"query": "ORCHID_PRIVATE_MARKER", "scope": "all"},
        tenant_id=str(n.tenant), execution_context=context)
    assert "ORCHID_PRIVATE_MARKER" in content and "<tool_error>" not in content, content
    assert "session_recall_access_denied" not in content, content
    content = await search_memory(n.agent, {"query": "ORCHID_PRIVATE_MARKER", "scope": "sessions"},
        tenant_id=str(n.tenant), execution_context=context)
    assert "<tool_error>" not in content and "session_recall_access_denied" not in content, content


async def test_six_round_search_preserves_own_inputs_and_results_but_withholds_forgery(
    shared_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.config import get_settings
    from app.memory.t0.ledger import append_t0_session_event
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history

    n = shared_memory.n
    # The index can discover a legacy body, but it has no canonical producer
    # binding. Its bytes must not contaminate the separately receipted memory.
    append_t0_session_event(
        agent_id=n.agent, session_id=str(n.session), event_type="user_message", role="user",
        content="ORCHID_PRIVATE_MARKER LEGACY_UNVERIFIED_BODY", source="web",
        data_root=get_settings().AGENT_DATA_DIR,
    )
    session, run = await new_owner_work(n, owner_sessionmaker)
    outputs = []
    for index in range(6):
        content, _invocation = await memory_call(
            n, app_user_sessionmaker, "search_memory",
            {"query": "ORCHID_PRIVATE_MARKER", "scope": "all"}, session=session, run=run,
            command=f"Search my release memory, round {index + 1}", include_history=True,
        )
        assert "Remember my private release codename ORCHID_PRIVATE_MARKER" in content, content
        outputs.append(content)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
        )
        messages = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
        assert sum(m.role == "tool" for m in messages) == 6
        assert sum(m.role == "user" for m in messages) >= 6
        member_message = next(m for m in messages if m.role == "user")
        messages.extend([LLMMessage(role="user", content="FOREIGN_UNBOUND_BODY"),
            LLMMessage(role="user", content="TAMPERED_MEMBER_BODY", source_ref=member_message.source_ref)])
        request = await prepare_model_request(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=7, messages=messages, tools=[],
            provider="minimax", model="local-fake",
        )
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == request,
        ))
        snapshot = model.model_request_snapshot_json
        entries = snapshot["input_bindings"]["messages"]
        assert all(e["status"] == "bound" and e["code"] != "model_input_source_unbound"
            for e in entries[:-2]), entries
        assert all(e["status"] == "withheld" and e["code"] == "model_input_source_unbound"
            for e in entries[-2:])
        assert "FOREIGN_UNBOUND_BODY" not in json.dumps(snapshot["wire_request"])
        assert "TAMPERED_MEMBER_BODY" not in json.dumps(snapshot["wire_request"])
        assert "LEGACY_UNVERIFIED_BODY" not in json.dumps(snapshot["wire_request"])
    assert all("LEGACY_UNVERIFIED_BODY" not in content for content in outputs), outputs
    assert all("Memory Availability" in content for content in outputs), outputs


async def test_partial_search_keeps_receipted_fact_when_one_owned_source_is_revoked(
    shared_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.models.chat_session import ChatSession
    from app.services.session_model_inputs import _origin_dependencies

    n = shared_memory.n
    original_session = n.session
    session, run = await new_owner_work(n, owner_sessionmaker)
    saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "project", "content": "ORCHID independent available fact"},
        session=session, run=run)
    assert "Saved to explicit memory overlay" in saved, saved
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, original_session)).user_id = n.foreign
        await db.commit()
    session, run = await new_owner_work(n, owner_sessionmaker)
    content, invocation = await memory_call(n, app_user_sessionmaker, "search_memory",
        {"query": "ORCHID", "scope": "all"}, session=session, run=run)
    assert "ORCHID independent available fact" in content, content
    assert "ORCHID_PRIVATE_MARKER" not in content, content
    issues = json.loads(content.split("## Memory Availability\n", 1)[1])["issues"]
    assert len(issues) == 1, issues
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        _sources, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
            ref=f"session-tool-result:{invocation.id}")
        assert complete


async def test_six_rounds_keep_native_provider_failure_feedback(
    shared_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch,
):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history

    async def no_provider(*_args, **_kwargs):
        return ""

    async def core_config(*_args, **_kwargs):
        return {"search_engine": "auto"}

    monkeypatch.setattr("app.services.agent_tool_domains.web_mcp._get_searxng_url", no_provider)
    monkeypatch.setattr("app.services.agent_tool_domains.web_mcp._get_tool_config", core_config)
    n = shared_memory.n
    session, run = await new_owner_work(n, owner_sessionmaker)
    for index in range(6):
        output, _ = await memory_call(n, app_user_sessionmaker, "web_search",
            {"query": "local acceptance"}, session=session, run=run,
            command=f"Search the web, round {index + 1}", include_history=True)
        assert '"error_class": "provider_unavailable"' in output, output
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session)
        messages = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=7, messages=messages, tools=[],
            provider="minimax", model="local-fake")
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = model.model_request_snapshot_json
        assert all(entry["status"] == "bound" and entry["code"] != "model_input_source_unbound"
            for entry in snapshot["input_bindings"]["messages"]), snapshot["input_bindings"]
        results = [message for message in snapshot["wire_request"]["messages"] if message["role"] == "tool"]
        assert len(results) == 6
        assert all('"error_class": "provider_unavailable"' in message["content"] for message in results)
