"""A parent reads a colleague's tool-backed answer through its exact delegation."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_r22a_owned_delegation_result_postgres import (
    RESULT as FILE_RESULT, _deliver_continuation, _parent_turn,
)
from tests.integration.test_r27a_delegation_result_source_readers_postgres import (
    _member_file_result, _reader_arguments, _task_body,
)
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")
ANSWER = "R28A_COLLEAGUE_ANSWER: clarify the goal, check evidence, and report the result."
READERS = ("check_async_task", "task_output", "list_async_tasks")


async def _tool_result(owner, app_factory, monkeypatch, tmp_path, tool):
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed = await _member_file_result(owner, app_factory, monkeypatch, tmp_path)
    setup_return = await _deliver_continuation(owner, app_factory, seed)
    setup_client = ScriptedLLMClient([{"content": "The initial file task is complete."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: setup_client)
    await _execute(owner, setup_return, "r28a-setup-return")
    async with owner() as db:
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.memory.read",
            allowed=True, requires_approval=False))
        await db.commit()
    arguments = {"query": "R28A_SUGGESTIONS", "scope": "facts"} if tool == "search_memory" else {"ids": ["r28a-missing-memory"]}

    parent_id, _ = await _parent_turn(owner, seed, monkeypatch, [
        {"tool_calls": [_call("delegate_to_agent", {"agent_name": "R22a colleague",
            "message": "Check your memory or operating instructions, then propose three suggestions.",
            "expected_output": "Three suggestions", "tool_profile": "memory_readonly"}, "r28a-delegate")],
            "finish_reason": "tool_calls"},
        {"content": "The colleague is reading the suggestions."},
    ])
    async with owner() as db:
        task = (await db.scalars(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == parent_id,
            RuntimeTask.task_type == "delegation"))).one()
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "r28a-colleague", 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        seed["child_id"], seed["child_session_id"] = task.id, UUID(task.child_session_id)
        await db.commit()
    child_client = ScriptedLLMClient([
        {"tool_calls": [_call(tool, arguments, "r28a-child-tool")], "finish_reason": "tool_calls"},
        {"content": ANSWER},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    token = set_runtime_task_fence(task_id=seed["child_id"], worker_id="r28a-colleague", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(seed["child_id"].hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
    calls = await _tool_results(owner, seed["child_id"])
    content = calls["r28a-child-tool"]["content"]
    if tool == "search_memory":
        assert "没有找到" in content and "R28A_SUGGESTIONS" in content, content
    else:
        assert "没有找到可读取的记忆" in content and "r28a-missing-memory" in content, content
    assert len(child_client.requests) == 2
    feedback = [message for message in child_client.requests[1]["messages"] if message.get("role") == "tool"]
    assert any(message["content"] == content for message in feedback)
    async with owner() as db:
        task = await db.get(RuntimeTask, seed["child_id"])
        session = await db.get(ChatSession, seed["child_session_id"])
        invocation = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == task.id))).one()
        assert task.status == "completed" and task.result_summary == ANSWER
        assert (task.tenant_id, task.parent_agent_id, task.child_agent_id, task.root_user_id) == (
            seed["tenant_id"], seed["agent_id"], seed["child_agent_id"], seed["user_id"])
        assert UUID(task.parent_session_id) == seed["session_id"]
        assert session.session_kind == "delegation_run" and session.source_channel == "agent"
        assert session.peer_agent_id == seed["agent_id"] and session.parent_session_id == seed["session_id"]
        assert session.user_id == seed["user_id"] and session.audience_kind is None
        assert invocation.tool_name == tool and invocation.effect_state == "effect_committed"
        assert invocation.permission_state == "not_required"
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        seed["tool_source"] = next(ref for ref in model.model_request_snapshot_json["input_bindings"]["dependencies"]
            if ref["source_kind"] == "session_tool_invocation" and ref["source_id"] == str(invocation.id))

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_factory, data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_factory).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r28a-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1, drained
    async with owner() as db:
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))).one()
        seed["result_ref"], seed["result_id"] = notice.result_ref, notice.result_object_id
        result = await db.get(RuntimeResultObject, seed["result_id"])
        assert ANSWER in bytes(result.payload_bytes).decode(result.encoding)
    return seed


def _observe_denials(monkeypatch):
    from app.services import content_input_receipts as receipts

    resolve = receipts._resolve_session_input_source

    async def observed(*args, **kwargs):
        try:
            return await resolve(*args, **kwargs)
        except receipts.ContentInputReceiptError as exc:
            cause = exc
            frames = []
            while cause is not None:
                tb = cause.__traceback__
                while tb is not None:
                    if tb.tb_frame.f_code.co_name == "_resolve_session_input_source":
                        frames.append(tb.tb_lineno)
                    tb = tb.tb_next
                cause = cause.__cause__
            print("R28A_SESSION_REFUSAL", json.dumps({"code": exc.code, "lines": frames}))
            raise

    monkeypatch.setattr(receipts, "_resolve_session_input_source", observed)


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
@pytest.mark.parametrize("reader", READERS)
async def test_parent_continuation_reads_tool_backed_colleague_answer(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, reader,
):
    import app.runtime.invoker as invoker

    seed = await _tool_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool)
    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    client = ScriptedLLMClient([
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-result")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call(reader, _reader_arguments(reader, seed["child_id"]), "r28a-task")],
            "finish_reason": "tool_calls"},
        {"content": "The colleague's suggestions are available."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    _observe_denials(monkeypatch)
    await _execute(owner_sessionmaker, return_id, "r28a-parent-read")
    results = await _tool_results(owner_sessionmaker, return_id)
    direct = json.loads(results["r28a-result"]["content"])
    checked = _task_body(reader, json.loads(results["r28a-task"]["content"]), seed["child_id"])
    print("R28A_READER", json.dumps({"tool": tool, "reader": reader, "direct_status": direct["status"],
        "task_publication": checked.get("result_publication"), "answer_present": ANSWER in json.dumps(checked)}))
    assert direct["status"] == "ok" and direct["complete"] is True, direct
    assert direct["dependency_coverage"] == {"complete": True}, direct
    assert ANSWER in direct["content"] and checked["result"] == ANSWER
    assert checked["status"] == "completed" and "result_publication" not in checked
    assert len(client.requests) == 3
    assert ANSWER not in json.dumps(client.requests[0])
    for index in (1, 2):
        assert ANSWER in json.dumps(client.requests[index])
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)
            .order_by(SessionModelResult.round_id)))
        assert len(models) == 3
        assert ANSWER not in json.dumps(models[0].model_request_snapshot_json["wire_request"])
        for model in models[1:]:
            assert ANSWER in json.dumps(model.model_request_snapshot_json["wire_request"])


@pytest.mark.parametrize("change", ["access", "deactivated", "deleted", "root_user", "parent_agent",
    "parent_session", "child_agent", "child_session", "task_type", "ordinary_session", "tenant", "memory_record"])
async def test_tool_source_requires_current_access_and_exact_delegation_binding(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,
):
    from app.models.tenant import Tenant
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.runtime_result_sources import validate_runtime_result_sources

    seed = await _tool_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, "search_memory")
    principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
        session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["child_id"])
        child = await db.get(Agent, seed["child_agent_id"])
        session = await db.get(ChatSession, seed["child_session_id"])
        if change == "access":
            child.owner_user_id = child.creator_id = child.sponsor_user_id = seed["other_user_id"]
        elif change in {"deactivated", "deleted"}:
            setattr(child, f"{change}_at", datetime.now(timezone.utc))
        elif change == "root_user":
            task.root_user_id = seed["other_user_id"]
        elif change == "parent_agent":
            task.parent_agent_id = seed["child_agent_id"]
        elif change == "parent_session":
            task.parent_session_id = str(uuid4())
        elif change == "child_agent":
            task.child_agent_id = seed["agent_id"]
        elif change == "child_session":
            task.child_session_id = str(uuid4())
        elif change == "task_type":
            task.task_type = "subagent"
        elif change == "ordinary_session":
            session.session_kind = "human_chat"
        elif change == "memory_record":
            record = await db.get(InvocationSpan, uuid5(UUID(seed["tool_source"]["source_id"]),
                "explicit-memory-tool-result"))
            assert record is not None and record.metadata_json["memory_result"]["sources"] == []
            record.metadata_json = {"memory_result": {**record.metadata_json["memory_result"], "content_hash": "0" * 64}}
        else:
            tenant_id = uuid4()
            db.add(Tenant(id=tenant_id, name="Other tenant", slug=f"r28a-{tenant_id.hex}"))
            await db.flush()
            task.tenant_id = tenant_id
        await db.commit()

    # Bare child sources remain denied on the parent-actor path.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        with pytest.raises(ContentInputReceiptError) as refused:
            await _validate_input_dependencies(db, seed["tenant_id"], [seed["tool_source"]], runtime_request=principal)
        assert refused.value.code == "content_source_access_denied" and refused.value.status_code == 403
        result = await db.get(RuntimeResultObject, seed["result_id"])
        try:
            allowed = await validate_runtime_result_sources(db, principal, result)
        except ContentInputReceiptError as exc:
            assert exc.code == ("runtime_result_sources_unbound" if change in {"child_agent", "child_session"}
                else "content_source_access_denied")
            allowed = False
        assert allowed is False

    calls = [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-denied-result")]
    calls.extend(_call(reader, _reader_arguments(reader, seed["child_id"]), f"r28a-denied-{reader}") for reader in READERS)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": calls, "finish_reason": "tool_calls"},
        {"content": "The colleague's source is unavailable to this reader."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    assert len(results) == 4
    if change not in {"parent_session", "child_agent", "child_session", "task_type", "tenant"}:
        for call in calls:
            assert ANSWER not in results[call["id"]]["content"]
    assert ANSWER not in json.dumps(client.requests)
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        assert len(models) == 2
        assert all(ANSWER not in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models)
    print("R28A_BINDING_REFUSED", change, "read_runtime_result", *READERS)


async def test_colleague_workspace_source_reads_use_the_existing_member_path(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    calls = [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-workspace-result")]
    calls.extend(_call(reader, _reader_arguments(reader, seed["child_id"]), f"r28a-workspace-{reader}") for reader in READERS)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": calls, "finish_reason": "tool_calls"}, {"content": "The file summary is readable."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    direct = json.loads(results["r28a-workspace-result"]["content"])
    assert direct["status"] == "ok" and FILE_RESULT in direct["content"], direct
    assert direct["dependency_coverage"] == {"complete": True}, direct
    for reader in READERS:
        checked = _task_body(reader, json.loads(results[f"r28a-workspace-{reader}"]["content"]), seed["child_id"])
        assert checked["result"] == FILE_RESULT and "result_publication" not in checked
    assert FILE_RESULT in json.dumps(client.requests[1])


HIT_MEMORY = "QUASARFACTTAG: clarify the goal; check the evidence; report the result."
UNRETURNED_MEMORY = "ZIRCONFACTTAG: prepare a separate payroll checklist."
TEAM_MEMORY = "QUASARFACTTAG: the team release cadence is weekly."


async def _memory_group(n, owner, monkeypatch):
    from types import SimpleNamespace
    from app.models.channel_config import ChannelConfig
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService

    members = {"r28a-memory-member"}

    async def current_members(_self, *_args, **_kwargs):
        return set(members)

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", current_members)
    async with owner() as db:
        config = ChannelConfig(id=uuid4(), tenant_id=n.tenant, agent_id=n.agent, channel_type="feishu",
            app_id="r28a-memory-installation", app_secret="reference-memory-secret", is_configured=True)
        db.add(config)
        await db.flush()
        await bind_authenticated_self_channel_principal(db, tenant_id=n.tenant, config=config,
            provider_subject_id="r28a-memory-member", user_id=n.user, actor_user_id=n.user)
        session = await find_or_create_shared_group_session(db, tenant_id=n.tenant, agent_id=n.agent,
            installation_ref=str(config.id), chat_id="r28a-memory-group", first_message_title="Memory source work")
        await db.commit()
    return SimpleNamespace(n=n, session=session.id, members=members, runs={}, rounds={})


async def _team_memory(n, owner, app_factory, monkeypatch):
    from app.api.memory import TeamMemoryUpsert, upsert_team_memory
    from app.database import tenant_scoped_session
    from app.models.agent_team import AgentTeam
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services.team_memory_runtime import source_dependency
    from tests.api.test_memory_api import _review_team_memory_semantics

    _review_team_memory_semantics.__wrapped__(monkeypatch)
    team_id = uuid4()
    workspace = f"agent_team:{team_id}"
    async with owner() as db:
        db.add(AgentTeam(id=team_id, tenant_id=n.tenant, lead_agent_id=n.agent,
            parent_session_id=n.session, name="Colleague release team", created_by_user_id=n.user))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        user = await db.get(User, n.user)
        saved = await upsert_team_memory(TeamMemoryUpsert(workspace_key=workspace, key="release",
            title="Team release", content=TEAM_MEMORY), current_user=user, db=db)
        assert saved["content"] == TEAM_MEMORY
        await db.commit()
        receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "team_memory", ContentInputReceipt.carrier_ref == f"{workspace}\nrelease"))
        assert receipt is not None
        return source_dependency(receipt)


async def _memory_hit_result(owner, app_factory, monkeypatch, tmp_path, tool, *, group=False, extra=False,
    entry_count=1, call_count=1, team=False):
    from types import SimpleNamespace
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.organization_identity import ContentInputReceipt
    from app.services import agent_tools
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.explicit_memory_authority import source_dependency
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work
    from tests.integration.test_m4d_group_memory_postgres import group_call

    seed = await _member_file_result(owner, app_factory, monkeypatch, tmp_path)
    setup_return = await _deliver_continuation(owner, app_factory, seed)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs:
        ScriptedLLMClient([{"content": "The initial file task is complete."}]))
    await _execute(owner, setup_return, "r28a-memory-setup")
    n = SimpleNamespace(tenant=seed["tenant_id"], user=seed["user_id"], agent=seed["child_agent_id"],
        root=seed["data_root"] / str(seed["child_agent_id"]), service=agent_tools._get_tool_runtime_service())
    n.session, n.run = await new_owner_work(n, owner)

    async def review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", review)
    async with owner() as db:
        for capability in ("agent.memory.write", "agent.memory.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    seed["memory_context"] = n
    if group:
        seed["memory_group"] = await _memory_group(n, owner, monkeypatch)
        saved, written = await group_call(seed["memory_group"], app_factory, "save_memory",
            {"content": HIT_MEMORY, "category": "project"}, command=f"Remember this group fact: {HIT_MEMORY}")
    else:
        saved, written = await memory_call(n, app_factory, "save_memory",
            {"content": HIT_MEMORY, "category": "project"}, command=f"Remember this private fact: {HIT_MEMORY}")
    assert "Saved memory" in saved and HIT_MEMORY in saved, saved
    async with owner() as db:
        receipt = await db.get(ContentInputReceipt, uuid5(written.id, "explicit-memory-source"))
        seed["memory_source"] = source_dependency(receipt)
        seed["memory_entry_id"] = receipt.authority_json["entry_id"]
    seed["memory_sources"] = [seed["memory_source"]]
    for index in range(max(entry_count, 2 if extra else 1) - 1):
        content = UNRETURNED_MEMORY if index == 0 else f"Resident memory {index}: retain the release checklist."
        saved_extra, extra_call = await memory_call(n, app_factory, "save_memory",
            {"content": content, "category": "project"}, command=f"Remember this separate fact: {content}")
        assert content in saved_extra, saved_extra
        async with owner() as db:
            extra_receipt = await db.get(ContentInputReceipt, uuid5(extra_call.id, "explicit-memory-source"))
            seed["memory_sources"].append(source_dependency(extra_receipt))
    if extra:
        seed["extra_memory_source"] = seed["memory_sources"][1]
    if team:
        seed["team_source"] = await _team_memory(n, owner, app_factory, monkeypatch)

    parent_id, _ = await _parent_turn(owner, seed, monkeypatch, [
        {"tool_calls": [_call("delegate_to_agent", {"agent_name": "R22a colleague",
            "message": "Read the remembered QUASARFACTTAG suggestions and return three suggestions.",
            "expected_output": "Three suggestions", "tool_profile": "memory_readonly"}, "r28a-hit-delegate")],
            "finish_reason": "tool_calls"}, {"content": "The colleague is reading its remembered suggestions."},
    ])
    async with owner() as db:
        task = (await db.scalars(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == parent_id,
            RuntimeTask.task_type == "delegation"))).one()
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "r28a-hit-worker", 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        seed["child_id"], seed["child_session_id"] = task.id, UUID(task.child_session_id)
        await db.commit()
    arguments = ({"query": "QUASARFACTTAG", "scope": "facts", **({"limit": 1} if extra else {})}
        if tool == "search_memory" else {"ids": [seed["memory_entry_id"]]})
    responses = ([{"tool_calls": [_call(tool, arguments, f"r28a-hit-tool-{index}")
        for index in range(call_count)], "finish_reason": "tool_calls"}] if tool else [])
    child_client = ScriptedLLMClient([*responses, {"content": ANSWER}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    from app.services.memory_service import build_memory_context

    original_memory_context = invoker.build_memory_context
    monkeypatch.setattr(invoker, "build_memory_context", build_memory_context)
    token = set_runtime_task_fence(task_id=seed["child_id"], worker_id="r28a-hit-worker", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(seed["child_id"].hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
        monkeypatch.setattr(invoker, "build_memory_context", original_memory_context)
    calls = await _tool_results(owner, seed["child_id"])
    assert len(calls) == (call_count if tool else 0)
    for index in range(call_count if tool else 0):
        assert HIT_MEMORY in calls[f"r28a-hit-tool-{index}"]["content"]
        assert UNRETURNED_MEMORY not in calls[f"r28a-hit-tool-{index}"]["content"]
        if team:
            assert TEAM_MEMORY in calls[f"r28a-hit-tool-{index}"]["content"]
    assert len(child_client.requests) == (2 if tool else 1)
    if tool:
        assert HIT_MEMORY in json.dumps(child_client.requests[1])
    async with owner() as db:
        task = await db.get(RuntimeTask, seed["child_id"])
        session = await db.get(ChatSession, seed["child_session_id"])
        assert task.status == "completed" and task.result_summary == ANSWER
        assert session.session_kind == "delegation_run" and session.user_id == seed["user_id"]
        invocations = list(await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == task.id)))
        assert len(invocations) == (call_count if tool else 0)
        for invocation in invocations:
            assert invocation.tool_name == tool and invocation.effect_state == "effect_committed"
            proof = await db.get(InvocationSpan, uuid5(invocation.id, "explicit-memory-tool-result"))
            expected = [seed["memory_source"], *([seed["team_source"]] if team else [])]
            assert proof.metadata_json["memory_result"]["sources"] == expected
            if extra and tool == "search_memory":
                assert invocation.effective_arguments_json["limit"] == 1
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id)
            .order_by(SessionModelResult.round_id)))
        for model in models:
            dependencies = model.model_request_snapshot_json["input_bindings"]["dependencies"]
            assert all(source in dependencies for source in seed["memory_sources"])
        # The real resident index is already present before any memory tool runs.
        assert HIT_MEMORY in json.dumps(child_client.requests[0])
        if extra:
            assert UNRETURNED_MEMORY in json.dumps(child_client.requests[0])
            assert UNRETURNED_MEMORY in json.dumps(models[0].model_request_snapshot_json["wire_request"])

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_factory, data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_factory).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r28a-hit-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1, drained
    async with owner() as db:
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))).one()
        seed["result_ref"], seed["result_id"] = notice.result_ref, notice.result_object_id
    return seed


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
@pytest.mark.parametrize("reader", READERS)
async def test_parent_continuation_reads_nonempty_colleague_memory(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, reader,
):
    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool)
    await _read_memory_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, seed, reader)


async def _read_memory_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, seed, reader, *, answer=ANSWER):
    import app.runtime.invoker as invoker

    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    client = ScriptedLLMClient([
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-hit-result")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call(reader, _reader_arguments(reader, seed["child_id"]), "r28a-hit-task")],
            "finish_reason": "tool_calls"}, {"content": "The colleague's remembered suggestions are available."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, return_id, "r28a-hit-parent")
    results = await _tool_results(owner_sessionmaker, return_id)
    direct = json.loads(results["r28a-hit-result"]["content"])
    assert direct["status"] == "ok" and direct["complete"] is True, direct
    assert direct["dependency_coverage"] == {"complete": True}, direct
    assert answer in direct["content"]
    assert len(client.requests) == 3 and answer not in json.dumps(client.requests[0])
    assert all(answer in json.dumps(request) for request in client.requests[1:])
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)
            .order_by(SessionModelResult.round_id)))
        assert len(models) == 3
        assert answer not in json.dumps(models[0].model_request_snapshot_json["wire_request"])
        assert all(answer in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models[1:])
    print("R28A_MEMORY_PARENT_READ", json.dumps({"answer": answer, "reader": reader,
        "dependency_coverage": direct["dependency_coverage"], "provider_and_wire": True,
        "task_result": results["r28a-hit-task"]["content"]}))
    assert answer in results["r28a-hit-task"]["content"], results["r28a-hit-task"]
    checked = _task_body(reader, json.loads(results["r28a-hit-task"]["content"]), seed["child_id"])
    assert checked["result"] == answer
    assert checked["status"] == "completed" and "result_publication" not in checked


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
@pytest.mark.parametrize("change", ["private_owner", "private_owner_mixed", "group_membership", "revoked", "receipt_hash",
    "receipt_authority", "other_agent"])
async def test_published_memory_keeps_member_rules_and_invocation_binding(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, change,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.explicit_memory_authority import memory_ref, validate_browser_memory_dependency
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.runtime_result_sources import _published_owned_delegation_input, validate_runtime_result_sources
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool,
        group=change == "group_membership", extra=change == "private_owner_mixed")
    source = seed.get("extra_memory_source", seed["memory_source"])
    if change == "group_membership":
        from app.models.user import User

        principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
            session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            user = await db.get(User, seed["user_id"])
            await validate_browser_memory_dependency(db, seed["tenant_id"], source, user=user)
            result = await db.get(RuntimeResultObject, seed["result_id"])
            assert await validate_runtime_result_sources(db, principal, result) is True
        seed["memory_group"].members.clear()
    elif change == "revoked":
        retired, _call_row = await memory_call(seed["memory_context"], app_user_sessionmaker, "retire_memory",
            {"memory_id": seed["memory_entry_id"], "reason": "privacy"}, command="Withdraw this memory for privacy")
        assert json.loads(retired)["status"] == "completed", retired
    else:
        async with owner_sessionmaker() as db:
            receipt = await db.get(ContentInputReceipt, UUID(source["source_id"]))
            if change in {"private_owner", "private_owner_mixed"}:
                receipt.authority_json = {**receipt.authority_json, "owner_user_id": str(seed["other_user_id"])}
            elif change == "receipt_authority":
                receipt.authority_json = {**receipt.authority_json, "source_session_id": str(uuid4())}
            elif change == "receipt_hash":
                receipt.content_sha256 = "0" * 64
            elif change == "other_agent":
                agent_id, session_id = uuid4(), uuid4()
                db.add(Agent(id=agent_id, tenant_id=seed["tenant_id"], creator_id=seed["user_id"],
                    owner_user_id=seed["user_id"], name="Other memory employee"))
                await db.flush()
                db.add(ChatSession(id=session_id, tenant_id=seed["tenant_id"], agent_id=agent_id,
                    user_id=seed["user_id"], audience_kind="private"))
                entry = seed["memory_entry_id"]
                target = seed["data_root"] / str(agent_id) / "memory/explicit/entries" / f"{entry}.md"
                target.parent.mkdir(parents=True)
                target.write_bytes((seed["memory_context"].root / "memory/explicit/entries" / f"{entry}.md").read_bytes())
                receipt.carrier_ref = memory_ref(agent_id, entry)
                receipt.authority_json = {**receipt.authority_json, "agent_id": str(agent_id),
                    "source_session_id": str(session_id)}
            await db.commit()
    principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
        session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        task = await db.get(RuntimeTask, seed["child_id"])
        if change == "other_agent":
            await validate_browser_memory_dependency(db, seed["tenant_id"], source, user=user)
            assert await _published_owned_delegation_input(db, source, task) is False
        else:
            with pytest.raises(ContentInputReceiptError) as refused:
                await validate_browser_memory_dependency(db, seed["tenant_id"], source, user=user)
            assert refused.value.status_code == 403
            expected_code = {"group_membership": "content_source_access_denied",
                "revoked": "explicit_memory_source_revoked"}.get(change, "explicit_memory_browser_denied")
            assert refused.value.code == expected_code
        result = await db.get(RuntimeResultObject, seed["result_id"])
        try:
            allowed = await validate_runtime_result_sources(db, principal, result)
        except ContentInputReceiptError as exc:
            assert exc.code == ("explicit_memory_source_unverified" if change == "other_agent" else expected_code)
            allowed = False
        assert allowed is False
    calls = [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-memory-denied")]
    calls.extend(_call(reader, _reader_arguments(reader, seed["child_id"]), f"r28a-memory-{reader}") for reader in READERS)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": calls, "finish_reason": "tool_calls"}, {"content": "The memory source is unavailable."},
    ])
    assert len(await _tool_results(owner_sessionmaker, run_id)) == 4
    assert len(client.requests) == 2 and ANSWER not in json.dumps(client.requests)
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        assert len(models) == 2
        assert all(ANSWER not in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models)


@pytest.mark.parametrize("tool", ["load_memory", "search_memory", None], ids=["load_a", "search_limit_one", "index_only"])
@pytest.mark.parametrize("reader", READERS)
async def test_parent_reads_active_index_memory_not_returned_by_a_call(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, reader,
):
    _observe_memory_denials(monkeypatch)
    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool, extra=True)
    await _read_memory_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, seed, reader)


def _observe_memory_denials(monkeypatch):
    from app.services import explicit_memory_authority as authority

    validate = authority.validate_explicit_memory_dependency

    async def observe_refusal(*args, **kwargs):
        try:
            return await validate(*args, **kwargs)
        except authority.ContentInputReceiptError as exc:
            frames, tb = [], exc.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename.endswith("explicit_memory_authority.py"):
                    frames.append(tb.tb_lineno)
                tb = tb.tb_next
            print("R28A_INDEX_REFUSAL", json.dumps({"code": exc.code, "lines": frames}))
            raise

    monkeypatch.setattr(authority, "validate_explicit_memory_dependency", observe_refusal)


FOLLOWUP_ANSWER = "R28A_FOLLOWUP_ANSWER: apply the remembered checklist to the next release."


async def _memory_followup_result(owner, app_factory, monkeypatch, tmp_path):
    import app.runtime.invoker as invoker
    from app.services.memory_service import build_memory_context
    from app.services.runtime_notification_outbox import produce_terminal_task_completion_notification

    seed = await _memory_hit_result(owner, app_factory, monkeypatch, tmp_path, None, extra=True)
    initial_return = await _deliver_continuation(owner, app_factory, seed)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs:
        ScriptedLLMClient([{"content": "The initial colleague task is complete."}]))
    await _execute(owner, initial_return, "r28a-before-followup")
    send_id, _client = await _parent_turn(owner, seed, monkeypatch, [
        {"tool_calls": [_call("send_agent_session_message", {"child_session_id": str(seed["child_session_id"]),
            "message": "Use your remembered checklist for the next release."}, "r28a-send-followup")],
            "finish_reason": "tool_calls"}, {"content": "The colleague is preparing its follow-up."},
    ])
    sent = json.loads((await _tool_results(owner, send_id))["r28a-send-followup"]["content"])
    assert sent["ok"] is True and sent["status"] == "started", sent
    followup_id = UUID(sent["run_id"])
    async with owner() as db:
        followup = await db.get(RuntimeTask, followup_id)
        assert followup.task_type == "a2a_continuation"
        assert followup.parent_session_id == followup.child_session_id == str(seed["child_session_id"])
        assert (await db.get(ChatSession, seed["child_session_id"])).runtime_task_id == seed["child_id"]
    client = ScriptedLLMClient([
        {"tool_calls": [_call("load_memory", {"ids": [seed["memory_entry_id"]]}, "r28a-followup-load")],
            "finish_reason": "tool_calls"}, {"content": FOLLOWUP_ANSWER},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    original_memory_context = invoker.build_memory_context
    monkeypatch.setattr(invoker, "build_memory_context", build_memory_context)
    try:
        await _execute(owner, followup_id, "r28a-followup-child")
    finally:
        monkeypatch.setattr(invoker, "build_memory_context", original_memory_context)
    assert len(client.requests) == 2
    assert all(content in json.dumps(client.requests[0]) for content in (HIT_MEMORY, UNRETURNED_MEMORY))
    calls = await _tool_results(owner, followup_id)
    assert len(calls) == 1 and HIT_MEMORY in calls["r28a-followup-load"]["content"]
    async with owner() as db:
        followup = await db.get(RuntimeTask, followup_id)
        print("R28A_FOLLOWUP_TERMINAL", json.dumps({"task_type": followup.task_type, "status": followup.status,
            "generation": followup.completion_outbox_generation, "attempts": followup.completion_outbox_attempt_count,
            "error": followup.completion_outbox_last_error, "summary": followup.result_summary}))
        assert followup.status == "completed", followup.result_summary
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == followup_id)))
        assert len(models) == 2
        for model in models:
            snapshot = model.model_request_snapshot_json
            assert all(source in snapshot["input_bindings"]["dependencies"] for source in seed["memory_sources"])
            assert UNRETURNED_MEMORY in json.dumps(snapshot["wire_request"])
        # Drive the existing publisher after the real Session V2 terminal commit.
        assert await produce_terminal_task_completion_notification(db, followup,
            attempted_at=datetime.now(timezone.utc), reconciled=False) is not None
        await db.commit()
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(followup_id)))).one()
        result = await db.get(RuntimeResultObject, notice.result_object_id)
        assert result.source_kind == "a2a_continuation" and FOLLOWUP_ANSWER in bytes(result.payload_bytes).decode(result.encoding)
        seed["child_id"], seed["result_ref"], seed["result_id"] = followup_id, notice.result_ref, result.id
    return seed


async def test_parent_reads_published_colleague_memory_followup(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    _observe_memory_denials(monkeypatch)
    seed = await _memory_followup_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    await _read_memory_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, seed,
        "check_async_task", answer=FOLLOWUP_ANSWER)


async def test_colleague_memory_followup_refuses_another_users_private_memory(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.runtime_result_sources import validate_runtime_result_sources

    seed = await _memory_followup_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        receipt = await db.get(ContentInputReceipt, UUID(seed["extra_memory_source"]["source_id"]))
        receipt.authority_json = {**receipt.authority_json, "owner_user_id": str(seed["other_user_id"])}
        await db.commit()
    principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
        session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        result = await db.get(RuntimeResultObject, seed["result_id"])
        with pytest.raises(ContentInputReceiptError) as refused:
            await validate_runtime_result_sources(db, principal, result)
        assert refused.value.code == "explicit_memory_browser_denied"
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-followup-denied"),
            _call("check_async_task", _reader_arguments("check_async_task", seed["child_id"]), "r28a-followup-task")],
            "finish_reason": "tool_calls"}, {"content": "The follow-up memory is unavailable."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    direct = json.loads(results["r28a-followup-denied"]["content"])
    assert direct["status"] == "source_access_denied" and direct["reason_code"] == "explicit_memory_browser_denied"
    assert len(results) == 2 and len(client.requests) == 2
    assert FOLLOWUP_ANSWER not in json.dumps(client.requests)
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        assert len(models) == 2
        assert all(FOLLOWUP_ANSWER not in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models)


async def test_index_source_check_statements_are_linear_in_entries_and_calls(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from collections import Counter
    from sqlalchemy import event
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.runtime_result_sources import validate_runtime_result_sources

    measured = {}
    for call_count in (1, 4):
        for entry_count in (1, 30):
            with monkeypatch.context() as patch:
                seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, patch,
                    tmp_path / f"entries-{entry_count}-calls-{call_count}", "load_memory",
                    entry_count=entry_count, call_count=call_count)
                principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
                    session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
                statements = []

                def count_statement(_connection, _cursor, statement, _parameters, _context, _many):
                    statements.append(statement)

                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    result = await db.get(RuntimeResultObject, seed["result_id"])
                    engine = db.bind.sync_engine
                    event.listen(engine, "before_cursor_execute", count_statement)
                    try:
                        assert await validate_runtime_result_sources(db, principal, result) is True
                    finally:
                        event.remove(engine, "before_cursor_execute", count_statement)
                counts = Counter("memory_invocations" if "session_tool_invocations" in sql else
                    "memory_receipts" if "content_input_receipts" in sql else "other" for sql in statements)
                measured[entry_count, call_count] = len(statements)
                print("R28A_INDEX_STATEMENTS", json.dumps({"entries": entry_count, "calls": call_count,
                    "statements": len(statements), "kinds": counts}))
                assert len(statements) <= 100 + 3 * entry_count
    for call_count in (1, 4):
        assert measured[30, call_count] - measured[1, call_count] <= 3 * 29, measured
    # Invocation, terminal event, model, session and saved proof checks, plus
    # their recipient checkpoints, have a fixed budget per additional call.
    for entry_count in (1, 30):
        assert measured[entry_count, 4] - measured[entry_count, 1] <= 24 * 3, measured


@pytest.mark.parametrize("source_kind", ["explicit_memory", "session_tool_invocation"])
async def test_published_input_refuses_malformed_child_session(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_kind,
):
    from types import SimpleNamespace
    from app.services.runtime_result_sources import _published_owned_delegation_input

    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, "load_memory")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["child_id"])
        bad_task = SimpleNamespace(id=task.id, tenant_id=task.tenant_id, child_agent_id=task.child_agent_id,
            child_session_id="malformed-session-id")
        source = seed["memory_source"]
        if source_kind == "session_tool_invocation":
            invocation = (await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == task.id))).one()
            source = {"source_kind": source_kind, "source_id": str(invocation.id)}
        assert await _published_owned_delegation_input(db, source, bad_task) is False


@pytest.mark.parametrize("missing", ["child_session_id", "source_id"])
async def test_published_memory_input_refuses_missing_identity(missing):
    from types import SimpleNamespace
    from app.services.runtime_result_sources import _published_owned_delegation_input

    task = SimpleNamespace(child_session_id=None if missing == "child_session_id" else str(uuid4()))
    source = {"source_kind": "explicit_memory", "source_id": None if missing == "source_id" else str(uuid4())}
    assert await _published_owned_delegation_input(None, source, task) is False


class TeamMemoryDelegationRefused(AssertionError):
    pass


@pytest.mark.xfail(strict=True, raises=TeamMemoryDelegationRefused,
    reason="Agent-team memory still checks its child-owned source session with the parent's runtime principal.")
async def test_parent_reads_colleague_team_memory_search_hit(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.runtime_result_sources import validate_runtime_result_sources

    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        "search_memory", team=True)
    principal = AgentRuntimePrincipal(agent_id=seed["agent_id"], requester_user_id=seed["user_id"],
        session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]), purpose="interactive_session")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        await _validate_input_dependencies(db, seed["tenant_id"], [seed["team_source"]], user=user)
        result = await db.get(RuntimeResultObject, seed["result_id"])
        refusal = None
        try:
            allowed = await validate_runtime_result_sources(db, principal, result)
        except ContentInputReceiptError as exc:
            refusal = exc
            frames, tb = [], exc.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename.endswith("team_memory_authority.py"):
                    frames.append(tb.tb_lineno)
                tb = tb.tb_next
            print("R28A_TEAM_MEMORY_REFUSAL", json.dumps({"code": exc.code, "lines": frames}))
            assert exc.code == "content_source_access_denied"
            assert 74 in frames
            allowed = False
        else:
            assert allowed is True
    calls = [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r28a-team-result")]
    calls.extend(_call(reader, _reader_arguments(reader, seed["child_id"]), f"r28a-team-{reader}") for reader in READERS)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": calls, "finish_reason": "tool_calls"}, {"content": "The team source is unavailable."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    assert len(results) == 4
    if not allowed:
        assert refusal is not None
        assert ANSWER not in json.dumps(client.requests)
        async with owner_sessionmaker() as db:
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
            assert len(models) == 2
            assert all(ANSWER not in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models)
        raise TeamMemoryDelegationRefused("The member can read the team hit; the parent's bound result read is refused.")
    assert ANSWER in json.dumps(client.requests[1])
