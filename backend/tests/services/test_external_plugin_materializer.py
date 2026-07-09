from __future__ import annotations

import pytest

from app.services.external_capabilities.plugin_materializer import (
    build_plugin_root_files,
    materialize_plugin_root,
    plugin_root_path,
    resolve_component_variables,
    substitute_plugin_variables,
    substitute_user_config_in_content,
    substitute_user_config_variables,
)


# --- ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA} (FreeCode pluginOptionsStorage.ts:326-344) ---


def test_substitute_plugin_root_replaces_every_occurrence():
    value = "python ${CLAUDE_PLUGIN_ROOT}/hooks/guard.py --root ${CLAUDE_PLUGIN_ROOT}"
    out = substitute_plugin_variables(value, plugin_root="/ws/plugins/toolkit")
    assert out == "python /ws/plugins/toolkit/hooks/guard.py --root /ws/plugins/toolkit"


def test_substitute_plugin_data_left_literal_when_absent():
    value = "${CLAUDE_PLUGIN_ROOT}/x ${CLAUDE_PLUGIN_DATA}/state"
    out = substitute_plugin_variables(value, plugin_root="/ws/p")
    assert out == "/ws/p/x ${CLAUDE_PLUGIN_DATA}/state"


def test_substitute_plugin_data_replaced_when_provided():
    value = "${CLAUDE_PLUGIN_DATA}/state.db"
    out = substitute_plugin_variables(value, plugin_root="/ws/p", plugin_data="/data/toolkit")
    assert out == "/data/toolkit/state.db"


def test_substitute_plugin_root_uses_function_replacement_for_dollar_paths():
    # Function-form replacement so `$`-sequences in the path aren't interpreted
    # as regex backreferences (FreeCode uses the same guard for NTFS paths).
    out = substitute_plugin_variables("${CLAUDE_PLUGIN_ROOT}/x", plugin_root="/ws/$1$&/p")
    assert out == "/ws/$1$&/p/x"


# --- ${user_config.KEY} config form (throws on missing, pluginOptionsStorage.ts:356-370) ---


def test_substitute_user_config_replaces_declared_keys():
    out = substitute_user_config_variables("--token ${user_config.API_KEY}", {"API_KEY": "secret-123"})
    assert out == "--token secret-123"


def test_substitute_user_config_raises_on_missing_key():
    with pytest.raises(ValueError, match="API_KEY"):
        substitute_user_config_variables("${user_config.API_KEY}", {})


# --- ${user_config.KEY} content form (sensitive-masked, missing-literal, :385-400) ---


def test_user_config_in_content_masks_sensitive_and_keeps_unknown_literal():
    content = "Key: ${user_config.API_KEY}, Endpoint: ${user_config.ENDPOINT}, Missing: ${user_config.NONE}"
    out = substitute_user_config_in_content(
        content,
        {"API_KEY": "secret", "ENDPOINT": "https://api.dev"},
        {"API_KEY": {"sensitive": True}, "ENDPOINT": {"sensitive": False}},
    )
    assert "secret" not in out
    assert "[sensitive option 'API_KEY' not available in plugin content]" in out
    assert "Endpoint: https://api.dev" in out
    assert "${user_config.NONE}" in out  # unknown key stays literal


# --- materialize plugin body to workspace/plugins/<name> ---


def test_materialize_plugin_root_writes_files_and_returns_abs_root(tmp_path):
    root = materialize_plugin_root(
        workspace=tmp_path,
        plugin_name="toolkit",
        files=[
            {"path": "hooks/guard.py", "content": "print('guard')"},
            {"path": "SKILL.md", "content": "---\nname: t\n---\nBody"},
        ],
    )
    assert root == (tmp_path / "plugins" / "toolkit").resolve()
    assert root.is_absolute()
    assert (root / "hooks" / "guard.py").read_text(encoding="utf-8") == "print('guard')"
    assert (root / "SKILL.md").exists()


def test_materialize_plugin_root_rejects_path_traversal(tmp_path):
    root = materialize_plugin_root(
        workspace=tmp_path,
        plugin_name="toolkit",
        files=[
            {"path": "../escape.py", "content": "bad"},
            {"path": "ok.py", "content": "good"},
        ],
    )
    assert (root / "ok.py").exists()
    assert not (tmp_path / "escape.py").exists()
    assert not (tmp_path.parent / "escape.py").exists()


