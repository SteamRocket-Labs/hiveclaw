from __future__ import annotations

from app.services.external_capabilities.context_projection import build_agent_extension_context
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle


def test_agent_extension_context_keeps_native_tools_separate_from_plugin_components():
    bundle = NormalizedExternalPluginBundle(
        source_format="cc_plugin",
        source_uri="github:acme/review-pack",
        plugin_name="review-pack",
        version="1.0.0",
        description="Review helpers",
        manifest_sha256="manifest",
        components=(
            ExternalCapabilityComponent(
                component_type="slash_command",
                local_name="check",
                qualified_name="review-pack:check",
                source_path="commands/check.md",
                content_sha256="cmd",
                runtime_projection={"description": "Run checks"},
            ),
            ExternalCapabilityComponent(
                component_type="subagent",
                local_name="reviewer",
                qualified_name="review-pack:reviewer",
                source_path="agents/reviewer.md",
                content_sha256="agent",
                runtime_projection={"description": "Review code", "tools": ["read_file"]},
            ),
            ExternalCapabilityComponent(
                component_type="skill",
                local_name="audit",
                qualified_name="review-pack:audit",
                source_path="skills/audit/SKILL.md",
                content_sha256="skill",
                runtime_projection={"description": "Audit code"},
            ),
            ExternalCapabilityComponent(
                component_type="mcp_server",
                local_name="browser",
                qualified_name="review-pack:mcp:browser",
                source_path=".mcp.json",
                content_sha256="mcp",
                runtime_projection={"server_name": "browser"},
            ),
        ),
    )

    snapshot = build_agent_extension_context(
        native_tool_names=("read_file", "write_file"),
        approved_bundles=(bundle,),
    )

    assert snapshot.native_tool_names == ("read_file", "write_file")
    assert snapshot.plugin_component_names == (
        "review-pack:check",
        "review-pack:reviewer",
        "review-pack:audit",
        "review-pack:mcp:browser",
    )
    assert snapshot.cc_aligned_payload == {
        "tools": ["read_file", "write_file"],
        "mcp_servers": ["review-pack:mcp:browser"],
        "slash_commands": ["review-pack:check"],
        "agents": ["review-pack:reviewer"],
        "skills": ["review-pack:audit"],
        "plugins": ["review-pack"],
        "hooks": [],
    }
