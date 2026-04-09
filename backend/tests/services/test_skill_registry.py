from __future__ import annotations


def test_skill_loader_registry_and_catalog_support_folder_and_flat_skills(tmp_path):
    from app.skills.loader import WorkspaceSkillLoader
    from app.skills.registry import SkillRegistry

    workspace = tmp_path / "agent"
    folder_skill = workspace / "skills" / "writing"
    folder_skill.mkdir(parents=True)
    (folder_skill / "SKILL.md").write_text(
        "---\nname: Writing\ndescription: Draft polished content\n---\n# Writing\nUse this skill.\n",
        encoding="utf-8",
    )

    flat_skill = workspace / "skills" / "research.md"
    flat_skill.write_text(
        "---\nname: Research\ndescription: Search and synthesize information\n---\n# Research\nUse this skill.\n",
        encoding="utf-8",
    )

    loader = WorkspaceSkillLoader()
    parsed = loader.load_from_workspace(workspace)
    registry = SkillRegistry()
    registry.register_many(parsed)

    assert sorted(registry.names()) == ["Research", "Writing"]
    assert "Draft polished content" in registry.render_catalog()
    assert "skills/writing/SKILL.md" in registry.render_catalog()
    assert registry.load_body("Writing").startswith("# Writing")
    assert registry.load_body("Research").startswith("# Research")


def test_skill_parser_and_registry_preserve_declared_tools(tmp_path):
    from app.skills.loader import WorkspaceSkillLoader
    from app.skills.registry import SkillRegistry

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "web-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: Web Research\n"
            "description: Search and synthesize web information\n"
            "tools:\n"
            "  - web_search\n"
            "  - firecrawl_fetch\n"
            "---\n"
            "# Web Research\n"
            "Use this skill.\n"
        ),
        encoding="utf-8",
    )

    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(workspace))

    skill = registry.resolve("Web Research")
    assert skill.metadata.name == "Web Research"
    assert skill.metadata.declared_tools == ("web_search", "firecrawl_fetch")
    assert registry.load_body("Web Research").startswith("# Web Research")


def test_skill_parser_supports_declared_packs(tmp_path):
    from app.skills.loader import WorkspaceSkillLoader
    from app.skills.registry import SkillRegistry

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "feishu-assistant"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: Feishu Assistant\n"
            "description: Work with Feishu messages and docs\n"
            "packs:\n"
            "  - feishu_pack\n"
            "tools:\n"
            "  - send_feishu_message\n"
            "---\n"
            "# Feishu Assistant\n"
            "Use this skill.\n"
        ),
        encoding="utf-8",
    )

    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(workspace))

    skill = registry.resolve("Feishu Assistant")
    assert skill.metadata.declared_packs == ("feishu_pack",)
    assert skill.metadata.declared_tools == ("send_feishu_message",)
