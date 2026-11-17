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
        except ContentInputReceiptError:
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
    for reader in READERS:
        checked = _task_body(reader, json.loads(results[f"r28a-workspace-{reader}"]["content"]), seed["child_id"])
        assert checked["result"] == FILE_RESULT and "result_publication" not in checked
    assert FILE_RESULT in json.dumps(client.requests[1])


HIT_MEMORY = "QUASARFACTTAG: clarify the goal; check the evidence; report the result."
UNRETURNED_MEMORY = "ZIRCONFACTTAG: prepare a separate payroll checklist."


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


async def _memory_hit_result(owner, app_factory, monkeypatch, tmp_path, tool, *, group=False, extra=False):
    from types import SimpleNamespace
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.organization_identity import ContentInputReceipt
    from app.services import agent_tools, session_model_round
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
    if extra:
        saved_extra, extra_call = await memory_call(n, app_factory, "save_memory",
            {"content": UNRETURNED_MEMORY, "category": "project"}, command="Remember a separate payroll fact")
        assert UNRETURNED_MEMORY in saved_extra, saved_extra
        async with owner() as db:
            extra_receipt = await db.get(ContentInputReceipt, uuid5(extra_call.id, "explicit-memory-source"))
            extra_id = extra_receipt.authority_json["entry_id"]
        retired_extra, _retirement = await memory_call(n, app_factory, "retire_memory",
            {"memory_id": extra_id, "reason": "obsolete"}, command="Retire the separate payroll fact from future recall")
        assert json.loads(retired_extra)["status"] == "completed", retired_extra
        from app.services.explicit_memory_authority import latest_memory_receipt

        async with owner() as db:
            seed["extra_memory_source"] = source_dependency(await latest_memory_receipt(db, n.tenant, n.agent, extra_id))

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
    if extra:
        from app.kernel.engine import _context_message

        prepare = session_model_round.prepare_model_request

        async def add_separate_memory(db, **kwargs):
            if kwargs["run_id"] == seed["child_id"] and kwargs["round_index"] == 1:
                kwargs["messages"].append(_context_message(role="system", kind="dynamic_prompt",
                    content=UNRETURNED_MEMORY, sources=[seed["extra_memory_source"]], unbound=()))
                kwargs["wire_request"]["messages"].append({"role": "system", "content": UNRETURNED_MEMORY})
            return await prepare(db, **kwargs)

        monkeypatch.setattr(session_model_round, "prepare_model_request", add_separate_memory)
    arguments = ({"query": "QUASARFACTTAG", "scope": "facts"} if tool == "search_memory"
        else {"ids": [seed["memory_entry_id"]]})
    child_client = ScriptedLLMClient([
        {"tool_calls": [_call(tool, arguments, "r28a-hit-tool")], "finish_reason": "tool_calls"},
        {"content": ANSWER},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    token = set_runtime_task_fence(task_id=seed["child_id"], worker_id="r28a-hit-worker", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(seed["child_id"].hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
    calls = await _tool_results(owner, seed["child_id"])
    assert HIT_MEMORY in calls["r28a-hit-tool"]["content"]
    assert UNRETURNED_MEMORY not in calls["r28a-hit-tool"]["content"]
    assert len(child_client.requests) == 2 and HIT_MEMORY in json.dumps(child_client.requests[1])
    async with owner() as db:
        task = await db.get(RuntimeTask, seed["child_id"])
        session = await db.get(ChatSession, seed["child_session_id"])
        assert task.status == "completed" and task.result_summary == ANSWER
        assert session.session_kind == "delegation_run" and session.user_id == seed["user_id"]
        invocation = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == task.id))).one()
        assert invocation.tool_name == tool and invocation.effect_state == "effect_committed"
        proof = await db.get(InvocationSpan, uuid5(invocation.id, "explicit-memory-tool-result"))
        assert proof.metadata_json["memory_result"]["sources"] == [seed["memory_source"]]
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        dependencies = model.model_request_snapshot_json["input_bindings"]["dependencies"]
        assert seed["memory_source"] in dependencies
        if extra:
            assert seed["extra_memory_source"] in dependencies
            first = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id)
                .order_by(SessionModelResult.round_id))).first()
            assert UNRETURNED_MEMORY in json.dumps(child_client.requests[0])
            assert UNRETURNED_MEMORY in json.dumps(first.model_request_snapshot_json["wire_request"])

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
    import app.runtime.invoker as invoker

    seed = await _memory_hit_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool)
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
    checked = _task_body(reader, json.loads(results["r28a-hit-task"]["content"]), seed["child_id"])
    assert direct["status"] == "ok" and direct["complete"] is True, direct
    assert ANSWER in direct["content"] and checked["result"] == ANSWER
    assert checked["status"] == "completed" and "result_publication" not in checked
    assert len(client.requests) == 3 and ANSWER not in json.dumps(client.requests[0])
    assert all(ANSWER in json.dumps(request) for request in client.requests[1:])
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)
            .order_by(SessionModelResult.round_id)))
        assert len(models) == 3
        assert ANSWER not in json.dumps(models[0].model_request_snapshot_json["wire_request"])
        assert all(ANSWER in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models[1:])


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
@pytest.mark.parametrize("change", ["private_owner", "group_membership", "revoked", "receipt_hash",
    "unreturned", "other_agent"])
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
        group=change == "group_membership", extra=change == "unreturned")
    source = seed.get("extra_memory_source", seed["memory_source"])
    if change == "group_membership":
        seed["memory_group"].members.clear()
    elif change == "revoked":
        retired, _call_row = await memory_call(seed["memory_context"], app_user_sessionmaker, "retire_memory",
            {"memory_id": seed["memory_entry_id"], "reason": "privacy"}, command="Withdraw this memory for privacy")
        assert json.loads(retired)["status"] == "completed", retired
    else:
        async with owner_sessionmaker() as db:
            receipt = await db.get(ContentInputReceipt, UUID(source["source_id"]))
            if change == "private_owner":
                receipt.authority_json = {**receipt.authority_json, "owner_user_id": str(seed["other_user_id"])}
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
        if change in {"unreturned", "other_agent"}:
            await validate_browser_memory_dependency(db, seed["tenant_id"], source, user=user)
            assert await _published_owned_delegation_input(db, source, task) is False
        else:
            with pytest.raises(ContentInputReceiptError) as refused:
                await validate_browser_memory_dependency(db, seed["tenant_id"], source, user=user)
            assert refused.value.status_code == 403
        result = await db.get(RuntimeResultObject, seed["result_id"])
        try:
            allowed = await validate_runtime_result_sources(db, principal, result)
        except ContentInputReceiptError:
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
