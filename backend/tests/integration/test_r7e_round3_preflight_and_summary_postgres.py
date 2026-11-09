"""Request preflight preserves kept bindings and semantic compaction records its trace."""

import json
import uuid
from copy import copy

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _compress_messages_with_lifecycle_hooks, _dicts_to_llm_messages
from app.models.invocation_span import InvocationSpan
from app.runtime.ccplus_contracts import ContextPolicyV1
from app.runtime.compaction_trace import CompactionTraceContext
from app.runtime.context_candidates import ContextMessageOrigin
from app.runtime.session_context_controller import prepare_session_context_for_request
from app.services.llm_client import LLMMessage
from tests.integration.test_m1g30_model_dependencies_postgres import _snapshot
from tests.integration.test_r7e_workspace_kernel_inputs_postgres import (
    _create, _tool_round, native_workspace, read_policy,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _trace(n, factory, facts):
    from app.services.invocation_trace import append_invocation_span, monotonic_ms, persist_invocation_span

    async def record(fact):
        append_invocation_span(agent_id=n.agent, span_type="compaction", name=fact["fact_type"],
            started_at_ms=monotonic_ms(), invocation_id=str(n.run), metadata=fact)
        async with factory() as db:
            await pin_rls_tenant_context(db, n.tenant)
            await persist_invocation_span(db=db, tenant_id=n.tenant, trace_id=str(n.run),
                span_id=fact.get("span_id", f"r7e-round3-{uuid.uuid4()}"),
                parent_span_id=None, parent_trace_id=None, span_type="compaction",
                name=fact["fact_type"], status="ok", duration_ms=0,
                agent_id=n.agent, user_id=n.user, runtime_task_id=n.run, session_id=str(n.session),
                request_id=None, metadata=fact)
            await db.commit()
        facts.append(fact)

    return CompactionTraceContext.enabled(thread_id=str(n.session), turn_id=str(n.run),
        model="synthetic-no-provider", provider_name="openai", fact_recorder=record)


async def _snapshot_messages(n, factory, messages, round_index, *, commit=True):
    async with factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        _, snapshot = await _snapshot(db, n.tenant, n.agent, n.session, n.run,
            [copy(message) for message in messages],
            round_index=round_index)
        if commit:
            await db.commit()
        else:
            await db.rollback()
    return snapshot


async def _preflight(n, factory, monkeypatch, messages, keep_recent, facts):
    from app.services import memory_service

    async def model(*_args, **_kwargs):
        return {"provider": "openai", "model": "synthetic-no-provider"}
    monkeypatch.setattr(memory_service, "_get_summary_model_config", model)
    trace = _trace(n, factory, facts)

    async def compress(rows, **kwargs):
        return await _compress_messages_with_lifecycle_hooks(memory_service.maybe_compress_messages,
            rows, trace_context=trace, agent_id=n.agent, session_id=str(n.session),
            trigger="request_preflight", instructions="request_preflight_context_compaction", **kwargs)

    return await prepare_session_context_for_request(messages=messages,
        policy=ContextPolicyV1(model_window=40_000),
        estimate_tokens=lambda rows: sum(len(row.content or "") for row in rows) // 4,
        compress_messages=compress, session_id=str(n.session), turn_id=str(n.run),
        runtime_task_id=str(n.run), compress_kwargs={"model_provider": "openai",
            "model_name": "synthetic-no-provider", "max_input_tokens_override": 40_000,
            "tenant_id": n.tenant, "keep_recent": keep_recent})


def _old_message():
    content = "old business task bytes " * 12_000
    return LLMMessage(role="user", content=content, source_ref="session-kernel-context:old-input",
        context_origin=ContextMessageOrigin.capture(kind="business_task", role="user",
            content=content, sources=(), unbound=()))


@pytest.mark.parametrize("checked", ["marker", "kept_tool"])
async def test_before_request_degraded_compaction_keeps_binding_status(
    native_workspace, app_user_sessionmaker, monkeypatch, checked,
):
    from app.services import memory_service
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import conversation_from_history_messages

    n = native_workspace
    await _create(n, "workspace/report.md", "KEPT_R7E_TOOL_RESULT")
    invocation, output = await _tool_round(n, app_user_sessionmaker, "grep_search",
        {"pattern": "KEPT_R7E_TOOL_RESULT", "root": "workspace"})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session)
    recent = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
    # Observe the real prepared binding without leaving an undispatched response round.
    before = await _snapshot_messages(n, app_user_sessionmaker, recent, 2, commit=False)
    context = LLMMessage(role="system", content="Retained compiler context")
    context.context_origin = ContextMessageOrigin.capture(kind="kernel_context", role="system",
        content=context.content, sources=(), unbound=())
    recent.append(context)
    ref = f"session-tool-result:{invocation.id}"
    before_entry = next(entry for entry in before["input_bindings"]["messages"]
                        if entry.get("source_ref") == ref)
    assert before_entry["status"] == "bound" and before_entry["dependency_status"] == "resolved", before_entry
    monkeypatch.setattr(memory_service, "_summary_breaker_is_open", lambda _tenant: True)
    facts = []
    old = _old_message()
    prepared = await _preflight(n, app_user_sessionmaker, monkeypatch, [old, *recent], len(recent), facts)
    assert prepared.compressed and prepared.changed
    assert len(prepared.messages) == len(recent) + 1
    snapshot = await _snapshot_messages(n, app_user_sessionmaker, prepared.messages, 3)
    if checked == "marker":
        marker = prepared.messages[0]
        assert marker.content == "[Older messages omitted: LLM semantic compaction summary unavailable]"
        entry = snapshot["input_bindings"]["messages"][0]
        assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
        assert marker.context_origin.kind == "compaction_status"
        assert marker.context_origin is not old.context_origin
        assert marker.source_ref != old.source_ref
        assert {"role": "system", "content": marker.content} in snapshot["wire_request"]["messages"]
    else:
        index = next(i for i, message in enumerate(prepared.messages)
                     if message.tool_call_id == invocation.provider_tool_use_id)
        kept = prepared.messages[index]
        assert kept.content == output
        entry = snapshot["input_bindings"]["messages"][index]
        assert entry["status"] == before_entry["status"], entry
        assert entry["dependency_status"] == before_entry["dependency_status"], entry
        assert entry["dependencies"] == before_entry["dependencies"]
        assert kept.source_ref == ref
        assert prepared.messages[-1].context_origin is context.context_origin
        assert prepared.messages[-1].source_ref == context.source_ref
        assert "KEPT_R7E_TOOL_RESULT" in json.dumps(snapshot["wire_request"])
        assert snapshot["input_bindings"]["messages"][-1]["dependency_status"] == "resolved"


