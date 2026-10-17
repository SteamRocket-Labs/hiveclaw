"""Canonical input coordinates stay private and bind only the selected bytes."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _clone_api_messages, _dicts_to_llm_messages, _llm_messages_to_dicts, _mid_run_items_to_user_messages
from app.models.session_v2 import SessionModelResult
from app.runtime.hooks import HookResult
from app.services.llm_client import LLMMessage
from app.services.session_model_round import bind_round_inputs, prepare_model_request
from tests.services.test_session_input_control_v2 import _accepted_input, _authority, _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_origin_survives_internal_round_trip_without_changing_provider_wire():
    from datetime import datetime, timezone
    from app.services.session_semantic_history import SessionSemanticMessage
    from app.services.web_chat_runtime import conversation_from_history_messages

    ref = f"session-tool-result:{uuid.uuid4()}"
    message = SessionSemanticMessage(id=ref, role="tool", content="selected result",
        created_at=datetime.now(timezone.utc), sequence_start=1, sequence_end=3,
        group_id="round", source_event_ids=("selected", "unrelated-sibling"), tool_call_id="call")
    history = conversation_from_history_messages([message])
    restored = _dicts_to_llm_messages(history)
    assert restored[0].source_ref == ref
    assert _clone_api_messages(restored)[0].source_ref == ref
    assert "source_ref" not in _llm_messages_to_dicts(restored)[0]
    assert "source_ref" not in restored[0].to_openai_format()
    assert ref not in json.dumps(restored[0].to_anthropic_format())
    kept = _dicts_to_llm_messages(_llm_messages_to_dicts(restored, include_source_refs=True))
    assert kept[0].source_ref == ref
    assert "unrelated-sibling" not in json.dumps(history)

    from app.runtime.invoker import _apply_vision_transform, _apply_cache_hints
    for vision in (False, True):
        user = LLMMessage(role="user", content="image [image_data:data:image/png;base64,YQ==]", source_ref="origin")
        assert _apply_vision_transform([user], vision)[0].source_ref == "origin"
    cached = _apply_cache_hints(_clone_api_messages(restored), "anthropic")
    assert cached[0].source_ref == ref


async def _bound_input(owner_sessionmaker):
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission

    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    async def allow(**_kwargs):
        return HookResult()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user, agent_id=agent, session_id=session)
        receipt = await _accepted_input(db, authority=authority, session_id=session, run_id=run,
            kind="steer_current_turn", content="Exact admitted input")
        await run_user_prompt_admission(db, authority=authority, input_id=receipt.input_id,
            worker_id="input-binding-test", hook_executor=allow)
        await queue_admitted_human_input(db, authority=authority, input_id=receipt.input_id)
        messages = await bind_round_inputs(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run, turn_id=f"turn-{run.hex}", round_index=1)
        await db.commit()
    return tenant, agent, session, run, messages


@pytest.mark.parametrize("alteration", [None, "body", "wire", "foreign-origin", "missing"])
async def test_actual_request_records_verified_origin_and_explicit_coverage_gaps(
    owner_sessionmaker, app_user_sessionmaker, alteration,
):
    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    messages = [LLMMessage(role="system", content="Runtime system context"), *_mid_run_items_to_user_messages(bound)]
    expected_ref = messages[1].source_ref
    if alteration == "body":
        messages[1].content = "Rewritten without a source receipt"
    if alteration == "foreign-origin":
        other = await _bound_input(owner_sessionmaker)
        messages[1].source_ref = f"session-input:{other[-1][0]['session_input_id']}"
    if alteration == "missing":
        messages[1].source_ref = None
    wire = {"messages": _llm_messages_to_dicts(messages), "tools": [], "temperature": 0.3,
        "max_tokens": 8192, "reasoning": {"reasoning_effort": "high"}}
    if alteration == "wire":
        wire["messages"][1]["content"] = "Different provider bytes"
    original_wire = json.loads(json.dumps(wire))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request_id = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=1, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider", wire_request=wire)
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        snapshot = result.model_request_snapshot_json
        assert snapshot["wire_request"] == original_wire
        assert "source_ref" not in json.dumps(snapshot["wire_request"])
        bindings = snapshot["input_bindings"]
        assert bindings["status"] == "incomplete"  # System context has no source authority receipt yet.
        assert bindings["source_authority"] == "not_evaluated"
        assert bindings["messages"][0]["code"] == "source_origin_missing"
        entry = bindings["messages"][1]
        if alteration is None:
            assert entry["status"] == "bound", bindings
            assert entry["source_ref"] == expected_ref
            assert entry["command_ref"].startswith("session-command:")
        else:
            assert entry["status"] == "unresolved", bindings
            assert entry["code"] == {"body": "source_content_mismatch", "wire": "source_wire_mismatch",
                "foreign-origin": "source_origin_unavailable", "missing": "source_origin_missing"}[alteration]
            assert "source_ref" not in entry


async def test_native_provider_alias_resolves_only_the_selected_canonical_tool(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.web_chat_runtime import conversation_from_history_messages
    from tests.services.test_session_semantic_history import _seal_round, _settle_tool_invocation

    tenant, _user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    turn = f"turn-{run.hex}"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request_id = await prepare_model_request(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run, turn_id=turn, round_index=1,
            messages=[LLMMessage(role="user", content="Read both files")], tools=[],
            provider="openai", model="synthetic-no-provider")
        calls = [{"id": name, "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": f"{name}.md"})}} for name in ["selected", "sibling"]]
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=turn, round_index=1, provider_request_id=request_id, content="Read plan", tool_calls=calls)
        invocations = []
        for name in ["selected", "sibling"]:
            invocations.append(await _settle_tool_invocation(db, tenant_id=tenant, agent_id=agent,
                session_id=session, run_id=run, provider_request_id=request_id,
                provider_tool_use_id=name, tool_name="read_file", arguments={"path": f"{name}.md"},
                result_content=f"{name} RAW", model_visible_content=f"{name} projection"))
        await db.flush()
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        assistant = _dicts_to_llm_messages(conversation_from_history_messages(
            [m for m in history.messages if m.role == "assistant"]))
        assistant[0].source_ref = f"session-provider-result:{request_id}"
        assistant_binding = await build_model_input_bindings(db, tenant_id=tenant, agent_id=agent,
            session_id=session, messages=assistant, wire_messages=_llm_messages_to_dicts(assistant))
        assert assistant_binding["status"] == "resolved", assistant_binding
        selected = [m for m in history.messages if m.tool_call_id == "selected"]
        assert len(selected) == 1
        assert len(selected[0].source_event_ids) == 3  # Projection group, including the sibling result.
        messages = _dicts_to_llm_messages(conversation_from_history_messages(selected))
        messages[0].source_ref = f"session-provider-tool-result:{request_id}"
        manifest = await build_model_input_bindings(db, tenant_id=tenant, agent_id=agent,
            session_id=session, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        assert manifest["status"] == "resolved", manifest
        assert manifest["messages"][0]["source_ref"] == f"session-tool-result:{invocations[0].id}"
        assert str(invocations[1].id) not in json.dumps(manifest)
        messages[0].content = "selected RAW"
        rejected = await build_model_input_bindings(db, tenant_id=tenant, agent_id=agent,
            session_id=session, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        assert rejected["messages"][0]["code"] == "source_content_mismatch"
