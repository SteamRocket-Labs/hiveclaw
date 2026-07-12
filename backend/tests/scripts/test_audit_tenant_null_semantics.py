from __future__ import annotations


def test_authority_discovery_uses_tenant_owned_parents_not_shared_assets() -> None:
    from app.models import import_all_models
    from app.scripts.audit_tenant_null_semantics import authority_sources_for_table

    import_all_models()
    agent_sources = authority_sources_for_table("agents")
    assignment_sources = authority_sources_for_table("agent_tools")

    assert ("users", "owner_user_id", "id") in agent_sources
    assert ("users", "sponsor_user_id", "id") in agent_sources
    assert not any(source[0] in {"agent_templates", "llm_models"} for source in agent_sources)
    assert ("agents", "agent_id", "id") in assignment_sources
    assert not any(source[0] == "tools" for source in assignment_sources)


def test_authority_discovery_adds_non_fk_runtime_and_trace_contracts() -> None:
    from app.models import import_all_models
    from app.scripts.audit_tenant_null_semantics import authority_sources_for_table

    import_all_models()
    assert ("agents", "parent_agent_id", "id") in authority_sources_for_table("runtime_tasks")
    assert ("users", "root_user_id", "id") in authority_sources_for_table("runtime_tasks")
    assert ("decision_traces", "decision_id", "decision_id") in authority_sources_for_table("decision_trace_feedback")
    assert ("agents", "author_id", "id") in authority_sources_for_table("plaza_posts")


def test_scope_report_state_never_calls_legacy_nulls_closed() -> None:
    from app.scripts.audit_tenant_null_semantics import classify_scope_state

    assert classify_scope_state(null_rows=0, quarantined_rows=0) == "strict"
    assert classify_scope_state(null_rows=0, quarantined_rows=2) == "quarantined"
    assert classify_scope_state(null_rows=1, quarantined_rows=0) == "migration_required"
