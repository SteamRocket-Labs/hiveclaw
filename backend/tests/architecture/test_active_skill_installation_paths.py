from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_active_skill_workspace_writes_use_unified_installer() -> None:
    forbidden_snippets = {
        "app/api/agents.py": [
            "shutil.copy2(str(skill_file), str(dest))",
            "file_path.write_text(sf.content)",
            "skill_folder = skills_dir / skill.folder_name",
        ],
        "app/api/files.py": [
            "def _guard_skill_files_or_raise(",
            "from app.services.skill_guard import SkillGuardReport, scan_skill_files",
        ],
        "app/services/agent_manager.py": [
            "fp.write_text(sf.content",
            "skill_folder = skills_dir / skill.folder_name",
        ],
        "app/services/agent_seeder.py": [
            "file_path.write_text(sf.content",
            "skill_folder = skills_dir / skill.folder_name",
        ],
        "app/services/agent_tool_domains/code_exec.py": [
            "shutil.copytree(skill_dir, dest)",
            'agent_skills = ws / "skills"',
        ],
        "app/services/skill_distiller.py": [
            "target.write_text(rendered_markdown",
            "from app.services.skill_guard import scan_skill_files",
        ],
        "app/services/skill_seeder.py": [
            "fp.write_text(sf.content",
            "skill_folder = skills_dir / skill.folder_name",
        ],
        "app/tools/handlers/hr.py": [
            "file_path.write_text(sf.content)",
            "skill_folder = skills_dir / skill.folder_name",
        ],
    }

    violations: list[str] = []
    for relative_path, snippets in forbidden_snippets.items():
        source = _source(relative_path)
        for snippet in snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []
