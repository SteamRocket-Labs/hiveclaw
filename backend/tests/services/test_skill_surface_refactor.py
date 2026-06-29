from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_builtin_skill_defaults_are_agent_preinstalled_platform_capsules():
    from app.services.skill_seeder import BUILTIN_SKILLS

    by_folder = {skill["folder_name"]: skill for skill in BUILTIN_SKILLS}

    expected_builtin_folders = {
        "skill-creator",
        "mcp-installer",
        "web-research",
        "feishu-integration",
        "plaza-guide",
        "email-guide",
        "dingtalk-integration",
        "atlassian-rovo",
        "skill-marketplace",
    }
    assert set(by_folder) == expected_builtin_folders
    assert {folder for folder, skill in by_folder.items() if skill.get("is_default")} == expected_builtin_folders
    assert by_folder["web-research"]["name"] == "Advanced Web Research"
    assert by_folder["skill-marketplace"]["is_default"] is True


def test_removed_skill_marketplace_split_brain_is_retired():
    from app.skills.retired import RETIRED_BUILTIN_SKILL_FOLDERS

    retired = set(RETIRED_BUILTIN_SKILL_FOLDERS)
    assert {"find-skills", "skill-vetter"} <= retired

    templates_dir = REPO_ROOT / "backend" / "app" / "templates" / "skills"
    assert not (templates_dir / "find-skills").exists()
    assert not (templates_dir / "skill-vetter").exists()
    assert (templates_dir / "skill-marketplace" / "SKILL.md").is_file()
    skill_marketplace_md = (templates_dir / "skill-marketplace" / "SKILL.md").read_text(encoding="utf-8")
    assert "is_default: false" not in skill_marketplace_md


def test_default_agent_skill_lists_do_not_reference_retired_skill_slugs():
    from app.services.agent_seeder import MEESEEKS_SKILLS, MORTY_SKILLS
    from app.skills.retired import RETIRED_BUILTIN_SKILL_FOLDERS

    assigned = set(MORTY_SKILLS) | set(MEESEEKS_SKILLS)
    assert assigned.isdisjoint(RETIRED_BUILTIN_SKILL_FOLDERS)


def test_pack_skill_entrypoints_are_agent_preinstalled_without_unlocking_pack_tools():
    import yaml

    from app.services.skill_seeder import _load_pack_skill_dicts

    pack_skills = {skill["folder_name"]: skill for skill in _load_pack_skill_dicts()}
    office_manifest = yaml.safe_load((REPO_ROOT / "backend" / "packs" / "office_pack" / "pack.yaml").read_text())

    assert set(pack_skills) == {"office-productivity"}
    assert pack_skills["office-productivity"]["is_default"] is True
    assert office_manifest["activation"]["default_state"] == "inactive"


def test_office_single_purpose_template_copies_are_removed():
    templates_dir = REPO_ROOT / "backend" / "app" / "templates" / "skills"
    pack_skills_dir = REPO_ROOT / "backend" / "packs" / "office_pack" / "skills"
    retired_office_templates = {
        "docx-generator",
        "xlsx-processor",
        "pptx-generator",
        "pdf-generator",
        "weekly-report-generator",
        "meeting-minutes",
        "pitch-deck-generator",
    }

    for folder_name in retired_office_templates:
        assert not (templates_dir / folder_name).exists(), folder_name
        assert not (pack_skills_dir / folder_name).exists(), folder_name

    assert (pack_skills_dir / "office-productivity" / "SKILL.md").is_file()


def test_skill_files_do_not_claim_loading_unlocks_tools():
    skill_roots = [
        REPO_ROOT / "backend" / "app" / "templates" / "system_skills",
        REPO_ROOT / "backend" / "app" / "templates" / "skills",
        REPO_ROOT / "backend" / "agent_template" / "skills",
        REPO_ROOT / "backend" / "packs" / "office_pack" / "skills",
    ]
    forbidden = (
        "activates the",
        "loading it means",
        "loading this skill unlocks",
        "unlocks the",
        "unlock the",
        "become callable",
    )

    failures: list[str] = []
    for root in skill_roots:
        if not root.exists():
            continue
        for skill_path in root.glob("*/SKILL.md"):
            content = skill_path.read_text(encoding="utf-8").lower()
            for phrase in forbidden:
                if phrase in content:
                    failures.append(f"{skill_path.relative_to(REPO_ROOT)} contains {phrase!r}")

    assert not failures, "\n".join(failures)


def test_memory_prompt_and_save_tool_are_not_invitation_style():
    prompt_files = [
        REPO_ROOT / "backend" / "app" / "runtime" / "prompt_sections" / "system.py",
        REPO_ROOT / "backend" / "app" / "runtime" / "prompt_sections" / "memory.py",
        REPO_ROOT / "backend" / "app" / "tools" / "handlers" / "memory.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in prompt_files)
    lowered = combined.lower()

    assert "explicitly asks you to remember" in lowered
    assert "explicit user-commanded" in lowered
    assert "worth remembering" not in lowered
    assert "immediately activatable" not in lowered
    assert "encounter information" not in lowered