def test_plugin_root_path_sanitizes_name(tmp_path):
    root = plugin_root_path(tmp_path, "../../evil")
    assert str(root).startswith(str((tmp_path / "plugins").resolve()))


def test_build_plugin_root_files_reconstructs_tree_from_components():
    components = [
        {
            "component_type": "skill",
            "local_name": "audit",
            "source_path": "skills/audit/SKILL.md",
            "metadata": {
                "files": [
                    {"path": "SKILL.md", "content": "# Audit"},
                    {"path": "scripts/run.py", "content": "print('x')"},
                ]
            },
        },
        {
            "component_type": "subagent",
            "local_name": "reviewer",
            "source_path": "agents/reviewer.md",
            "metadata": {"definition": "---\nname: reviewer\n---\nReview."},
        },
        {
            "component_type": "slash_command",
            "local_name": "about",
            "source_path": "",
            "metadata": {"content": "# About"},
        },
    ]
    files = {entry["path"]: entry["content"] for entry in build_plugin_root_files(components)}
    assert files["skills/audit/SKILL.md"] == "# Audit"
    assert files["skills/audit/scripts/run.py"] == "print('x')"
    assert files["agents/reviewer.md"].startswith("---\nname: reviewer")
    assert files["commands/about.md"] == "# About"


# --- component-level resolution (hooks/mcp/command/agent/skill) ---


def test_resolve_component_variables_substitutes_hook_command_root_and_config():
    component = {
        "component_type": "hook",
        "qualified_name": "toolkit:hook:PreToolUse:*:0",
        "runtime_projection": {
            "event": "PreToolUse",
            "spec": {
                "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/h.py --k ${user_config.API_KEY}"}]
            },
        },
    }
    resolved = resolve_component_variables(
        component,
        plugin_root="/ws/plugins/toolkit",
        user_config={"API_KEY": "secret"},
        user_config_schema={"API_KEY": {"sensitive": True}},
    )
    command = resolved["runtime_projection"]["spec"]["hooks"][0]["command"]
    assert command == "/ws/plugins/toolkit/h.py --k secret"
    # Original component is not mutated in place.
    assert "${CLAUDE_PLUGIN_ROOT}" in component["runtime_projection"]["spec"]["hooks"][0]["command"]


def test_resolve_component_variables_substitutes_mcp_config():
    component = {
        "component_type": "mcp_server",
        "runtime_projection": {
            "server_name": "db",
            "config": {
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/db",
                "args": ["--config", "${CLAUDE_PLUGIN_ROOT}/db.json"],
                "env": {"TOKEN": "${user_config.DB_TOKEN}"},
            },
        },
    }
    resolved = resolve_component_variables(
        component,
        plugin_root="/ws/plugins/conn",
        user_config={"DB_TOKEN": "t-1"},
        user_config_schema={"DB_TOKEN": {"sensitive": True}},
    )
    config = resolved["runtime_projection"]["config"]
    assert config["command"] == "/ws/plugins/conn/bin/db"
    assert config["args"] == ["--config", "/ws/plugins/conn/db.json"]
    assert config["env"]["TOKEN"] == "t-1"


def test_resolve_component_variables_masks_sensitive_in_skill_content():
    component = {
        "component_type": "skill",
        "runtime_projection": {"folder_name": "helper"},
        "metadata": {
            "files": [
                {"path": "SKILL.md", "content": "Root=${CLAUDE_PLUGIN_ROOT} Secret=${user_config.API_KEY}"},
            ]
        },
    }
    resolved = resolve_component_variables(
        component,
        plugin_root="/ws/plugins/pack",
        user_config={"API_KEY": "secret"},
        user_config_schema={"API_KEY": {"sensitive": True}},
    )
    content = resolved["metadata"]["files"][0]["content"]
    assert "Root=/ws/plugins/pack" in content
    # Skill content goes to the model prompt: sensitive value must be masked.
    assert "secret" not in content
    assert "[sensitive option 'API_KEY' not available in plugin content]" in content
