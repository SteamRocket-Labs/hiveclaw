"""Instruction-only parent readership and computed trigger permissions."""
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services import content_input_receipts as receipts


@pytest.mark.parametrize("lane", ["trigger", "native_trigger", "web"])
def test_computed_trigger_permissions_use_the_real_session_source(lane):
    from app.kernel.contracts import InvocationRequest, ExecutionIdentityRef
    from app.kernel.engine import _build_permissions_context
    from app.runtime.context import SessionContext, ensure_runtime_assembly_state

    agent = uuid.uuid4()
    session = SessionContext(session_id=str(uuid.uuid4()), source="web_chat" if lane == "native_trigger" else lane,
        metadata={"allowed_tools": ["search_memory"]})
    request = InvocationRequest(model=None, messages=[], agent_name="Reader", role_description="Reader",
        agent_id=agent, session_context=session, allowed_tool_names=["search_memory"],
        execution_identity=ExecutionIdentityRef(identity_type="agent_bot", identity_id=agent, label="Scheduled")
        if lane == "native_trigger" else None)
    prompt = _build_permissions_context(request, None)
    assert "search_memory" in prompt
    unbound = ensure_runtime_assembly_state(session).context_unbound_for(agent_id=agent, content=prompt)
    assert ("permission_configuration_sources_unbound" in unbound) is (lane == "web")


