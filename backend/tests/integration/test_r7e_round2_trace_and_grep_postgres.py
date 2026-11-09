"""Scoped default grep and degraded compaction survive the actual input boundary."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.invocation_span import InvocationSpan
from tests.integration.test_r7e_workspace_kernel_inputs_postgres import (
    _create, _next_request, _tool_round, native_workspace, read_policy,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("native", [False, True])
async def test_default_grep_reads_only_workspace_despite_other_platform_roots(
    native_workspace, app_user_sessionmaker, monkeypatch, native,
):
    from app.services.agent_tool_domains import workspace

    n = native_workspace
    await _create(n, "workspace/report.md", "OWN_R7E_SEARCH_RESULT")
    hidden_paths = (
        "subagents/helper.md", "enterprise_info/company_profile.md", "plans/plan.md",
        "session_extensions/session/extension.md", ".office_meta/document.json",
        "plugins/plugin/manifest.json", ".hr_template_version", ".exec-home/config",
        ".office-runtime/config", ".office-home/config", "root-note.md",
        "workspace-copy/note.md", "workspace.md",
    )
    for relative in hidden_paths:
        path = n.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("UNRECEIPTED_R7E_PLATFORM_SECRET")
    if not native:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
    else:
        assert workspace.shutil.which("rg")

    invocation, output = await _tool_round(n, app_user_sessionmaker, "grep_search",
        {"pattern": "R7E_SEARCH_RESULT|R7E_PLATFORM_SECRET"})
    snapshot = await _next_request(n, app_user_sessionmaker)
    entry = next(e for e in snapshot["input_bindings"]["messages"]
                 if e.get("source_ref") == f"session-tool-result:{invocation.id}")
    assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
    wire = json.dumps(snapshot["wire_request"])
    assert "OWN_R7E_SEARCH_RESULT" in output and "OWN_R7E_SEARCH_RESULT" in wire
    assert "UNRECEIPTED_R7E_PLATFORM_SECRET" not in output
    assert "UNRECEIPTED_R7E_PLATFORM_SECRET" not in wire
    for relative in hidden_paths:
        assert relative not in output
        assert relative not in wire


@pytest.mark.parametrize("input_origin", [False, True])
async def test_degraded_trim_with_real_trace_retains_the_bound_constant(
    native_workspace, app_user_sessionmaker, monkeypatch, input_origin,
):
    from app.kernel.engine import _compress_messages_with_lifecycle_hooks, _dicts_to_llm_messages
    from app.runtime.compaction_trace import CompactionTraceContext
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services import memory_service
    from app.services.invocation_trace import append_invocation_span, monotonic_ms, persist_invocation_span

    n = native_workspace
    await _create(n, "workspace/report.md", "LOCAL_SEARCH")
    await _tool_round(n, app_user_sessionmaker, "grep_search", {"pattern": "LOCAL_SEARCH", "root": "workspace"})
    # The real breaker suppresses provider work while keeping the production trim path.
    monkeypatch.setattr(memory_service, "_summary_breaker_is_open", lambda _tenant: True)
    async def summary_model(*_args, **_kwargs):
        return {"provider": "openai", "model": "synthetic-no-provider"}
    monkeypatch.setattr(memory_service, "_get_summary_model_config", summary_model)

    recent = {"role": "user", "content": "keep"}
    messages = [{"role": "user", "content": "old bytes " * 400}, recent]
    if input_origin:
        for message in messages:
            message["context_origin"] = ContextMessageOrigin.capture(kind="business_task",
                role=message["role"], content=message["content"], sources=(), unbound=())
    original_messages = [dict(message) for message in messages]
    facts = []

    async def record(fact):
        # Both real sinks must serialize the trace, rather than a permissive test recorder.
        append_invocation_span(agent_id=n.agent, span_type="compaction", name=fact["fact_type"],
            started_at_ms=monotonic_ms(), invocation_id=str(n.run), metadata=fact)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            await persist_invocation_span(db=db, tenant_id=n.tenant, trace_id=str(n.run),
                span_id=f"r7e-round2-{uuid.uuid4()}", parent_span_id=None, parent_trace_id=None,
                span_type="compaction", name=fact["fact_type"], status="ok", duration_ms=0,
                agent_id=n.agent, user_id=n.user, runtime_task_id=n.run, session_id=str(n.session),
                request_id=None, metadata=fact)
            await db.commit()
        facts.append(fact)

    context = CompactionTraceContext.enabled(thread_id=str(n.session), turn_id=str(n.run),
        model="synthetic-no-provider", provider_name="openai", fact_recorder=record)
    compressed = await _compress_messages_with_lifecycle_hooks(memory_service.maybe_compress_messages,
        messages, trace_context=context, agent_id=n.agent, session_id=str(n.session), trigger="initial",
        model_provider="openai", model_name="synthetic-no-provider", max_input_tokens_override=64,
        tenant_id=n.tenant, keep_recent=1)
    assert messages == original_messages
    assert compressed[-1] is recent
    marker = _dicts_to_llm_messages(compressed)[0]
    assert marker.content == "[Older messages omitted: LLM semantic compaction summary unavailable]"
    assert marker.context_origin is compressed[0]["context_origin"]
    assert marker.context_origin.sources == () and marker.context_origin.unbound == ()
    snapshot = await _next_request(n, app_user_sessionmaker, extra=[marker])
    entry = snapshot["input_bindings"]["messages"][0]
    assert entry["status"] == "bound" and entry["dependency_status"] == "resolved", entry
    assert {"role": "system", "content": marker.content} in snapshot["wire_request"]["messages"]

    assert [fact["fact_type"] for fact in facts] == [
        "compaction_attempt_started", "compaction_attempt_completed", "compaction_checkpoint_installed"]
    histories = [facts[0]["request"]["input"], facts[1]["output_items"],
                 facts[2]["checkpoint"]["input_history"], facts[2]["checkpoint"]["replacement_history"]]
    assert all("context_origin" not in message for history in histories for message in history)
    assert facts[1]["output_items"][0] == {"role": "system", "content": marker.content}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        spans = (await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == n.run,
            InvocationSpan.span_type == "compaction"))).all()
        assert len(spans) == 3
        assert sorted(span.name for span in spans) == sorted(fact["fact_type"] for fact in facts)
