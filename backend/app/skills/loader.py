"""Discover and load workspace skills from flat or folder-based layouts."""

from __future__ import annotations

from pathlib import Path

from .parser import SkillParser
from .retired import RETIRED_BUILTIN_SKILL_FOLDERS
from .types import ParsedSkill

RESOURCE_DIRS = frozenset({"references", "scripts", "templates", "assets", "evals"})


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
            if entry.name in RETIRED_BUILTIN_SKILL_FOLDERS:
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

    def list_resources(self, workspace: Path, skill_name: str) -> tuple[str, ...]:
        """List auxiliary files for a folder-based skill.

        Paths are returned relative to the skill folder and only include known
        skill resource directories.
        """
        skill_dir = self._resolve_skill_dir(workspace, skill_name)
        if not skill_dir:
            return ()

        resources: list[str] = []
        for resource_dir_name in sorted(RESOURCE_DIRS):
            resource_dir = skill_dir / resource_dir_name
            if not resource_dir.is_dir():
                continue
            for path in sorted(resource_dir.rglob("*")):
                if path.is_file():
                    resources.append(path.relative_to(skill_dir).as_posix())
        return tuple(resources)

    def read_resource(self, workspace: Path, skill_name: str, resource_path: str) -> str:
        """Read a text resource from a folder-based skill.

        The path must stay inside one of RESOURCE_DIRS to avoid exposing
        arbitrary workspace files through the resource API.
        """
        skill_dir = self._resolve_skill_dir(workspace, skill_name)
        if not skill_dir:
            raise FileNotFoundError(f"Skill not found: {skill_name}")

        normalized = resource_path.strip().lstrip("/")
        top_level = normalized.split("/", 1)[0]
        if top_level not in RESOURCE_DIRS:
            raise PermissionError(f"Skill resource must be under one of: {', '.join(sorted(RESOURCE_DIRS))}")

        target = (skill_dir / normalized).resolve()
        skill_root = skill_dir.resolve()
        try:
            target.relative_to(skill_root)
        except ValueError as exc:
            raise PermissionError("Skill resource path escapes skill directory") from exc
        if not target.is_file():
            raise FileNotFoundError(normalized)
        return target.read_text(encoding="utf-8", errors="replace")

    def _resolve_skill_dir(self, workspace: Path, skill_name: str) -> Path | None:
        skills_dir = workspace / "skills"
        if not skills_dir.exists():
            return None

        normalized = self._normalize(skill_name)
        if normalized in RETIRED_BUILTIN_SKILL_FOLDERS:
            return None
        for parsed in self.load_from_workspace(workspace):
            if self._normalize(parsed.metadata.name) == normalized:
                parent = parsed.file_path.parent
                if parent.parent == skills_dir:
                    return parent
                return None
        direct = skills_dir / normalized
        if direct.is_dir() and any((direct / filename).exists() for filename in ("SKILL.md", "skill.md")):
            return direct
        return None

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("_", "-").replace(" ", "-")
