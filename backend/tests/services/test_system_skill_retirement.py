from pathlib import Path


RETIRED_DEFAULT_GUIDE_SKILLS = {
    "complex-task-executor",
    "workspace-guide",
    "trigger-guide",
    "memory-guide",
    "messaging-guide",
    "delegation-guide",
}


def test_retired_default_guides_are_not_seeded_as_builtin_skills():
    from app.services.skill_seeder import BUILTIN_SKILLS
    from app.skills.retired import RETIRED_BUILTIN_SKILL_FOLDERS

    seeded = {skill["folder_name"] for skill in BUILTIN_SKILLS}

    assert RETIRED_DEFAULT_GUIDE_SKILLS.isdisjoint(seeded)
    assert RETIRED_DEFAULT_GUIDE_SKILLS <= set(RETIRED_BUILTIN_SKILL_FOLDERS)


def test_retired_default_guide_templates_are_removed():
    repo_root = Path(__file__).resolve().parents[3]
    system_skills_dir = repo_root / "backend" / "app" / "templates" / "system_skills"

    for folder_name in RETIRED_DEFAULT_GUIDE_SKILLS - {"complex-task-executor"}:
        assert not (system_skills_dir / folder_name).exists(), folder_name


def test_core_prompt_no_longer_requires_retired_guides():
    repo_root = Path(__file__).resolve().parents[3]
    prompt_files = [
        repo_root / "backend" / "app" / "runtime" / "prompt_sections" / "system.py",
        repo_root / "backend" / "app" / "runtime" / "prompt_sections" / "memory.py",
        repo_root / "backend" / "app" / "runtime" / "prompt_sections" / "executing_actions.py",
        repo_root / "backend" / "app" / "runtime" / "prompt_sections" / "tools.py",
        repo_root / "backend" / "app" / "tools" / "handlers" / "skills.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in prompt_files)

    forbidden = (
        "Memory Guide",
        "Messaging Guide",
        "Workspace Guide",
        "Trigger Management Guide",
        "Delegation Guide",
        "Complex Task Executor",
        "memory-guide",
        "messaging-guide",
        "workspace-guide",
        "trigger-guide",
        "delegation-guide",
        "complex-task-executor",
    )
    for phrase in forbidden:
        assert phrase not in combined


def test_default_agent_templates_use_work_ledger_not_complex_task_skill():
    repo_root = Path(__file__).resolve().parents[3]
    agent_seeder = (repo_root / "backend" / "app" / "services" / "agent_seeder.py").read_text(encoding="utf-8")

    assert "complex-task-executor" not in agent_seeder
    assert "Complex Task Executor" not in agent_seeder
    assert "ALWAYS create a plan.md" not in agent_seeder
    assert "track_todo" in agent_seeder
    assert "read_ledger" in agent_seeder
