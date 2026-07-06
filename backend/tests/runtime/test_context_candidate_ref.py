from __future__ import annotations


def test_context_candidate_ref_builds_stable_kind_id_version_hash() -> None:
    from app.runtime.context_candidates import ContextCandidateRef, build_context_candidate_ref

    ref = build_context_candidate_ref(
        kind="Tool Schema",
        item_id="web_search",
        version="schema-v1",
        payload={"name": "web_search", "description": "Search the web"},
    )

    assert isinstance(ref, ContextCandidateRef)
    assert ref.kind == "tool_schema"
    assert ref.item_id == "web_search"
    assert ref.version == "schema-v1"
    assert ref.content_hash
    assert ref.candidate_id.startswith("tool_schema:web_search:schema-v1/")
    assert ref.to_manifest()["candidate_id"] == ref.candidate_id


def test_runtime_prompt_manifest_exposes_unified_candidate_refs() -> None:
    from app.runtime.turn_envelope import build_runtime_prompt_assembly_manifest

    manifest = build_runtime_prompt_assembly_manifest(
        turn_id="turn-ref",
        session_id="session-ref",
        frozen_prefix="## Identity\nAnalyst.",
        dynamic_suffix="## Memory\nmemory text\n\n## Knowledge\nknowledge text",
        provider_system_prompt="## Identity\nAnalyst.",
        provider_dynamic_notice="## Memory\nmemory text",
        context_budget={"memory_budget_chars": 120, "retrieval_budget_chars": 90, "skill_catalog_budget_chars": 80},
        model_window=1000,
        tools_for_llm=[
            {"type": "function", "function": {"name": "read_file", "description": "Read a file"}},
        ],
        memory_snapshot="memory text",
        retrieval_context="knowledge text",
        skill_catalog="## Skills\n- python",
        active_skill_names=["python"],
        available_deferred_tools=[
            {
                "name": "firecrawl_fetch",
                "group": "web",
                "reason": "advanced crawl needed",
                "selector": "select:firecrawl_fetch",
                "schema_token_cost": 42,
                "risk": "network_read",
            }
        ],
    )

    refs = {item["legacy_id"]: item for item in manifest["context_candidate_refs"]}
    candidates = {item["id"]: item for item in manifest["context_candidates"]}

    assert refs["ctx:memory:memory_files"]["candidate_id"].startswith("memory:memory_files:")
    assert refs["ctx:skill:skill_catalog"]["candidate_id"].startswith("skill:skill_catalog:")
    assert candidates["ctx:memory:memory_files"]["candidate_ref"] == refs["ctx:memory:memory_files"]
    assert manifest["skill_candidate_refs"][0]["candidate_id"].startswith("skill:python:")
    assert manifest["tool_candidate_refs"][0]["candidate_id"].startswith("tool_schema:read_file:")
    assert manifest["available_deferred_tool_candidates"][0]["candidate_ref"]["candidate_id"].startswith(
        "tool_schema:firecrawl_fetch:"
    )