async def test_semantic_summary_with_origin_records_real_trace_without_rebinding_summary(
    native_workspace, app_user_sessionmaker, monkeypatch,
):
    from app.services import conversation_summarizer, memory_service

    n = native_workspace
    old = _old_message()
    recent = {"role": "user", "content": "Keep this request"}
    async def model(*_args, **_kwargs):
        return {"provider": "openai", "model": "synthetic-no-provider"}
    monkeypatch.setattr(memory_service, "_get_summary_model_config", model)
    monkeypatch.setattr(memory_service, "_summary_breaker_is_open", lambda _tenant: False)
    summarized = []

    async def summarize(rows, *_args, **_kwargs):
        summarized.extend(rows)
        return "Stubbed semantic summary"
    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summarize)
    facts = []
    compressed = await _compress_messages_with_lifecycle_hooks(memory_service.maybe_compress_messages,
        [{"role": old.role, "content": old.content, "source_ref": old.source_ref,
          "context_origin": old.context_origin}, recent],
        trace_context=_trace(n, app_user_sessionmaker, facts), agent_id=n.agent,
        session_id=str(n.session), trigger="request_preflight", model_provider="openai",
        model_name="synthetic-no-provider", max_input_tokens_override=40_000,
        tenant_id=n.tenant, keep_recent=1)
    assert summarized[0]["context_origin"] is old.context_origin
    assert summarized[0]["source_ref"] == old.source_ref
    summary = _dicts_to_llm_messages(compressed)[0]
    assert "Stubbed semantic summary" in summary.content
    assert summary.context_origin is None
    assert summary.source_ref.startswith("session-compaction:") and summary.source_ref != old.source_ref
    assert compressed[-1] is recent
    assert old.context_origin is summarized[0]["context_origin"]
    assert [fact["fact_type"] for fact in facts] == ["compaction_attempt_started",
        "compaction_attempt_completed", "compaction_checkpoint_installed"]
    source = facts[-1]["compaction_source"]
    assert source["inputs"] == [{key: value for key, value in summarized[0].items() if key != "context_origin"}]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        spans = (await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == n.run,
            InvocationSpan.span_type == "compaction"))).all()
        assert len(spans) == 3
        checkpoint = next(span for span in spans if span.name == "compaction_checkpoint_installed")
        stored = checkpoint.metadata_json["compaction_source"]
        assert {key: stored[key] for key in source} == source
        assert stored["input_bindings"]["dependency_coverage"]["complete"] is False
