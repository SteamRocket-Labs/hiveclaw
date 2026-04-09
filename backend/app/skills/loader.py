"""Discover and load workspace skills from flat or folder-based layouts."""

from __future__ import annotations

from pathlib import Path

from .parser import SkillParser
from .types import ParsedSkill


class WorkspaceSkillLoader:
    """Load skills from an agent workspace."""

    def __init__(self, parser: SkillParser | None = None) -> None:
        self.parser = parser or SkillParser()

    def load_from_workspace(self, workspace: Path) -> list[ParsedSkill]:
        skills_dir = workspace / "skills"
        if not skills_dir.exists():
            return []

        skills: list[ParsedSkill] = []
        for entry in sorted(skills_dir.iterdir()):
            if entry.name.startswith("."):
                continue

            if entry.is_dir():
                for filename in ("SKILL.md", "skill.md"):
                    skill_file = entry / filename
                    if skill_file.exists():
                        skills.append(
                            self.parser.parse_file(
                                skill_file,
                                relative_path=f"skills/{entry.name}/{skill_file.name}",
                                default_name=entry.name,
                            )
                        )
                        break
            elif entry.is_file() and entry.suffix == ".md":
                skills.append(
                    self.parser.parse_file(
                        entry,
                        relative_path=f"skills/{entry.name}",
                        default_name=entry.stem,
                    )
                )

        return skills
