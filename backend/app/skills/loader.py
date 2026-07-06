"""Discover and load workspace skills from flat, folder, or scoped layouts."""

from __future__ import annotations

import os
from pathlib import Path

from .parser import SkillParser
from .retired import RETIRED_BUILTIN_SKILL_FOLDERS
from .types import ParsedSkill

RESOURCE_DIRS = frozenset({"references", "scripts", "templates", "assets", "evals"})
_SKILLS_DIR_NAME = "skills"
_DISCOVERY_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        ".cache",
        ".next",
    }
)


class WorkspaceSkillLoader:
    """Load skills from an agent workspace."""

    def __init__(self, parser: SkillParser | None = None) -> None:
        self.parser = parser or SkillParser()

    def load_from_workspace(self, workspace: Path) -> list[ParsedSkill]:
        skills: list[ParsedSkill] = []
        workspace_root = workspace.resolve()
        for skills_dir in self._discover_skill_dirs(workspace_root):
            skills.extend(self._load_from_skills_dir(workspace_root, skills_dir))

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
        workspace_root = workspace.resolve()

        normalized = self._normalize(skill_name)
        if normalized in RETIRED_BUILTIN_SKILL_FOLDERS:
            return None
        for parsed in self.load_from_workspace(workspace):
            if self._normalize(parsed.metadata.name) == normalized:
                parent = parsed.file_path.parent
                if parent.parent.name == _SKILLS_DIR_NAME and self._is_inside_workspace(parent, workspace_root):
                    return parent
                return None
        for skills_dir in self._discover_skill_dirs(workspace_root):
            direct = skills_dir / normalized
            if (
                direct.is_dir()
                and self._is_inside_workspace(direct.resolve(), workspace_root)
                and any(
                    (direct / filename).exists()
                    and self._is_inside_workspace((direct / filename).resolve(), workspace_root)
                    for filename in ("SKILL.md", "skill.md")
                )
            ):
                return direct
        return None

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("_", "-").replace(" ", "-")

    def _discover_skill_dirs(self, workspace_root: Path) -> tuple[Path, ...]:
        if not workspace_root.exists():
            return ()

        discovered: list[Path] = []
        seen: set[Path] = set()

        def add_candidate(path: Path) -> None:
            if not path.is_dir() or path.name != _SKILLS_DIR_NAME:
                return
            resolved = path.resolve()
            if resolved in seen or not self._is_inside_workspace(resolved, workspace_root):
                return
            seen.add(resolved)
            discovered.append(resolved)

        add_candidate(workspace_root / _SKILLS_DIR_NAME)
        for root, dirs, _files in os.walk(workspace_root, followlinks=False):
            root_path = Path(root)
            if root_path.resolve() in seen:
                dirs[:] = []
                continue
            pruned_dirs: list[str] = []
            for dirname in sorted(dirs):
                if dirname in _DISCOVERY_PRUNE_DIRS:
                    continue
                candidate = root_path / dirname
                if dirname == _SKILLS_DIR_NAME:
                    add_candidate(candidate)
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

        return tuple(discovered)

    def _load_from_skills_dir(self, workspace_root: Path, skills_dir: Path) -> list[ParsedSkill]:
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
                    if skill_file.exists() and self._is_inside_workspace(skill_file.resolve(), workspace_root):
                        skills.append(
                            self.parser.parse_file(
                                skill_file,
                                relative_path=skill_file.resolve().relative_to(workspace_root).as_posix(),
                                default_name=entry.name,
                            )
                        )
                        break
            elif entry.is_file() and entry.suffix == ".md" and self._is_inside_workspace(entry.resolve(), workspace_root):
                skills.append(
                    self.parser.parse_file(
                        entry,
                        relative_path=entry.resolve().relative_to(workspace_root).as_posix(),
                        default_name=entry.stem,
                    )
                )
        return skills

    @staticmethod
    def _is_inside_workspace(path: Path, workspace_root: Path) -> bool:
        try:
            path.relative_to(workspace_root)
        except ValueError:
            return False
        return True