async def test_unproved_catalog_data_withholds_only_data_and_keeps_platform_guidance(monkeypatch):
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix
    from app.runtime.prompt_sections.skills_catalog import build_skills_catalog_section, SKILL_CATALOG_GUIDANCE
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.llm_client import LLMMessage
    from tests.integration.test_a4c_one_item_withheld import _bindings

    ledger = []
    suffix = build_dynamic_prompt_suffix(skill_catalog=build_skills_catalog_section("CUSTOM_CATALOG_SECRET"),
        permissions_context="computed trigger permissions", permissions_platform_constant=True,
        source="trigger", context_section_ledger=ledger)
    messages, unbound, cursor = [], [], 0
    for section in ledger:
        if not section["selected"]:
            continue
        text = suffix[cursor:cursor + section["rendered_chars"]]
        cursor += section["rendered_chars"] + 2
        messages.append(LLMMessage(role="system", content=text, context_origin=ContextMessageOrigin.capture(
            kind="dynamic_prompt", role="system", content=text, sections=[{"name": section["name"]}])))
        if section["name"] == "skill_catalog":
            unbound.append(len(messages) - 1)
        elif section["name"] in {"skill_guidance", "permissions_context"}:
            assert section["platform_constant"] is True
    assert unbound
    bindings = _bindings(len(messages), {})
    for index in unbound:
        bindings["messages"][index]["dependency_status"] = "incomplete"
    db = NS(get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.shared_group_authority.validate_shared_group_model_inputs", AsyncMock())
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", AsyncMock())
    wire = {"messages": [{"role": message.role, "content": message.content} for message in messages]}
    await _validate_or_withhold_model_sources(db, tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(), run_id=uuid.uuid4(), input_bindings=bindings, messages=messages, wire_request=wire)
    assert "CUSTOM_CATALOG_SECRET" not in str(wire)
    assert "技能目录内容暂时无法核实" in str(wire)
    assert any(SKILL_CATALOG_GUIDANCE in message["content"] for message in wire["messages"])
    assert any("computed trigger permissions" in message["content"] for message in wire["messages"])
    assert all(bindings["messages"][index]["status"] == "withheld" for index in unbound)


@pytest.mark.parametrize("case", ["same_member", "different_member", "changed_instruction", "child_read",
    "invalid_child_session", "invalid_source_ref", "missing_receipt", "missing_sections"])
async def test_parent_memory_is_attested_only_for_exact_same_member_instruction(monkeypatch, case):
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.services import delegation_input_origin as delegation
    from app.services.session_model_inputs import _hash
    from tests.integration.test_a4c_one_item_withheld import _bindings

    tenant, parent_agent, child_agent, user, parent_session, child_session, parent_run, child_run = (
        uuid.uuid4() for _ in range(8))
    source = {"source_kind": "explicit_memory", "source_id": str(uuid.uuid4()), "source_version": "v1"}
    instruction = "INHERITED_MEMORY_MARKER Reply ready."
    call = NS(id=uuid.uuid4())
    model = NS(id=uuid.uuid4(), state="round_committed", model_request_hash="parent-hash", model_request_snapshot_json={
        "input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [source],
            "dependency_coverage": {"complete": True}}})
    model.model_request_hash = _hash(model.model_request_snapshot_json)
    proof = {"schema": "hive.delegation_instruction_origin.v1", "parent_invocation_id": str(call.id),
        "parent_model_result_id": str(model.id), "parent_model_request_hash": model.model_request_hash,
        "recipient_agent_id": str(child_agent), "content": instruction, "requester_user_id": str(user),
        "call_id": "call-1"}
    pointer = {"source_hash": _hash(proof)}
    pointer["source_id"] = str(uuid.uuid5(call.id, f"delegation-instruction:{pointer['source_hash']}"))
    receipt = InvocationSpan(id=uuid.UUID(pointer["source_id"]), tenant_id=tenant, agent_id=parent_agent,
        session_id=str(parent_session), runtime_task_id=parent_run, user_id=user,
        span_type="delegation_input", status="captured", name="model_authored_instruction",
        span_id="delegation-instruction:" + pointer["source_id"], metadata_json={"instruction_origin": proof})
    child = NS(id=child_run, tenant_id=tenant, task_type="delegation", parent_agent_id=parent_agent,
        child_agent_id=child_agent, parent_session_id=str(parent_session), child_session_id=child_session.hex,
        root_user_id=uuid.uuid4() if case == "different_member" else user,
        prompt=instruction + (" changed" if case == "changed_instruction" else ""),
        metadata_json={"conversation_messages": [{"a2a_instruction_origin": pointer}]})
    parent = NS(id=parent_run, tenant_id=tenant, root_user_id=user)
    canonical = {"kind": "delegated_instruction", "message": {"role": "user", "content": child.prompt},
        "sections": [{"kind": "parent_tool_call", **pointer}], "dependencies": [source]}
    if case == "invalid_child_session":
        child.child_session_id = "not-a-uuid"
    if case == "missing_sections":
        canonical.pop("sections")
    monkeypatch.setattr("app.services.session_model_inputs.load_context_message_origin", AsyncMock(return_value=canonical))
    monkeypatch.setattr(delegation, "_parent_call", AsyncMock(return_value=(call, model, {"message": instruction})))

    class DB:
        added = []
        receipt_reads = 0

        def get_transaction(self):
            # An idle AsyncSession returns None, so every dependency is revalidated.
            return None

        async def get(self, kind, key, **kwargs):
            if kind is RuntimeTask:
                return child if key == child_run else parent
            if kind is InvocationSpan:
                self.receipt_reads += 1
                if case == "missing_receipt" and self.receipt_reads > 1:
                    return None
                return receipt

        async def scalar(self, query):
            if query.column_descriptions[0].get("entity") is ChatSession:
                return ChatSession(audience_kind="private")

        async def execute(self, query):
            return NS(scalars=lambda: [])

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            pass

    async def memory_check(db, tid, refs, *, runtime_request, **kwargs):
        if runtime_request.agent_id != parent_agent or runtime_request.requester_user_id != user:
            raise receipts.ContentInputReceiptError("explicit_memory_source_unverified", "Private memory", 403)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", memory_check)
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", AsyncMock())
    messages = [{"role": "user", "content": child.prompt}]
    bindings = _bindings(1, {0: [source]})
    bindings["messages"][0]["source_ref"] = "session-kernel-context:" + str(uuid.uuid4())
    if case == "invalid_source_ref":
        bindings["messages"][0]["source_ref"] = "session-kernel-context:not-a-uuid"
    if case == "child_read":
        messages.append({"role": "tool", "content": "CHILD_OWN_MEMORY_READ"})
        bindings = _bindings(2, {0: [source], 1: [source]})
        bindings["messages"][0]["source_ref"] = "session-kernel-context:" + str(uuid.uuid4())
    wire = {"messages": [dict(message) for message in messages]}
    from app.services import session_model_round as rounds

    monkeypatch.setattr(rounds, "_bound_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rounds, "append_session_events", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.capture_context_message_origins", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.build_model_input_bindings", AsyncMock(return_value=bindings))
    db = DB()
    await rounds.prepare_model_request(db, tenant_id=tenant, agent_id=child_agent,
        session_id=child_session, run_id=child_run, turn_id="a4c-parent-reader", round_index=0,
        messages=messages, tools=[], provider="local", model="a4c", wire_request=wire)
    wire = db.added[0].model_request_snapshot_json["wire_request"]
    text = str(wire)
    if case in {"same_member", "child_read"}:
        assert instruction in text
        assert source in bindings["dependencies"]
    else:
        assert "INHERITED_MEMORY_MARKER" not in text
        assert bindings["messages"][0]["status"] == "withheld"
    if case == "child_read":
        assert "CHILD_OWN_MEMORY_READ" not in text
        assert bindings["messages"][1]["status"] == "withheld"
