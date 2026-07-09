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
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "write_file", "hooks": [{"type": "command", "command": "python guard.py"}]}
                    ]
                }
            }
        ),
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


def test_manifest_commands_merge_with_directory_and_support_inline_content(tmp_path):
    # FreeCode schemas.ts:429-452 — manifest `commands` supplement the commands/
    # directory ("in addition to"), NOT mutually exclusive; the object-mapping
    # form (schemas.ts:385-416) carries inline `content` XOR a `source` path.
    plugin_root = tmp_path / "toolkit"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / "commands").mkdir()
    (plugin_root / "commands" / "build.md").write_text(
        "---\ndescription: Build it\n---\nRun the build.", encoding="utf-8"
    )
    (plugin_root / "docs").mkdir()
    (plugin_root / "docs" / "guide.md").write_text("---\ndescription: Guide\n---\nGuide body.", encoding="utf-8")
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "toolkit",
                "commands": {
                    "about": {
                        "content": "# About\nInline about text.",
                        "description": "About this plugin",
                        "allowedTools": ["read_file"],
                    },
                    "guide": {"source": "./docs/guide.md"},
                },
            }
        ),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/toolkit")

    names = {component.qualified_name for component in bundle.components}
    # Directory command AND both manifest commands present (merge, not exclusive).
    assert {"toolkit:build", "toolkit:about", "toolkit:guide"} <= names
    about = bundle.component_by_name("toolkit:about")
    assert about.component_type == "slash_command"
    assert about.metadata["content"] == "# About\nInline about text."
    assert about.runtime_projection["description"] == "About this plugin"
    assert about.runtime_projection["allowed_tools"] == ["read_file"]
    assert about.source_path == ""  # inline content has no source file
    guide = bundle.component_by_name("toolkit:guide")
    assert guide.metadata["content"] == "---\ndescription: Guide\n---\nGuide body."


def test_manifest_agents_and_skills_supplement_directories(tmp_path):
    # FreeCode schemas.ts:460-499 — manifest `agents`/`skills` supplement the
    # agents/ and skills/ directories.
    plugin_root = tmp_path / "pack"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / "agents").mkdir()
    (plugin_root / "agents" / "dir-agent.md").write_text(
        "---\nname: dir-agent\ndescription: Directory agent\n---\nBody", encoding="utf-8"
    )
    (plugin_root / "extra").mkdir()
    (plugin_root / "extra" / "extra-agent.md").write_text(
        "---\nname: extra-agent\ndescription: Manifest agent\n---\nBody", encoding="utf-8"
    )
    (plugin_root / "toolkits" / "helper").mkdir(parents=True)
    (plugin_root / "toolkits" / "helper" / "SKILL.md").write_text(
        "---\nname: helper\ndescription: Manifest skill\n---\nHelp", encoding="utf-8"
    )
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "pack",
                "agents": ["./extra/extra-agent.md"],
                "skills": "./toolkits/helper",
            }
        ),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/pack")

    names = {component.qualified_name for component in bundle.components}
    assert {"pack:dir-agent", "pack:extra-agent", "pack:helper"} <= names
    assert bundle.component_by_name("pack:extra-agent").component_type == "subagent"
    assert bundle.component_by_name("pack:helper").component_type == "skill"


def test_manifest_hooks_three_forms_supplement_hooks_json(tmp_path):
    # FreeCode schemas.ts:348-373 — manifest `hooks` union: path | inline
    # HooksSchema | array, all "in addition to" hooks/hooks.json.
    plugin_root = tmp_path / "hooked"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / "hooks").mkdir()
    (plugin_root / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"matcher": "write_file", "hooks": [{"type": "command", "command": "python a.py"}]}]
                }
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "more-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [{"matcher": "read_file", "hooks": [{"type": "command", "command": "python b.py"}]}]
                }
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hooked",
                "hooks": [
                    "./more-hooks.json",
                    {"SessionStart": [{"hooks": [{"type": "command", "command": "python c.py"}]}]},
                ],
            }
        ),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/hooked")

    hook_events = {
        component.runtime_projection["event"] for component in bundle.components if component.component_type == "hook"
    }
    assert hook_events == {"PreToolUse", "PostToolUse", "SessionStart"}


def test_manifest_mcp_servers_path_and_array_forms(tmp_path):
    # FreeCode schemas.ts:543-572 — manifest `mcpServers` union: path | mcpb |
    # dict | array, "in addition to" .mcp.json.
    plugin_root = tmp_path / "connectors"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"browser": {"command": "npx", "args": ["@acme/browser"]}}}),
        encoding="utf-8",
    )
    (plugin_root / "servers.json").write_text(
        json.dumps({"mcpServers": {"database": {"command": "npx", "args": ["@acme/db"]}}}),
        encoding="utf-8",
    )
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "connectors",
                "mcpServers": ["./servers.json", {"search": {"command": "npx", "args": ["@acme/search"]}}],
            }
        ),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/connectors")

    mcp_names = {
        component.qualified_name for component in bundle.components if component.component_type == "mcp_server"
    }
    assert mcp_names == {
        "connectors:mcp:browser",
        "connectors:mcp:database",
        "connectors:mcp:search",
    }


def test_manifest_metadata_captures_author_homepage_and_dependencies(tmp_path):
    # FreeCode schemas.ts:274-320 — plugin.json metadata carries author,
    # homepage, license, keywords, dependencies.
    plugin_root = tmp_path / "rich"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "rich",
                "version": "2.0.0",
                "author": {"name": "Acme Inc", "email": "hi@acme.dev", "url": "https://acme.dev"},
                "homepage": "https://acme.dev/rich",
                "license": "MIT",
                "keywords": ["tools", "review"],
                "dependencies": ["base-pack", "utils@other-market"],
            }
        ),
        encoding="utf-8",
    )

    bundle = load_cc_plugin_bundle(plugin_root, source_uri="github:acme/rich")

    assert bundle.manifest_metadata["author"] == {
        "name": "Acme Inc",
        "email": "hi@acme.dev",
        "url": "https://acme.dev",
    }
    assert bundle.manifest_metadata["homepage"] == "https://acme.dev/rich"
    assert bundle.manifest_metadata["license"] == "MIT"
    assert bundle.manifest_metadata["keywords"] == ["tools", "review"]
    assert bundle.manifest_metadata["dependencies"] == ["base-pack", "utils@other-market"]


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
