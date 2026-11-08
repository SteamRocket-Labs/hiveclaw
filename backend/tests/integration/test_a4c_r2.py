"""Accepted A4c review regressions at the producer and shared request boundary."""

import json
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from tests.integration.test_a4c_one_item_withheld import _bindings, _Task


class DB:
    def __init__(self, *rows):
        self.rows = {(type(row), row.id): row for row in rows}
        self.session_reads = 0

    async def get(self, model, key, **kwargs):
        return self.rows.get((model, key))

    def add(self, row):
        self.rows[type(row), row.id] = row

    async def flush(self):
        pass

    async def scalar(self, query):
        self.session_reads += 1
        return self.session


@pytest.mark.parametrize("state", ["command", "missing", "drift", "owner"])
async def test_schedule_producer_distinguishes_no_pointer_command(state):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.trigger import AgentTrigger
    from app.services.session_model_inputs import _hash
    from app.services.trigger_context_sources import _facts
    from app.services.trigger_daemon import _trigger_child_run_id
    from app.services.trigger_instruction_sources import record_native_trigger_input, native_trigger_input_sources

    tenant, user, aid, sid, wid, cid, tid, bid = (uuid.uuid4() for _ in range(8))
    iid = _trigger_child_run_id(wid, effect="same_session", discriminator=str(sid))
    agent = Agent(id=aid, tenant_id=tenant, agent_type="native")
    session = ChatSession(id=sid, tenant_id=tenant, agent_id=aid, user_id=user)
    wrapper = RuntimeTask(id=wid, tenant_id=tenant, parent_agent_id=aid, task_type="trigger",
        status="completed", root_user_id=user, budget_run_id=bid, metadata_json={
            "delivery": "same_session", "source_session_id": str(sid), "trigger_ids": [str(tid)],
            "trigger_settlement_overrides": {str(tid): "success"}})
    parts = [{"type": "text", "text": "member wakeup"}]
    row = SessionTurnInput(id=iid, tenant_id=tenant, session_id=sid, command_id=cid,
        intent="queue_next_turn", revision=1, content_parts_json=parts)
    metadata = {"source": "loop_same_session", "trigger_runtime_task_id": str(wid), "trigger_ids": [str(tid)]}
    command = SessionCommand(id=cid, tenant_id=tenant, session_id=sid, principal_id=user,
        principal_type="user", namespace="human_input", command_kind="queue_next_turn",
        request_json={"content_parts": parts}, target_json={"runtime_metadata": metadata})
    command.request_hash = _hash({"command_kind": command.command_kind, "request": command.request_json})
    command.target_hash = _hash(command.target_json)
    trigger = AgentTrigger(id=tid, tenant_id=tenant, agent_id=aid, name="Member loop", type="interval",
        reason="member instruction", config={"command": "loop", "created_by": str(user),
            "minutes": 5, "source_session_id": str(sid)})
    if state == "missing":
        trigger.config = {"created_by": str(user), "minutes": 5}
    budget = RuntimeBudgetRun(id=bid, tenant_id=tenant, root_runtime_task_id=wid,
        root_agent_id=aid, root_user_id=user)
    db = DB(agent, session, wrapper, row, command, budget, trigger)
    if state in {"drift", "owner"}:
        proof = {"fields": _facts(trigger), "owner_user_id": str(user)}
        if state == "drift":
            proof["fields"]["reason"] = "old reason"
        else:
            proof["owner_user_id"] = str(uuid.uuid4())
        db.add(InvocationSpan(id=uuid.uuid5(tid, "trigger-configuration-pointer"),
            metadata_json={"configuration": proof}))
    await record_native_trigger_input(db, agent=agent, session=session, wrapper_id=wid,
        input_id=iid, triggers=[trigger])
    refs, complete = await native_trigger_input_sources(db, tenant_id=tenant, agent_id=aid,
        row=row, command=command)
    assert refs
    assert complete is (state == "command")


