from __future__ import annotations

import json

from app.services.external_capabilities.codex_plugin_adapter import load_codex_plugin_bundle


def test_codex_plugin_adapter_normalizes_skills_and_app_connector_manifest(tmp_path):
    plugin_root = tmp_path / "github"
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    (plugin_root / "skills" / "triage").mkdir(parents=True)
    (plugin_root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "github",
                "version": "0.1.6",
                "description": "GitHub workflows",
                "skills": "./skills/",
                "apps": "./.app.json",
                "interface": {"displayName": "GitHub"},
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / ".app.json").write_text(
        json.dumps({"apps": {"github": {"id": "connector_123", "required": True}}}),
        encoding="utf-8",
    )
    (plugin_root / "skills" / "triage" / "SKILL.md").write_text(
        "---\nname: triage\ndescription: Triage GitHub issues\n---\nTriage issues.",
        encoding="utf-8",
    )

    bundle = load_codex_plugin_bundle(plugin_root, source_uri="codex:github")

    assert bundle.source_format == "codex_plugin"
    assert bundle.plugin_name == "github"
    assert bundle.version == "0.1.6"
    skill = bundle.component_by_name("github:triage")
    assert skill.component_type == "skill"
    assert skill.metadata["files"] == [
        {
            "path": "SKILL.md",
            "content": "---\nname: triage\ndescription: Triage GitHub issues\n---\nTriage issues.",
        }
    ]
    assert bundle.unsupported_components == [
        {
            "component_type": "apps",
            "reason": "codex_app_connector_requires_hive_connector_binding",
            "apps": ["github"],
        }
    ]
