from __future__ import annotations

import json

import pytest


def test_activation_query_manifest_is_schema_versioned_and_json_safe() -> None:
    from app.runtime.activation_query import ActivationQuery

    query = ActivationQuery(
        raw_prompt="Please review docs/memory-system-spec.md and fix the runtime debt.",
        session_id="session-1",
        turn_id="turn-1",
        intent_id="intent-1",
        agent_id="agent-1",
        agent_role="runtime engineer",
        owner_context={"owner_id": "owner-1", "company_id": "company-1"},
        task_profile={"primary": "coding", "confidence": 0.87},
        intent="fix_runtime_debt",
        entities=[{"kind": "file", "value": "docs/memory-system-spec.md"}],
        concepts=["memory", "runtime", "technical_debt"],
        temporal_hints=[{"kind": "relative", "value": "now"}],
        referenced_files=["docs/memory-system-spec.md"],
        risk_level="medium",
        memory_need="required",
        knowledge_need="use_if_relevant",
        skill_need="optional",
        tool_need="required",
        candidate_lanes=("memory", "skill", "tool"),
        budget_policy={"max_candidates": 12},
        parse_trace=[{"source": "mechanical", "field": "referenced_files"}],
    )

    manifest = query.to_manifest()

    assert manifest["schema"] == "hive.ccplus.activation_query.v1"
    assert manifest["query_id"].startswith("aq:")
    assert manifest["raw_prompt"] == "Please review docs/memory-system-spec.md and fix the runtime debt."
    assert manifest["candidate_lanes"] == ["memory", "skill", "tool"]
    assert manifest["owner_context"] == {"owner_id": "owner-1", "company_id": "company-1"}
    json.dumps(manifest)


def test_activation_query_ref_is_stable_after_manifest_roundtrip() -> None:
    from app.runtime.activation_query import ActivationQuery, build_activation_query_ref

    query = ActivationQuery(
        raw_prompt="Compare the CC context window with our runtime.",
        session_id="session-2",
        turn_id="turn-2",
        intent_id="intent-2",
        intent="compare_runtime_context",
        concepts=["cc_parity", "context_window"],
        candidate_lanes=("memory", "knowledge"),
    )

    roundtripped = ActivationQuery.from_manifest(query.to_manifest())

    assert roundtripped == query
    assert build_activation_query_ref(roundtripped) == build_activation_query_ref(query)
    assert build_activation_query_ref(query)["schema"] == "hive.ccplus.activation_query_ref.v1"
    assert build_activation_query_ref(query)["kind"] == "activation_query"


def test_invoker_activation_query_records_turn_and_intent_metadata_source() -> None:
    from types import SimpleNamespace

    from app.runtime.invoker import AgentInvocationRequest, _build_activation_query_for_request
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-turn",
        metadata={"turn_id": "turn-existing", "intent_id": "intent-existing"},
    )
    manifest = _build_activation_query_for_request(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "继续这个 memory runtime 任务"}],
            agent_name="Agent",
            role_description="Runtime engineer",
            session_context=session,
        )
    )

    assert manifest["turn_id"] == "turn-existing"
    assert manifest["intent_id"] == "intent-existing"
    trace_fields = {(item["field"], item["source"]) for item in manifest["parse_trace"]}
    assert ("turn_id", "_ensure_turn_metadata") in trace_fields
    assert ("intent_id", "_ensure_turn_metadata") in trace_fields


def test_invoker_activation_query_embeds_task_profile_payload() -> None:
    from types import SimpleNamespace

    from app.runtime.invoker import AgentInvocationRequest, _build_activation_query_for_request
    from app.runtime.session import SessionContext

    manifest = _build_activation_query_for_request(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "请研究最新行业资料，带来源链接，并调用 web 搜索。"}],
            agent_name="Agent",
            role_description="Researcher",
            session_context=SessionContext(session_id="session-task-profile", metadata={"request_id": "task-profile"}),
        )
    )

    assert manifest["task_profile"] == {
        "name": "research",
        "complexity": "medium",
        "execution_shape": "direct",
        "suggested_deferred_tool_group_names": ["web"],
    }
    trace_fields = {(item["field"], item["source"]) for item in manifest["parse_trace"]}
    assert ("task_profile", "infer_task_profile") in trace_fields


def test_activation_query_from_manifest_rejects_wrong_schema() -> None:
    from app.runtime.activation_query import ActivationQuery

    with pytest.raises(ValueError, match="activation query schema"):
        ActivationQuery.from_manifest({"schema": "wrong", "raw_prompt": "hello"})
