"""Discover and load workspace skills from flat, folder, or scoped layouts."""

from __future__ import annotations

import os
from pathlib import Path

from app.services.skill_evolution_registry import get_skill_evolution_entry, is_skill_runtime_loadable

from .parser import SkillParser
from .retired import RETIRED_BUILTIN_SKILL_FOLDERS
from .types import ParsedSkill

RESOURCE_DIRS = frozenset({"references", "scripts", "templates", "assets", "evals", "workflows", "subagents"})
_SKILLS_DIR_NAME = "skills"
_SESSION_EXTENSIONS_DIR_NAME = "session_extensions"
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

    def load_from_workspace(self, workspace: Path, *, session_id: object | None = None, source_authorizer=None,
                            instructions: list[tuple[str, tuple[Path, ...]]] | None = None) -> list[ParsedSkill]:
        workspace_root = workspace.resolve()
        if instructions is None:
            instructions = self.discover_instructions(workspace_root, session_id=session_id)
        skills: list[ParsedSkill] = []
        for name, candidates in instructions:
            for path in candidates:
                resolved = path.resolve()
                if not resolved.is_relative_to(workspace_root):
                    continue
                if source_authorizer is not None and not source_authorizer(path):
                    continue
                relative = resolved.relative_to(workspace_root).as_posix()
                if is_skill_runtime_loadable(workspace_root, relative):
                    parsed = self._parse_file(path, relative_path=relative, default_name=name)
                    if parsed is not None:
                        skills.append(parsed)
                    break
        return skills

    def discover_instructions(self, workspace: Path, *, session_id: object | None = None) -> list[tuple[str, tuple[Path, ...]]]:
        """Enumerate instruction paths so the catalog can authorize only the files it reads."""
        workspace_root = workspace.resolve()
        instructions = []
        for skills_dir in self._discover_skill_dirs(workspace_root, session_id=session_id):
            instructions.extend(self._instruction_candidates(workspace_root, skills_dir))
        return instructions

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
            if not direct.is_dir() or not self._is_inside_workspace(direct.resolve(), workspace_root):
                continue
            instruction_file = next(
                (
                    direct / filename
                    for filename in ("SKILL.md", "skill.md")
                    if (direct / filename).exists()
                    and self._is_inside_workspace((direct / filename).resolve(), workspace_root)
                ),
                None,
            )
            if instruction_file is None:
                continue
            relative_instruction = instruction_file.resolve().relative_to(workspace_root).as_posix()
            if not is_skill_runtime_loadable(
                workspace_root,
                normalized,
            ) or not is_skill_runtime_loadable(workspace_root, relative_instruction):
                continue
            return direct
        return None

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("_", "-").replace(" ", "-")

    def _discover_skill_dirs(self, workspace_root: Path, *, session_id: object | None = None) -> tuple[Path, ...]:
        if not workspace_root.exists():
            return ()

        discovered: list[Path] = []
        seen_paths: set[str] = set()
        session_key = str(session_id).strip() if session_id is not None else ""

        def allowed_session_path(path: Path) -> bool:
            try:
                parts = path.resolve().relative_to(workspace_root).parts
            except ValueError:
                return False
            if _SESSION_EXTENSIONS_DIR_NAME not in parts:
                return True
            index = parts.index(_SESSION_EXTENSIONS_DIR_NAME)
            return bool(session_key and len(parts) > index + 1 and parts[index + 1] == session_key)

        def add_candidate(path: Path) -> None:
            if not path.is_dir() or path.name != _SKILLS_DIR_NAME:
                return
            resolved = path.resolve()
            if (
                os.fspath(resolved) in seen_paths
                or not self._is_inside_workspace(resolved, workspace_root)
                or not allowed_session_path(resolved)
            ):
                return
            seen_paths.add(os.fspath(resolved))
            discovered.append(resolved)

        def visit(directory: str, *, session_tree: bool = False, session_parent: bool = False) -> None:
            try:
                with os.scandir(directory) as children:
                    directories = sorted((entry for entry in children if entry.name not in _DISCOVERY_PRUNE_DIRS and (
                        entry.is_dir(follow_symlinks=False) or entry.name == _SKILLS_DIR_NAME
                    )), key=lambda entry: entry.name)
            except OSError:
                return
            # Keep the legacy order: register sibling skill directories before descending.
            nested = []
            for entry in directories:
                if entry.name == _SKILLS_DIR_NAME:
                    add_candidate(Path(entry.path))
                elif not session_parent or entry.name == session_key:
                    scoped = not session_tree and entry.name == _SESSION_EXTENSIONS_DIR_NAME
                    if not scoped or session_key:
                        nested.append((entry.path, session_tree or scoped, scoped))
            for path, session_tree, session_parent in nested:
                if path not in seen_paths:
                    visit(path, session_tree=session_tree, session_parent=session_parent)

        add_candidate(workspace_root / _SKILLS_DIR_NAME)
        visit(os.fspath(workspace_root))

        return tuple(discovered)

    def _parse_file(self, path: Path, *, relative_path: str, default_name: str) -> ParsedSkill | None:
        from app.services.workspace_resource_authority import WorkspaceAuthorityError

        try:
            return self.parser.parse_file(path, relative_path=relative_path, default_name=default_name)
        except (OSError, ValueError, WorkspaceAuthorityError):
            return None

    def _instruction_candidates(self, workspace_root: Path, skills_dir: Path):
        if not skills_dir.exists():
            return

        for entry in sorted(skills_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.name in RETIRED_BUILTIN_SKILL_FOLDERS:
                continue

            if entry.is_dir():
                candidates = [path for path in (entry / "SKILL.md", entry / "skill.md")
                    if path.exists() and self._is_inside_workspace(path.resolve(), workspace_root)]
                if len(candidates) == 2 and candidates[0].samefile(candidates[1]):
                    candidates = [next((path for path in candidates if get_skill_evolution_entry(
                        workspace_root, path.resolve().relative_to(workspace_root).as_posix()
                    ) is not None), candidates[0])]
                if candidates:
                    yield entry.name, tuple(candidates)
            elif (
                entry.is_file()
                and entry.suffix == ".md"
                and self._is_inside_workspace(entry.resolve(), workspace_root)
            ):
                yield entry.stem, (entry,)

    @staticmethod
    def _is_inside_workspace(path: Path, workspace_root: Path) -> bool:
        try:
            path.relative_to(workspace_root)
        except ValueError:
            return False
        return True
