from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_turn_envelope_projects_default_hook_catalog_statuses():
    from app.runtime.turn_envelope import build_turn_envelope

    agent_id = uuid4()
    session = SimpleNamespace(id=uuid4(), source_channel="web", session_kind="human_chat")

    envelope = build_turn_envelope(agent_id=agent_id, session=session, active_run={"metadata": {}})

    hook_state = envelope["hook_state"]
    assert hook_state["PreToolUse"] == "supported_active"
    assert hook_state["UserPromptSubmit"] == "supported_active"
    assert hook_state["Stop"] == "supported_active"
    assert hook_state["PostToolUse"] == "supported_observe_only"
    assert hook_state["Setup"] == "unsupported_with_reason"
    assert set(hook_state.values()) <= {
        "supported_active",
        "supported_observe_only",
        "declared_not_wired",
        "unsupported_with_reason",
    }


def test_build_turn_envelope_and_prompt_manifest_from_active_run_metadata():
    from app.runtime.turn_envelope import build_prompt_assembly_manifest, build_turn_envelope

    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    session = SimpleNamespace(id=session_id, source_channel="web", session_kind="human_chat")
    active_run = {
        "id": str(run_id),
        "status": "running",
        "metadata": {
            "turn_id": "turn-42",
            "model": {"provider": "openai", "model": "gpt-4.1"},
            "context_policy": {"model_window": 128000, "active_context_tokens": 2048},
            "permission_profile": {"approval_policy": "granular", "sandbox": "workspace_write"},
            "multi_agent_mode": "proactive",
            "active_tool_names": ["read_file", "spawn_subagent"],
            "deferred_tool_names": ["github_review"],
            "skill_catalog_refs": ["skill:python"],
            "mcp_server_refs": ["mcp:linear"],
            "memory_refs": ["t3:user"],
            "team_mailbox_refs": [{"team_id": "team-1", "member_name": "critic"}],
            "a2a_collaborator_refs": [{"agent_id": "agent-2", "name": "Finance"}],
            "hook_state": {"UserPromptSubmit": "supported_active"},
            "prompt_sections": [
                {"name": "frozen_identity", "kind": "frozen", "token_estimate": 100},
                {"name": "team_context", "kind": "dynamic", "token_estimate": 40},
            ],
            "output_cap": 4096,
            "trace_id": "trace-1",
            "span_id": "span-1",
        },
    }

    envelope = build_turn_envelope(agent_id=agent_id, session=session, active_run=active_run)

    assert envelope["schema"] == "hive.ccplus.turn_envelope.v1"
    assert envelope["turn_id"] == "turn-42"
    assert envelope["session_id"] == str(session_id)
    assert envelope["runtime_task_id"] == str(run_id)
    assert envelope["multi_agent_mode"] == "proactive"
    assert envelope["active_tool_names"] == ["read_file", "spawn_subagent"]
    assert envelope["team_mailbox_refs"][0]["member_name"] == "critic"
    assert envelope["hook_state"]["UserPromptSubmit"] == "supported_active"
    assert envelope["trace"]["trace_id"] == "trace-1"

    manifest = build_prompt_assembly_manifest(envelope)

    assert manifest["schema"] == "hive.ccplus.prompt_assembly_manifest.v1"
    assert manifest["turn_id"] == "turn-42"
    assert manifest["frozen_sections"] == ["frozen_identity"]
    assert manifest["dynamic_sections"] == ["team_context"]
    assert manifest["loaded_skills"] == ["skill:python"]
    assert manifest["mcp_instructions_delta"]["server_refs"] == ["mcp:linear"]
    assert manifest["context_budget"]["model_window"] == 128000


