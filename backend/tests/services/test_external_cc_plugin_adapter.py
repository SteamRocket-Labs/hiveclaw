from __future__ import annotations

import json

from app.services.external_capabilities.cc_plugin_adapter import load_cc_plugin_bundle


def test_cc_plugin_adapter_namespaces_components_and_ignores_agent_escalation(tmp_path):
    plugin_root = tmp_path / "review-pack"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / "commands").mkdir()
    (plugin_root / "skills" / "audit").mkdir(parents=True)
    (plugin_root / "agents").mkdir()
    (plugin_root / "hooks").mkdir()

    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "review-pack",
                "version": "1.2.3",
                "description": "Review helpers",
                "repository": "https://github.com/acme/review-pack",
                "userConfig": {
                    "apiKey": {
                        "type": "string",
                        "title": "API key",
                        "description": "Secret token",
                        "sensitive": True,
                    },
                    "endpoint": {
                        "type": "string",
                        "title": "Endpoint",
                        "description": "Public endpoint",
                    },
                },
                "outputStyles": "output-styles",
                "lspServers": {"typescript": {"command": "tsserver", "fileExtensions": [".ts"]}},
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "commands" / "check.md").write_text(
        "---\ndescription: Run checks\nallowed-tools:\n  - read_file\n---\nRun project checks.",
        encoding="utf-8",
    )
    (plugin_root / "skills" / "audit" / "SKILL.md").write_text(
        "---\nname: audit\ndescription: Audit code\n---\nAudit the selected code.",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Review code\npermissionMode: bypassPermissions\nhooks:\n  - shell\nmcpServers:\n  github: {}\ntools:\n  - read_file\n---\nReview like a senior engineer.",
        encoding="utf-8",
    )
    (plugin_root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "write_file", "hooks": [{"type": "command", "command": "python guard.py"}]}]}}),
        encoding="utf-8",
    )
    (plugin_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"browser": {"command": "npx", "args": ["@acme/browser-mcp"]}}}),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/review-pack")

    assert bundle.source_format == "cc_plugin"
    assert bundle.plugin_name == "review-pack"
    assert {component.qualified_name for component in bundle.components} == {
        "review-pack:check",
        "review-pack:audit",
        "review-pack:reviewer",
        "review-pack:hook:PreToolUse:write_file:0",
        "review-pack:mcp:browser",
    }
    reviewer = bundle.component_by_name("review-pack:reviewer")
    assert reviewer.component_type == "subagent"
    assert reviewer.runtime_projection["tools"] == ["read_file"]
    assert reviewer.metadata["definition"].startswith("---\nname: reviewer")
    assert "permissionMode" not in reviewer.runtime_projection
    assert set(reviewer.ignored_fields) == {"hooks", "mcpServers", "permissionMode"}
    skill = bundle.component_by_name("review-pack:audit")
    assert skill.metadata["files"] == [
        {
            "path": "SKILL.md",
            "content": "---\nname: audit\ndescription: Audit code\n---\nAudit the selected code.",
        }
    ]
    assert bundle.credential_requirements == [
        {"key": "apiKey", "sensitive": True, "source": "manifest.userConfig"},
        {"key": "endpoint", "sensitive": False, "source": "manifest.userConfig"},
    ]
    assert bundle.unsupported_components == [
        {"component_type": "lspServers", "reason": "not_supported_by_hive_runtime_yet"},
        {"component_type": "outputStyles", "reason": "not_supported_by_hive_runtime_yet"},
    ]


def test_cc_plugin_adapter_rejects_path_traversal_component_paths(tmp_path):
    plugin_root = tmp_path / "bad-pack"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "bad-pack", "commands": "../escape.md"}),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/bad-pack")

    assert bundle.components == []
    assert bundle.admission_notes == [
        {
            "code": "component_path_escape",
            "component_type": "commands",
            "path": "../escape.md",
        }
    ]
