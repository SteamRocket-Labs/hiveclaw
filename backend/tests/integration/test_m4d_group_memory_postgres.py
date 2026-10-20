"""Group memory retains its original installation, audience and live sources."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def group_memory(shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.models.channel_config import ChannelConfig
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService

    s = shared_memory
    n = s.n
    members = {"memory-author", "memory-member"}

    async def current_members(_self, *_args, **_kwargs):
        return set(members)

    async def no_worker_notification(**_kwargs):
        return None

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", current_members)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_worker_notification)
    async with owner_sessionmaker() as db:
        config = ChannelConfig(id=uuid4(), tenant_id=n.tenant, agent_id=n.agent, channel_type="feishu",
            app_id="reference-memory-installation", app_secret="reference-memory-secret", is_configured=True)
        grant = ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.foreign,
            resource_type="agent", resource_id=n.agent, actions=["read", "execute", "manage"], effect="allow")
        db.add_all([config, grant])
        company_access = await db.scalar(select(AgentPermission).where(AgentPermission.agent_id == n.agent,
            AgentPermission.scope_type == "company"))
        company_access.scope_type, company_access.scope_id = "user", n.user
        await db.flush()
        for user_id, subject in ((n.user, "memory-author"), (n.foreign, "memory-member")):
            bound = await bind_authenticated_self_channel_principal(db, tenant_id=n.tenant, config=config,
                provider_subject_id=subject, user_id=user_id, actor_user_id=user_id)
            assert bound.actor.id == user_id
        grant.actions = ["read", "execute"]
        company_access.scope_type, company_access.scope_id = "company", None
        group = await find_or_create_shared_group_session(db, tenant_id=n.tenant, agent_id=n.agent,
            installation_ref=str(config.id), chat_id="reference-memory-group", first_message_title="Group memory work")
        await db.commit()
    return SimpleNamespace(s=s, n=n, session=group.id, members=members, runs={}, rounds={})


async def group_call(g, app_user_sessionmaker, tool_name, arguments, *, user_id=None, command=None, include_history=False):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.session_model_round import bind_round_inputs, prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation
    from app.services.session_v2_persistence import accept_human_input
    from app.services.shared_group_authority import resolve_shared_group_web_authority
    from app.services.web_chat_runtime import _persist_tool_call

    async def allow(**_kwargs):
        return HookResult()

    n = g.n
    user_id = user_id or n.user
    input_id = uuid4()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        session = await db.get(ChatSession, g.session)
        authority = await resolve_shared_group_web_authority(db, session=session, user=await db.get(User, user_id),
            action="mutate_session_input")
        run_id = g.runs.get(user_id)
        task = await db.get(RuntimeTask, run_id) if run_id else None
        intent = {"kind": "steer_current_turn" if task else "start_turn", "input_id": str(input_id),
            "idempotency_key": f"group-memory:{input_id}", "session_id": str(session.id),
            "content_parts": [{"type": "text", "text": command or arguments.get("content") or "Recall our group memory"}]}
        if task:
            intent.update(expected_turn_id=task.metadata_json["turn_id"], expected_run_id=str(task.id),
                terminal_fallback="queue_next_turn")
        admitted = await accept_human_input(db, authority=authority, intent=intent)
        await db.commit()
        await run_user_prompt_admission(db, authority=authority, input_id=admitted.input_id,
            worker_id="group-memory-reference", hook_executor=allow)
        if task:
            await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
        else:
            await recover_admitted_session_inputs_once(db, tenant_id=n.tenant, worker_id="group-memory-dispatch",
                admission_ids=(uuid5(admitted.command_id, "input-admission:revision:1"),))
            run_id = uuid5(input_id, "session-v2-runtime-run")
            task = await db.get(RuntimeTask, run_id)
            assert task is not None
            assert task.root_user_id == user_id
            task.status, task.claimed_by = "running", "group-memory-reference-worker"
            task.claim_version, task.attempt_count = 1, 1
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            g.runs[user_id] = run_id
        turn = task.metadata_json["turn_id"]
        index = g.rounds.get(run_id, 0) + 1
        g.rounds[run_id] = index
        bound = await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session.id,
            run_id=run_id, turn_id=turn, round_index=index)
        messages = _mid_run_items_to_user_messages(bound)
        if include_history:
            from app.services.llm_client import LLMMessage
            from app.services.session_semantic_history import load_session_semantic_history

            history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=session.id, reader_run_id=run_id)
            messages = [*[LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages], *messages]
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session.id,
            run_id=run_id, turn_id=turn, round_index=index, messages=messages,
            tools=[{"type": "function", "function": {"name": tool_name}}], provider="synthetic", model="group-memory-reference")
        call_id = f"group-memory-{index}"
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session.id,
            run_id=run_id, turn_id=turn, round_index=index, provider_request_id=request_id,
            response={"content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": tool_name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session.id,
            run_id=run_id, turn_id=turn, round_index=index, provider_request_id=request_id)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=session.id,
            run_id=run_id, provider_request_id=request_id, provider_tool_use_id=call_id, tool_name=tool_name, arguments=arguments)
        await db.commit()
    trace = {}
    raw = await n.service.execute(tool_name, arguments, agent_id=n.agent, user_id=user_id, session_id=str(g.session),
        runtime_task_id=str(run_id), turn_id=turn, tool_call_id=call_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
    await _persist_tool_call(agent_id=n.agent, user_id=user_id, session_id=str(g.session), data={
        "name": tool_name, "args": arguments, "status": "done", "result": str(raw), "tool_call_id": call_id,
        "provider_request_id": request_id, "runtime_task_id": str(run_id), "tool_execution_evidence": _execution_evidence(trace)})
    return str(raw), invocation


async def save_group(g, factory):
    from app.models.organization_identity import ContentInputReceipt

    result, invocation = await group_call(g, factory, "save_memory",
        {"content": "Remember our group release cadence GROUP_RELEASE_MARKER", "category": "project"})
    assert "Saved to explicit memory overlay" in result, result
    async with tenant_scoped_session(g.n.tenant, session_factory=factory) as db:
        row = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        assert row.authority_json["audience_kind"] == "group"
        assert row.authority_json["source_session_id"] == str(g.session)
        g.entry_id = row.authority_json["entry_id"]
        g.source_id = row.id
    return invocation


async def finish_group_work(g, factory, *, user_id):
    from app.services.llm_client import LLMMessage
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import _finalize_web_chat_run_with_assistant
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    async def reference_summary(_messages, **_kwargs):
        return "The group recorded a release cadence for future work."

    async def no_advisory_hook(*_args, **_kwargs):
        return None

    run = g.runs[user_id]
    index = g.rounds[run] + 1
    content = "The requested group memory work is recorded."
    async with tenant_scoped_session(g.n.tenant, session_factory=factory) as db:
        task = await db.get(RuntimeTask, run)
        turn = task.metadata_json["turn_id"]
        history = await load_session_semantic_history(db, tenant_id=g.n.tenant, agent_id=g.n.agent,
            session_id=g.session, reader_run_id=run)
        messages = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
        request_id = await prepare_model_request(db, tenant_id=g.n.tenant, agent_id=g.n.agent, session_id=g.session,
            run_id=run, turn_id=turn, round_index=index, messages=messages, tools=[], provider="synthetic", model="group-memory-reference")
        await seal_model_response(db, tenant_id=g.n.tenant, agent_id=g.n.agent, session_id=g.session,
            run_id=run, turn_id=turn, round_index=index, provider_request_id=request_id,
            response={"content": content, "tool_calls": [], "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=g.n.tenant, agent_id=g.n.agent, session_id=g.session,
            run_id=run, turn_id=turn, round_index=index, provider_request_id=request_id)
        await db.commit()
    fence = set_runtime_task_fence(task_id=run, worker_id="group-memory-reference-worker", claim_version=1)
    try:
        assert await _finalize_web_chat_run_with_assistant(run_uuid=run, agent_id=g.n.agent, user_id=user_id,
            session_id=str(g.session), content=content, thinking=None, status="completed", result_summary=content)
    finally:
        reset_runtime_task_fence(fence)
    processor = WebTerminalBoundaryProcessor(session_factory=factory, data_root=g.n.root.parent,
        summary_generator=reference_summary, emit_advisory_hook=no_advisory_hook)
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=factory).drain_once(
        tenant_id=g.n.tenant, worker_id="group-memory-terminal-boundary", canonical_validator=processor.validate,
        process_callback=processor, task_types=("web_chat_turn",))
    assert boundary == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}, boundary


async def test_another_member_can_correct_group_memory_in_the_original_group_work(
    group_memory, app_user_sessionmaker,
):
    from app.models.organization_identity import ContentInputReceipt

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    await finish_group_work(g, app_user_sessionmaker, user_id=n.user)
    read, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", {"ids": [g.entry_id]}, user_id=n.foreign,
        include_history=True)
    assert "GROUP_RELEASE_MARKER" in read, read
    corrected, invocation = await group_call(g, app_user_sessionmaker, "update_memory",
        {"memory_id": g.entry_id, "content": "Our group cadence MEMBER_CORRECTION_MARKER", "reason": "incorrect"},
        user_id=n.foreign, include_history=True)
    assert "Updated explicit memory" in corrected, corrected
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        row = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        assert row.authority_json["owner_user_id"] == str(n.foreign)
        assert row.authority_json["audience_kind"] == "group"
        assert any(ref["source_id"] == str(g.source_id) for ref in row.dependencies_json), row.dependencies_json


async def test_current_group_member_can_save_and_another_member_can_recall_without_private_context(
    group_memory, app_user_sessionmaker,
):
    g = group_memory
    await save_group(g, app_user_sessionmaker)
    other = str(await g.n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=g.n.agent, user_id=g.n.foreign, session_id=str(g.s.second_session),
        runtime_task_id=str(g.s.second_run), emit_runtime_hooks=False))
    assert "GROUP_RELEASE_MARKER" in other and "ORCHID_PRIVATE_MARKER" not in other, other
    g.members.remove("memory-member")
    revoked = str(await g.n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=g.n.agent, user_id=g.n.foreign, session_id=str(g.s.second_session),
        runtime_task_id=str(g.s.second_run), emit_runtime_hooks=False))
    assert "GROUP_RELEASE_MARKER" not in revoked, revoked


@pytest.mark.parametrize("surface", ["load", "raw_file", "automatic", "resident", "group_load", "group_automatic", "group_resident"])
async def test_group_memory_reaches_each_authorized_consumer_without_private_memory(
    group_memory, app_user_sessionmaker, monkeypatch, surface,
):
    from app.memory.retriever import MemoryRetriever
    from app.memory.activation import ActivationContext
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    in_group = surface.startswith("group_")
    user, session, run = (n.user, g.session, g.runs[n.user]) if in_group else (n.foreign, g.s.second_session, g.s.second_run)
    if surface == "group_load":
        result, _invocation = await group_call(g, app_user_sessionmaker, "load_memory",
            {"ids": [g.entry_id, g.s.entry_id]}, include_history=True)
    elif surface in {"load", "raw_file"}:
        name = "read_file" if surface == "raw_file" else "load_memory"
        arguments = {"path": f"memory/explicit/entries/{g.entry_id}.md"} if surface == "raw_file" else {"ids": [g.entry_id, g.s.entry_id]}
        result = str(await n.service.execute(name, arguments, agent_id=n.agent,
            user_id=user, session_id=str(session), runtime_task_id=str(run), emit_runtime_hooks=False))
    else:
        observed = []

        async def choose(_self, *, items, **_kwargs):
            observed.extend(item.content for item in items)
            return [item.metadata["selection_candidate_id"] for item in items], "Use the authorized group release fact"

        monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
        if surface.endswith("resident"):
            from app.services import memory_service

            async def model(*_args, **_kwargs):
                return {"provider": "reference", "model": "local-reference"}

            monkeypatch.setattr(memory_service, "_get_rerank_model_config", model)
            result = await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(session),
                runtime_task_id=str(run), current_user_id=user, query="release")
        else:
            activation = ActivationContext(query="release", principal_stack=PrincipalStack(
                current_user=Principal(PrincipalRole.CURRENT_USER, str(user)),
                direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
            items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(session), str(n.tenant),
                runtime_task_id=str(run), activation_context=activation,
                rerank_model_config={"provider": "reference", "model": "local-reference"})
            result = "\n".join(item.content for item in items)
        assert all("ORCHID_PRIVATE_MARKER" not in body for body in observed), observed
    assert "GROUP_RELEASE_MARKER" in result and "ORCHID_PRIVATE_MARKER" not in result, result


async def test_group_source_unavailability_is_visible_and_does_not_hide_available_private_memory(
    group_memory, app_user_sessionmaker, monkeypatch,
):
    from app.services.feishu_service import FeishuService

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)

    async def offline(*_args, **_kwargs):
        raise OSError("Reference group membership service unavailable")

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", offline)
    for user, session, run in ((n.user, n.session, n.run), (n.foreign, g.s.second_session, g.s.second_run)):
        result = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
            agent_id=n.agent, user_id=user, session_id=str(session), runtime_task_id=str(run), emit_runtime_hooks=False))
        assert "GROUP_RELEASE_MARKER" not in result and "unavailable" in result, result
        assert ("ORCHID_PRIVATE_MARKER" in result) == (user == n.user), result


async def test_group_correction_changes_later_member_work_and_privacy_withdrawal_reaches_derived_memory(
    group_memory, app_user_sessionmaker,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionModelResult
    from app.services.explicit_memory_authority import latest_memory_receipt
    from app.services.session_model_inputs import _model_dependencies
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    loaded, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", {"ids": [g.entry_id]}, include_history=True)
    assert "GROUP_RELEASE_MARKER" in loaded, loaded
    changed, invocation = await group_call(g, app_user_sessionmaker, "update_memory",
        {"memory_id": g.entry_id, "content": "Our group release cadence is GROUP_REVISED_MARKER", "reason": "incorrect"},
        include_history=True)
    assert "Updated explicit memory" in changed, changed
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == invocation.provider_request_id))
        dependencies, complete = _model_dependencies(model)
        assert complete and any(d["source_id"] == str(g.source_id) for d in dependencies), dependencies
        revised = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        assert revised.authority_json["audience_kind"] == "group"
        old = await latest_memory_receipt(db, n.tenant, n.agent, g.entry_id)
        assert old.authority_json["state"] == "retired"
        revised_id = revised.authority_json["entry_id"]
    read, _invocation = await memory_call(n, app_user_sessionmaker, "search_memory", {"query": "release", "scope": "facts"},
        user=n.foreign, session=g.s.second_session, run=g.s.second_run)
    assert "GROUP_REVISED_MARKER" in read and "GROUP_RELEASE_MARKER" not in read, read
    used, use_invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "For my next release note use GROUP_REVISED_MARKER", "category": "project"},
        user=n.foreign, session=g.s.second_session, run=g.s.second_run, include_history=True)
    assert "Saved to explicit" in used, used
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        derived = await db.get(ContentInputReceipt, uuid5(use_invocation.id, "explicit-memory-source"))
        assert derived.authority_json["audience_kind"] == "private"
        assert any(d["source_id"] == str(revised.id) for d in derived.dependencies_json), derived.dependencies_json
        derived_id = derived.authority_json["entry_id"]
    retired, _invocation = await group_call(g, app_user_sessionmaker, "retire_memory", {"memory_id": g.entry_id, "reason": "privacy"})
    assert json.loads(retired)["status"] == "completed", retired
    after = str(await n.service.execute("load_memory", {"ids": [revised_id, derived_id]}, agent_id=n.agent,
        user_id=n.foreign, session_id=str(g.s.second_session), runtime_task_id=str(g.s.second_run), emit_runtime_hooks=False))
    assert "GROUP_REVISED_MARKER" not in after, after


async def test_private_correction_cannot_disclose_into_group_memory(group_memory, app_user_sessionmaker):
    from app.models.organization_identity import ContentInputReceipt
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    before = {p.relative_to(n.root).as_posix(): p.read_bytes() for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}
    result, invocation = await memory_call(n, app_user_sessionmaker, "update_memory",
        {"memory_id": g.entry_id, "content": "My private correction PRIVATE_GROUP_PROMOTION_MARKER", "reason": "incorrect"})
    assert "Updated explicit" not in result and "PRIVATE_GROUP_PROMOTION_MARKER" not in result, result
    assert "Return to the group where this memory was recorded" in result, result
    after = {p.relative_to(n.root).as_posix(): p.read_bytes() for p in (n.root / "memory/explicit").rglob("*") if p.is_file()}
    assert before == after
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        assert await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source")) is None


async def test_group_cannot_consume_another_groups_memory_even_when_requester_belongs_to_both(
    group_memory, app_user_sessionmaker,
):
    from app.services.channel_session import find_or_create_shared_group_session

    g = group_memory
    await save_group(g, app_user_sessionmaker)
    async with tenant_scoped_session(g.n.tenant, session_factory=app_user_sessionmaker) as db:
        origin = await db.get(ChatSession, g.session)
        other = await find_or_create_shared_group_session(db, tenant_id=g.n.tenant, agent_id=g.n.agent,
            installation_ref=origin.group_installation_ref, chat_id="other-reference-group", first_message_title="Other group")
        await db.commit()
    elsewhere = SimpleNamespace(n=g.n, session=other.id, runs={}, rounds={})
    result, _invocation = await group_call(elsewhere, app_user_sessionmaker, "load_memory", {"ids": [g.entry_id]})
    assert "GROUP_RELEASE_MARKER" not in result and "Memory Availability" not in result, result


async def test_membership_withdrawal_during_model_selection_withholds_selected_body(
    group_memory, app_user_sessionmaker, monkeypatch,
):
    from app.memory.retriever import MemoryRetriever
    from app.memory.activation import ActivationContext
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    observed = []

    async def select_then_leave(_self, *, items, **_kwargs):
        observed.extend(item.content for item in items)
        g.members.remove("memory-member")
        return [item.metadata["selection_candidate_id"] for item in items], "Use the group fact"

    monkeypatch.setattr(MemoryRetriever, "_select_with_model", select_then_leave)
    principal = ActivationContext(query="release", principal_stack=PrincipalStack(
        current_user=Principal(PrincipalRole.CURRENT_USER, str(n.foreign)),
        direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
    items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(g.s.second_session), str(n.tenant),
        runtime_task_id=str(g.s.second_run), activation_context=principal,
        rerank_model_config={"provider": "reference", "model": "local-reference"})
    assert any("GROUP_RELEASE_MARKER" in body for body in observed), observed
    assert all("GROUP_RELEASE_MARKER" not in item.content for item in items), items


async def test_nonmember_administrator_can_inspect_derived_business_file_but_cannot_use_group_sources_in_agent_work(
    group_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.models.chat_artifact import ChatArtifact
    from app.models.organization_identity import ContentInputReceipt
    from app.models.security_audit import SecurityAuditEvent
    from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [g.entry_id]},
        user=n.foreign, session=g.s.second_session, run=g.s.second_run)
    assert "GROUP_RELEASE_MARKER" in loaded
    path = "workspace/group-release.md"
    written, _invocation = await memory_call(n, app_user_sessionmaker, "write_file",
        {"path": path, "content": "Release note uses GROUP_RELEASE_MARKER."}, user=n.foreign,
        session=g.s.second_session, run=g.s.second_run, include_history=True)
    assert (n.root / path).read_text() == "Release note uses GROUP_RELEASE_MARKER.", written
    g.members.remove("memory-author")
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    runtime = str(await n.service.execute("load_memory", {"ids": [g.entry_id]}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert "GROUP_RELEASE_MARKER" not in runtime, runtime
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        source = await db.get(ContentInputReceipt, g.source_id)
        request = ToolExecutionRequest("save_memory", {}, ToolExecutionContext(agent_id=n.agent, user_id=n.user,
            tenant_id=str(n.tenant), workspace=n.root, session_id=str(n.session), runtime_task_id=str(n.run)))
        with pytest.raises(ContentInputReceiptError) as denied:
            await validate_runtime_input_dependencies(db, request, source.dependencies_json)
        assert denied.value.status_code == 403
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))
        assert artifact is not None
        artifact_id = artifact.id
    inspected = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact_id}/content",
        params={"operator_view": True, "operator_reason": "Inspect the group-derived release note"})
    assert inspected.status_code == 200 and "GROUP_RELEASE_MARKER" in inspected.text, inspected.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        events = list((await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.actor_id == n.user,
            SecurityAuditEvent.event_type == "session.scoped_business_admin_access"))).all())
        assert events and all("GROUP_RELEASE_MARKER" not in json.dumps(event.details) for event in events)


async def test_group_write_losing_membership_after_file_commit_can_be_cancelled_without_repeating_review(
    group_memory, app_user_sessionmaker, monkeypatch,
):
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.organization_identity import ContentInputReceipt
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    review_calls = []

    async def reviewer(content, *, category, **_kwargs):
        review_calls.append(content)
        return MemoryWriteDecision(original_content=content, content=content, category=category, sensitivity="PL1_public")

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reviewer)
    original = AgentAssetTransaction.commit
    withdrawn = []

    def commit_and_leave(self):
        receipt = original(self)
        if self.operation == "explicit_memory_source_write" and not withdrawn:
            withdrawn.append(True)
            g.members.remove("memory-author")
        return receipt

    monkeypatch.setattr(AgentAssetTransaction, "commit", commit_and_leave)
    result, invocation = await group_call(g, app_user_sessionmaker, "update_memory",
        {"memory_id": g.entry_id, "content": "Group cadence UNPUBLISHED_GROUP_CHANGE_MARKER", "reason": "incorrect"})
    operation_id = str(uuid5(invocation.id, "explicit-memory-source"))
    assert "Updated explicit" not in result and "UNPUBLISHED_GROUP_CHANGE_MARKER" not in result and operation_id in result, result
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        row = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        assert row.authority_json["state"] == "prepared"
    cancelled, _invocation = await memory_call(n, app_user_sessionmaker, "reconcile_memory_write",
        {"operation_id": operation_id, "action": "cancel"})
    assert json.loads(cancelled)["status"] == "cancelled", cancelled
    other = str(await n.service.execute("load_memory", {"ids": [g.entry_id]}, agent_id=n.agent, user_id=n.foreign,
        session_id=str(g.s.second_session), runtime_task_id=str(g.s.second_run), emit_runtime_hooks=False))
    assert "GROUP_RELEASE_MARKER" in other and "UNPUBLISHED_GROUP_CHANGE_MARKER" not in other, other
    assert len(review_calls) == 1, review_calls
