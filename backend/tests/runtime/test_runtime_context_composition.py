"""P1-W3-5 — RuntimeContext is the convergence point for the four runtime
primitives that used to be spread across context.py / context_engine.py /
context_budget.py / coordinator.py.

These tests pin the new composition contract so future contributors can
discover the canonical wiring and downstream callers stop importing all
four modules to build one consistent context.
"""

from __future__ import annotations

import uuid

from app.runtime.context import RuntimeContext, ensure_runtime_assembly_state
from app.runtime.context_budget import ContextBudget, TaskProfile
from app.runtime.context_engine import DefaultContextEngine
from app.runtime.session import SessionContext


def test_default_construction_keeps_existing_fields_intact() -> None:
    """The legacy four fields (session/identity/tenant/metadata) must
    still work with no arguments — backwards compat for existing callers."""
    ctx = RuntimeContext()
    assert isinstance(ctx.session, SessionContext)
    assert ctx.execution_identity is None
    assert ctx.tenant_id is None
    assert ctx.metadata == {}


def test_default_construction_does_not_eagerly_attach_engine_or_budget() -> None:
    """Plain construction stays lightweight — engine/budget only attach
    via the explicit factory or by direct field assignment."""
    ctx = RuntimeContext()
    assert ctx.engine is None
    assert ctx.budget is None


def test_for_invocation_attaches_default_engine() -> None:
    """The factory is the new ergonomic entry point — by default it
    materializes a DefaultContextEngine so callers don't have to know."""
    ctx = RuntimeContext.for_invocation()
    assert isinstance(ctx.engine, DefaultContextEngine)


def test_for_invocation_preserves_explicit_engine() -> None:
    custom_engine = DefaultContextEngine(artifact_limit=5)
    ctx = RuntimeContext.for_invocation(engine=custom_engine)
    assert ctx.engine is custom_engine


def test_for_invocation_attaches_explicit_budget() -> None:
    profile = TaskProfile(name="general", complexity="low")
    budget = ContextBudget(
        task_profile=profile,
        system_prompt_budget_chars=60000,
        active_tool_groups_budget_chars=2000,
        retrieval_budget_chars=3000,
        knowledge_budget_chars=2000,
        memory_budget_chars=12000,
        skill_catalog_budget_chars=4000,
        soul_budget_chars=16000,
        relationships_budget_chars=2000,
        company_info_budget_chars=5000,
        org_structure_budget_chars=2000,
        focus_budget_chars=3000,
        runtime_triggers_budget_chars=2000,
        restore_budget_chars=12000,
        restore_per_file_cap_chars=2500,
        semantic_limit=12,
        episodic_limit=4,
        external_limit=3,
        rerank_max_select=8,
    )
    ctx = RuntimeContext.for_invocation(budget=budget)
    assert ctx.budget is budget


def test_for_invocation_threads_session_identity_tenant_metadata() -> None:
    session = SessionContext(session_id="s1", source="task")
    tenant = uuid.uuid4()
    metadata = {"a": 1}
    ctx = RuntimeContext.for_invocation(
        session=session, tenant_id=tenant, metadata=metadata
    )
    assert ctx.session is session
    assert ctx.tenant_id == tenant
    assert ctx.metadata is metadata


def test_engine_inject_records_artifact_on_session_metadata() -> None:
    """The engine — once attached — produces fenced context blocks and
    leaves an artifact trail on the session so post-compaction restoration
    can reconstruct what was injected."""
    ctx = RuntimeContext.for_invocation()
    fenced = ctx.engine.inject(
        ctx.session,
        kind="memory",
        source="t3",
        content="hello world",
    )
    assert fenced.startswith('<context_block kind="memory" source="t3">')
    assert "hello world" in fenced
    artifacts = ctx.session.metadata.get("context_artifacts") or []
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "memory"
    assert artifacts[0]["char_count"] == len("hello world")


def test_factory_with_no_args_creates_independent_session_per_call() -> None:
    """Default-factory `SessionContext()` must not be shared between
    invocations — each call gets a fresh mutable state."""
    a = RuntimeContext.for_invocation()
    b = RuntimeContext.for_invocation()
    assert a.session is not b.session
    assert a.metadata is not b.metadata


def test_runtime_context_attaches_runtime_assembly_state() -> None:
    session = SessionContext(session_id="assembly-session")
    ctx = RuntimeContext.for_invocation(session=session)

    ctx.assembly_state.record_deferred_tools(
        [{"name": "web_search", "group": "web", "selector": "select:web_search"}]
    )
    ctx.assembly_state.record_skill_catalog_ranking(
        ranking=[{"skill_name": "research", "score": 0.9}],
        inputs={"scenario_text_present": True},
    )
    ctx.assembly_state.record_prompt_manifest(
        {
            "schema": "hive.ccplus.prompt_assembly_manifest.v1",
            "context_usage_ledger": {"schema": "usage"},
            "dynamic_context_section_ledger": {"schema": "sections", "sections": []},
        }
    )

    state = session.metadata["runtime_assembly_state"]
    assert state["schema"] == "hive.ccplus.runtime_assembly_state.v1"
    assert state["available_deferred_tools"] == ["web_search"]
    assert state["skill_catalog_ranking"] == [{"skill_name": "research", "score": 0.9}]
    assert state["prompt_assembly_manifest"]["schema"] == "hive.ccplus.prompt_assembly_manifest.v1"
    assert session.metadata["prompt_assembly_manifest"] == state["prompt_assembly_manifest"]
    assert session.metadata["dynamic_context_section_ledger"] == {"schema": "sections", "sections": []}


def test_tool_result_ledger_mirrors_into_runtime_assembly_state() -> None:
    from app.runtime.tool_result_ledger import append_tool_result_ledger_entry

    session = SessionContext(session_id="tool-ledger")
    state = ensure_runtime_assembly_state(session)
    entry = {"schema": "hive.tool_result_ledger.v1", "tool_name": "web_search", "status": "ok"}

    append_tool_result_ledger_entry(session, entry)

    assert session.metadata["tool_result_ledger"] == [entry]
    assert state.to_metadata()["tool_result_ledger"] == [entry]
    assert session.metadata["runtime_assembly_state"]["tool_result_ledger"] == [entry]