def test_runtime_prompt_manifest_includes_context_usage_ledger():
    from app.runtime.turn_envelope import build_runtime_prompt_assembly_manifest

    manifest = build_runtime_prompt_assembly_manifest(
        turn_id="turn-context",
        session_id="session-context",
        frozen_prefix="## Identity\nYou are Analyst.",
        dynamic_suffix="## Dynamic\nRuntime metadata.",
        provider_system_prompt="## Identity\nYou are Analyst.",
        provider_dynamic_notice="## Memory\nRemember user preferences.",
        context_budget={},
        model_window=1000,
        tools_for_llm=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read files.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mcp_list_resources",
                    "description": "List MCP resources.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        active_tool_groups=[{"name": "web", "tools": ["web_search"]}],
        available_deferred_tools=[
            {
                "name": "firecrawl_fetch",
                "group": "web_pack",
                "reason": "advanced crawl needed",
                "selector": "select:firecrawl_fetch",
                "schema_token_cost": 42,
                "risk": "network_read",
            }
        ],
        memory_snapshot="memory text",
        retrieval_context="knowledge text",
        skill_catalog="## Skills\n- python",
        mcp_server_refs=["mcp:linear"],
        available_agent_types=[{"type": "critic"}],
        messages=[{"role": "user", "content": "请检查 context usage"}],
    )

    ledger = manifest["context_usage_ledger"]
    categories = {item["name"]: item for item in ledger["categories"]}

    assert ledger["schema"] == "hive.ccplus.context_usage_ledger.v1"
    assert ledger["model_window_tokens"] == 1000
    assert ledger["deferred_tool_index_tokens"] == categories["deferred_tool_index"]["tokens"]
    assert ledger["loaded_tool_schema_tokens"] == categories["system_tools"]["tokens"] + categories["mcp_tools"]["tokens"]
    assert categories["system_prompt"]["tokens"] > 0
    assert categories["system_tools"]["tokens"] > 0
    assert categories["custom_agents"]["tokens"] > 0
    assert categories["memory_files"]["tokens"] > 0
    assert categories["skills"]["tokens"] > 0
    assert categories["messages"]["tokens"] > 0
    assert categories["mcp_tools"]["tokens"] > 0
    assert categories["deferred_tool_index"]["item_count"] == 1
    assert categories["free_space"]["tokens"] >= 0
    assert ledger["used_tokens"] == sum(
        item["tokens"] for item in ledger["categories"] if item["name"] != "free_space"
    )


def test_runtime_prompt_manifest_records_selection_reasons_source_hashes_and_budget_decisions():
    from app.runtime.turn_envelope import build_runtime_prompt_assembly_manifest

    manifest = build_runtime_prompt_assembly_manifest(
        turn_id="turn-selection",
        session_id="session-selection",
        frozen_prefix="## Identity\nAnalyst.",
        dynamic_suffix="## Memory\nmemory text\n\n## Knowledge\nknowledge text",
        provider_system_prompt="## Identity\nAnalyst.",
        provider_dynamic_notice="## Memory\nmemory text",
        context_budget={"memory_budget_chars": 120, "retrieval_budget_chars": 90, "skill_catalog_budget_chars": 80},
        model_window=1000,
        tools_for_llm=[],
        memory_snapshot="memory text",
        permissions_context="",
        retrieval_context="knowledge text",
        skill_catalog="## Skills\n- python",
        active_skill_names=["python"],
        skill_ranking=[
            {"skill_name": "python", "rank": 1, "score": 300, "reasons": ["scenario_overlap:python"]}
        ],
        available_deferred_tools=[
            {
                "name": "firecrawl_fetch",
                "group": "web_pack",
                "reason": "advanced crawl needed",
                "selector": "select:firecrawl_fetch",
                "schema_token_cost": 42,
                "risk": "network_read",
            }
        ],
    )

    candidates = {item["id"]: item for item in manifest["context_candidates"]}
    selected_ids = {item["id"] for item in manifest["selected_contexts"]}
    suppressed_ids = {item["id"] for item in manifest["suppressed_contexts"]}
    budget_decisions = {item["candidate_id"]: item for item in manifest["budget_decisions"]}

    assert candidates["ctx:memory:memory_files"]["selected"] is True
    assert candidates["ctx:memory:memory_files"]["why_selected"] == "memory_snapshot_or_retrieval_context_present"
    assert candidates["ctx:memory:memory_files"]["source_hash"]
    assert candidates["ctx:skill:skill_catalog"]["source_hash"] == manifest["source_hashes"]["ctx:skill:skill_catalog"]
    assert candidates["ctx:skill:skill_catalog"]["payload"]["ranking"][0]["skill_name"] == "python"
    assert candidates["ctx:skill:skill_catalog"]["payload"]["ranking"][0]["reasons"] == ["scenario_overlap:python"]
    assert candidates["ctx:tools:available_deferred_tools"]["payload"][0]["selector"] == "select:firecrawl_fetch"
    assert candidates["ctx:tools:available_deferred_tools"]["payload"][0]["group"] == "web_pack"
    assert "ctx:memory:memory_files" in selected_ids
    assert "ctx:permissions:permissions_context" in suppressed_ids
    assert budget_decisions["ctx:memory:memory_files"]["budget_key"] == "memory_budget_chars"
    assert budget_decisions["ctx:skill:skill_catalog"]["budget_chars"] == 80
