from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_mcp_metadata_trust_is_wired_at_import_schema_and_both_execution_entries() -> None:
    discovery = (BACKEND_ROOT / "app/services/resource_discovery.py").read_text(encoding="utf-8")
    agent_tools = (BACKEND_ROOT / "app/services/agent_tools.py").read_text(encoding="utf-8")
    generic_handler = (BACKEND_ROOT / "app/tools/handlers/mcp.py").read_text(encoding="utf-8")
    dynamic_handler = (BACKEND_ROOT / "app/services/agent_tool_domains/web_mcp.py").read_text(encoding="utf-8")

    assert discovery.count("_apply_discovered_mcp_metadata(") >= 7
    assert "prepare_mcp_metadata_candidate(" in discovery
    assert "apply_mcp_metadata_candidate(" in discovery
    assert 'description=mcp_tool.get("description", "")[:500]' not in discovery
    assert 'description=mcp_tool.get("description", description)[:500]' not in discovery
    assert "is_mcp_metadata_runtime_approved" in agent_tools
    assert "is_mcp_metadata_runtime_approved" in generic_handler
    assert "is_mcp_metadata_runtime_approved" in dynamic_handler


def test_raw_mcp_metadata_is_not_used_by_model_tool_definition_builder() -> None:
    source = (BACKEND_ROOT / "app/services/agent_tools.py").read_text(encoding="utf-8")
    model_builder = source[
        source.index("# Build OpenAI function-calling format") : source.index("result.append(tool_def)")
    ]
    assert "mcp_raw_description" not in model_builder
    assert "mcp_raw_schema" not in model_builder
    assert '"description": t.description' in model_builder
    assert "raw_params = t.parameters_schema" in model_builder