def patch_validators(monkeypatch, bad=()):
    from app.services.content_input_receipts import ContentInputReceiptError

    async def check(db, *, dependencies, **kwargs):
        if any(ref in bad for ref in dependencies):
            raise ContentInputReceiptError("content_source_changed", "Unavailable")

    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.explicit_memory_authority.validate_memory_model_sources", check)


async def validate(db, bindings, messages):
    from app.services.session_model_round import _validate_or_withhold_model_sources

    wire = {"messages": [dict(message) for message in messages]}
    await _validate_or_withhold_model_sources(db, tenant_id=_Task.tenant_id, agent_id=_Task.parent_agent_id,
        session_id=uuid.UUID(_Task.parent_session_id), run_id=_Task.id, input_bindings=bindings,
        messages=messages, wire_request=wire)
    return wire


@pytest.mark.parametrize("group", [False, True])
async def test_unbound_sibling_never_reaches_wire_or_derived_content(monkeypatch, group):
    db = DB()
    db.session = NS(audience_kind="group" if group else "private")
    # Keep the real group frame check, including actor recovery.
    from app.models.runtime_task import RuntimeTask
    db.rows[RuntimeTask, _Task.id] = _Task()
    monkeypatch.setattr("app.services.shared_group_authority.load_shared_group_runtime_actor",
        AsyncMock(return_value=NS(id=_Task.root_user_id)))
    patch_validators(monkeypatch)
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    good = {"source_kind": "session_input", "source_id": "own", "source_version": "v1"}
    messages = [{"role": "user", "content": "Remember: weekly report on Friday."},
        {"role": "user", "content": "PRIVATE_GROUP_NOTE_CANARY UNBOUND_SECRET"}]
    ambient = {"source_kind": "agent_resident_memory", "source_id": "unbound-ambient", "source_version": "v1"}
    bindings = _bindings(2, {0: [good], 1: [ambient]})
    bindings["messages"][1].update(status="unbound", dependency_status="incomplete", code="source_origin_missing")
    bindings["status"] = "incomplete"
    bindings["dependency_coverage"]["complete"] = False
    wire = await validate(db, bindings, messages)
    assert "UNBOUND_SECRET" not in json.dumps(wire)
    assert "PRIVATE_GROUP_NOTE_CANARY" not in json.dumps(wire)
    assert messages[0]["content"].startswith("Remember:")
    assert bindings["messages"][1]["status"] == "withheld"
    assert bindings["dependency_coverage"]["complete"] is True
    assert bindings["dependencies"] == [good]
    # A copying consumer can only copy the sanitized model input.
    for tool in ("save_memory", "delegate_to_agent"):
        instruction = json.dumps({"tool": tool, "content": wire["messages"]})
        assert "UNBOUND_SECRET" not in instruction


