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
        available_deferred_tools=["firecrawl_fetch"],
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
    assert categories["system_prompt"]["tokens"] > 0
    assert categories["system_tools"]["tokens"] > 0
    assert categories["custom_agents"]["tokens"] > 0
    assert categories["memory_files"]["tokens"] > 0
    assert categories["skills"]["tokens"] > 0
    assert categories["messages"]["tokens"] > 0
    assert categories["mcp_tools"]["tokens"] > 0
    assert categories["free_space"]["tokens"] >= 0
    assert ledger["used_tokens"] == sum(
        item["tokens"] for item in ledger["categories"] if item["name"] != "free_space"
    )
