"""Real compaction retains only the inputs actually summarized, without authority claims."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _compress_messages_with_trace, _llm_messages_to_dicts, _mid_run_items_to_user_messages, _dicts_to_llm_messages
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionCommand
from app.runtime.compaction_trace import CompactionTraceContext
from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
from app.services.invocation_trace import persist_invocation_span
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.integration.test_m1g30_model_dependencies_postgres import _snapshot

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("message", [
    {"role": "system", "content": "PREVIOUS_SUMMARY_MUST_SURVIVE"},
    {"role": "assistant", "content": "ASSISTANT_PLAN_MUST_SURVIVE", "tool_calls": [
        {"function": {"name": "read_file", "arguments": "{}"}}]},
])
def test_compaction_does_not_drop_summary_or_text_attached_to_a_tool_call(message):
    from app.services.conversation_summarizer import _serialize_message_for_summary

    rendered = "\n".join(_serialize_message_for_summary(message))
    assert message["content"] in rendered
    if message.get("tool_calls"):
        assert "read_file" in rendered


async def _compact(owner_sessionmaker, app_user_sessionmaker, monkeypatch, *, unknown=False, repeat=False):
    from app.services import memory_service as memory
    from app.services import conversation_summarizer as summarizer

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    messages = _llm_messages_to_dicts(_mid_run_items_to_user_messages(bound), include_source_refs=True)
    if unknown:
        messages[0].pop("source_ref")
    old = dict(messages[0])
    recent = {"role": "user", "content": "UNSELECTED_RECENT_INPUT_WITHOUT_AN_ORIGIN"}
    messages.append(recent)
    async def config(*_args, **_kwargs):
        return {}
    async def model(*_args, **_kwargs):
        return {"provider": "openai", "model": "synthetic-no-provider"}
    actual_inputs = []
    async def summarize(inputs, *_args, **_kwargs):
        actual_inputs.extend(inputs)
        return "Provider-authored compact summary"
    monkeypatch.setattr(memory, "_get_memory_config", config)
    monkeypatch.setattr(memory, "_get_summary_model_config", model)
    monkeypatch.setattr(memory, "estimate_tokens", lambda *_args, **_kwargs: 10000)
    monkeypatch.setattr(summarizer, "_llm_summarize", summarize)
    async with owner_sessionmaker() as db:
        user = (await db.get(ChatSession, session)).user_id
    async def record(fact):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            await persist_invocation_span(db=db, tenant_id=tenant, trace_id=str(run),
                span_id=fact.get("span_id", f"fixture-{uuid.uuid4()}"), parent_span_id=None, parent_trace_id=None,
                span_type="compaction", name=fact["fact_type"], status="ok", duration_ms=0,
                agent_id=agent, user_id=user, runtime_task_id=run, session_id=str(session), request_id=None, metadata=fact)
            await db.commit()
    context = CompactionTraceContext.enabled(thread_id=str(session), turn_id=str(run),
        model="synthetic-no-provider", provider_name="openai", fact_recorder=record)
    compressed = await _compress_messages_with_trace(memory.maybe_compress_messages, messages,
        trace_context=context, model_provider="openai", model_name="synthetic-no-provider",
        max_input_tokens_override=4096, tenant_id=tenant, keep_recent=1, compress_threshold=0.01,
        agent_id=agent, user_id=user)
    assert actual_inputs == [old]
    assert compressed[-1] == recent
    if repeat:
        previous = dict(compressed[0])
        actual_inputs.clear()
        compressed = await _compress_messages_with_trace(memory.maybe_compress_messages, [previous, recent],
            trace_context=context, model_provider="openai", model_name="synthetic-no-provider",
            max_input_tokens_override=4096, tenant_id=tenant, keep_recent=1, compress_threshold=0.01,
            agent_id=agent, user_id=user)
        assert actual_inputs == [previous]
    return SimpleNamespace(tenant=tenant, agent=agent, session=session, run=run, user=user, summary=compressed[0], old=old)


@pytest.mark.parametrize("repeat", [False, True])
async def test_next_actual_model_snapshot_inherits_only_summarized_inputs(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, repeat,
):
    s = await _compact(owner_sessionmaker, app_user_sessionmaker, monkeypatch, repeat=repeat)
    assert s.summary.get("source_ref", "").startswith("session-compaction:"), s.summary
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == s.session))
        _request, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, _dicts_to_llm_messages([s.summary]))
        catalog = snapshot["input_bindings"]
        assert catalog["dependency_coverage"]["complete"] is True, catalog
        assert catalog["source_authority"] == "not_evaluated"
        assert catalog["dependencies"] == [{"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}]
        assert "source_ref" not in str(snapshot["wire_request"])
        assert "UNSELECTED_RECENT_INPUT" not in str(catalog)


@pytest.mark.parametrize("change", ["body", "span", "foreign_session", "unknown_input"])
async def test_unknown_or_changed_summary_origin_cannot_be_rebased_as_verified_input(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, change,
):
    s = await _compact(owner_sessionmaker, app_user_sessionmaker, monkeypatch, unknown=change == "unknown_input")
    assert s.summary.get("source_ref"), s.summary
    if change == "body":
        s.summary["content"] += "UNVERIFIED_SUMMARY_REWRITE"
    elif change == "span":
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(InvocationSpan).where(InvocationSpan.session_id == str(s.session),
                InvocationSpan.name == "compaction_checkpoint_installed"))
            metadata = dict(row.metadata_json)
            metadata["compaction_source"] = {**metadata["compaction_source"], "message": {
                "role": "system", "content": "REWRITTEN_CHECKPOINT"}}
            row.metadata_json = metadata
            await db.commit()
    elif change == "foreign_session":
        _tenant, _agent, session, run, _bound = await _bound_input(owner_sessionmaker)
        s.tenant, s.agent, s.session, s.run = _tenant, _agent, session, run
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        _request, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, _dicts_to_llm_messages([s.summary]))
        assert snapshot["input_bindings"]["dependency_coverage"]["complete"] is False


async def test_summary_dependencies_still_require_current_upstream_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    s = await _compact(owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    assert s.summary.get("source_ref"), s.summary
    request = ToolExecutionRequest(tool_name="read_file", arguments={"path": "workspace/derived.md"},
        context=ToolExecutionContext(agent_id=s.agent, user_id=s.user, tenant_id=str(s.tenant), session_id=str(s.session), workspace=tmp_path))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        _request, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, _dicts_to_llm_messages([s.summary]))
        deps = snapshot["input_bindings"]["dependencies"]
        assert deps
        await validate_runtime_input_dependencies(db, request, deps)
        await db.commit()
        async with owner_sessionmaker() as writer:
            command = await writer.get(SessionCommand, uuid.UUID(deps[0]["source_id"]))
            command.target_json = {"changed_after_compaction": True}
            await writer.commit()
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, request, deps)
        assert error.value.status_code == 403