@pytest.mark.parametrize("missing_id", [False, True])
async def test_withheld_tool_result_removes_owning_call_and_reasoning(monkeypatch, missing_id):
    db = DB()
    db.session = NS(audience_kind="private")
    bad = {"source_kind": "explicit_memory", "source_id": "revoked", "source_version": "v1"}
    call = {"type": "function", "function": {"name": "save_memory", "arguments": "TOOL_SECRET"}}
    if not missing_id:
        call["id"] = "call-1"
    messages = [{"role": "assistant", "content": "", "reasoning_content": "TOOL_SECRET", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call-1", "content": "TOOL_SECRET"}]
    patch_validators(monkeypatch, [bad])
    bindings = _bindings(2, {0 if missing_id else 1: [bad]})
    wire = await validate(db, bindings, messages)
    assert "TOOL_SECRET" not in json.dumps(wire)
    assert all(message["role"] == "system" for message in wire["messages"])


async def test_private_session_frame_is_checked_once_for_many_dependencies(monkeypatch):
    db = DB()
    db.session = NS(audience_kind="private")
    patch_validators(monkeypatch)
    refs = [{"source_kind": "session_input", "source_id": str(i), "source_version": "v1"} for i in range(100)]
    await validate(db, _bindings(1, {0: refs}), [{"role": "user", "content": "member"}])
    assert db.session_reads == 1


async def test_one_bad_tool_result_preserves_valid_sibling_call_and_result(monkeypatch):
    db = DB()
    db.session = NS(audience_kind="private")
    bad = {"source_kind": "explicit_memory", "source_id": "revoked", "source_version": "v1"}
    good = {"source_kind": "session_input", "source_id": "own", "source_version": "v1"}
    calls = [{"id": name, "type": "function", "function": {"name": "read_file", "arguments": content}}
        for name, content in (("bad", "REVOKED_ARGUMENT"), ("good", "VALID_ARGUMENT"))]
    messages = [{"role": "assistant", "content": "REVOKED_REASONING", "reasoning_content": "REVOKED_REASONING", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "bad", "content": "REVOKED_RESULT"},
        {"role": "tool", "tool_call_id": "good", "content": "VALID_RESULT"}]
    bindings = _bindings(3, {0: [good], 1: [bad], 2: [good]})
    patch_validators(monkeypatch, [bad])
    wire = await validate(db, bindings, messages)
    assert "REVOKED_" not in json.dumps(wire)
    assert "VALID_RESULT" in json.dumps(wire) and "VALID_ARGUMENT" in json.dumps(wire)
    assert wire["messages"][0]["tool_calls"] == [calls[1]]
    assert wire["messages"][2]["role"] == "tool"
    assert bindings["dependencies"] == [good]


@pytest.mark.parametrize("unbound_sibling", [False, True])
async def test_unattributable_group_dependency_still_refuses_the_frame(monkeypatch, unbound_sibling):
    from app.models.runtime_task import RuntimeTask
    from app.services.content_input_receipts import ContentInputReceiptError

    db = DB()
    db.session = NS(audience_kind="group")
    db.rows[RuntimeTask, _Task.id] = _Task()
    patch_validators(monkeypatch)
    monkeypatch.setattr("app.services.shared_group_authority.load_shared_group_runtime_actor",
        AsyncMock(return_value=NS(id=_Task.root_user_id)))
    bad = {"source_kind": "session_command", "source_id": "private-unattributed", "source_version": "v1"}
    async def check(db, tenant_id, dependencies, **kwargs):
        if bad in dependencies:
            raise ContentInputReceiptError("content_source_access_denied", "Private source", 403)
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", check)
    bindings = _bindings(1, {})
    bindings["dependencies"].append(bad)
    messages = [{"role": "user", "content": "Member command"}]
    if unbound_sibling:
        messages.append({"role": "user", "content": "Unbound sibling"})
        bindings["messages"].append({"message_index": 1, "dependency_status": "incomplete", "dependencies": []})
        bindings["dependency_coverage"]["complete"] = False
    with pytest.raises(ContentInputReceiptError) as denied:
        await validate(db, bindings, messages)
    assert denied.value.code == "group_model_sources_unavailable"


async def test_recovered_attachment_is_removed_from_rejections(monkeypatch):
    from app.services.session_terminal_outcome import _recover_read_artifact_selection

    part = {"path": "workspace/report.md", "artifact_id": str(uuid.uuid4())}
    monkeypatch.setattr("app.services.chat_artifact_delivery.materialize_declared_read_artifacts",
        AsyncMock(return_value=[part]))
    session_id = uuid.uuid4()
    task = NS(id=uuid.uuid4(), tenant_id=uuid.uuid4(), parent_agent_id=uuid.uuid4(),
        root_session_id=str(session_id), root_user_id=uuid.uuid4(),
        metadata_json={"rejected_artifact_paths": [part["path"]]})
    result = NS(id=uuid.uuid4(), session_id=session_id,
        seal_json={"semantic_content": "DELIVERABLE: workspace/report.md"})
    db = DB()
    db.scalars = AsyncMock(return_value=[])
    await _recover_read_artifact_selection(db, task=task, result=result)
    db.scalars.assert_awaited_once()
    assert task.metadata_json["artifacts"] == [part]
    assert task.metadata_json["rejected_artifact_paths"] == []


def test_withheld_note_has_no_internal_code_or_id():
    from app.services.memory_service import _render_withheld_note

    note = _render_withheld_note({"explicit_memory": {"count": 1, "code": "explicit_memory_source_partial"}})
    assert "1" in note
    assert "explicit_memory" not in note
    assert "source_partial" not in note


@pytest.mark.parametrize("resident_index", [False, True])
async def test_revoked_recall_preserves_valid_resident_memory(monkeypatch, tmp_path, resident_index):
    from app.memory.activation import ActivationContext
    from app.memory.types import MemoryItem, MemoryKind
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services import memory_service, session_model_round
    from app.services.llm_client import LLMMessage
    from app.services.principal_context import PrincipalStack

    aid, tenant = uuid.uuid4(), uuid.uuid4()
    directory = tmp_path / str(aid) / "memory/self"
    directory.mkdir(parents=True)
    (directory / "self.md").write_text("VALID_RESIDENT_MEMORY")
    bad = {"source_kind": "explicit_memory", "source_id": "revoked", "source_version": "v1"}
    good = {"source_kind": "explicit_memory", "source_id": "valid", "source_version": "v1"}

    class Retriever:
        async def retrieve(self, *args, **kwargs):
            return [MemoryItem(kind=MemoryKind.SEMANTIC, content=text, score=1, source=ref["source_id"],
                source_refs=(ref,), metadata={"entry_id": ref["source_id"]}) for ref, text in (
                    (bad, "REVOKED_RECALL_SECRET"), (good, "VALID_RECALLED_MEMORY"))]

    monkeypatch.setattr(memory_service, "get_settings", lambda: NS(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **kwargs: Retriever())
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", AsyncMock(return_value=None))
    monkeypatch.setattr(memory_service, "_resolve_activation_context",
        AsyncMock(return_value=ActivationContext(query="q", principal_stack=PrincipalStack())))
    authorized = [(NS(entry_id="revoked", content="REVOKED_RECALL_SECRET", category="project",
        status="active", sensitivity="PL1_public", created_at="2026-11-06"),
        NS(id="revoked", content_sha256="v1"))] if resident_index else []
    monkeypatch.setattr("app.services.explicit_memory_authority.authorized_explicit_entries", AsyncMock(return_value=authorized))
    result = await memory_service.build_memory_context(aid, tenant, query="q", session_id=str(uuid.uuid4()),
        current_user_id=uuid.uuid4(), return_result=True)
    assert "VALID_RESIDENT_MEMORY" in result.content and "REVOKED_RECALL_SECRET" in result.content
    message = LLMMessage(role="system", content=result.content, context_origin=ContextMessageOrigin.capture(
        kind="dynamic_prompt", role="system", content=result.content,
        sources=result.source_refs, unbound=result.source_unbound))
    split = getattr(session_model_round, "split_context_message", lambda message, sections: [message])
    parts = split(message, getattr(result, "sections", ()))
    messages = [{"role": part.role, "content": part.content} for part in parts]
    bindings = _bindings(len(parts), {i: list(part.context_origin.sources) for i, part in enumerate(parts)})
    db = DB()
    db.session = NS(audience_kind="private")
    patch_validators(monkeypatch, [bad])
    wire = await validate(db, bindings, messages)
    assert "REVOKED_RECALL_SECRET" not in json.dumps(wire)
    assert "VALID_RESIDENT_MEMORY" in json.dumps(wire)
    assert "VALID_RECALLED_MEMORY" in json.dumps(wire)
