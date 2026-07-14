"""Skill registry and catalog renderer."""

from __future__ import annotations

import fnmatch
from collections import OrderedDict
from pathlib import PurePosixPath

from .types import ParsedSkill


def _model_catalog_visible(skill: ParsedSkill) -> bool:
    return not skill.metadata.hidden and not skill.metadata.disable_model_invocation


def _normalize_match_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("/")


def _skill_path_matches(path: str, pattern: str) -> bool:
    normalized_path = _normalize_match_path(path)
    normalized_pattern = _normalize_match_path(pattern)
    if not normalized_path or not normalized_pattern:
        return False
    has_glob = any(token in normalized_pattern for token in ("*", "?", "["))
    if not has_glob:
        prefix = normalized_pattern.rstrip("/")
        return normalized_path == prefix or normalized_path.startswith(prefix + "/")
    return PurePosixPath(normalized_path).match(normalized_pattern) or fnmatch.fnmatch(
        normalized_path,
        normalized_pattern,
    )


class SkillRegistry:
    """Deduplicated registry keyed by display name."""

    def __init__(self) -> None:
        self._skills: "OrderedDict[str, ParsedSkill]" = OrderedDict()

    def register(self, skill: ParsedSkill) -> None:
        self._skills.setdefault(skill.metadata.name, skill)

    def register_many(self, skills: list[ParsedSkill]) -> None:
        for skill in skills:
            self.register(skill)

    def names(self) -> list[str]:
        return list(self._skills.keys())

    def resolve(self, name: str) -> ParsedSkill:
        if name in self._skills:
            return self._skills[name]

        normalized = self._normalize(name)
        for key, skill in self._skills.items():
            if self._normalize(key) == normalized:
                return skill

        raise KeyError(name)

    def load_body(self, name: str) -> str:
        return self.resolve(name).body

    def load_body_with_dependencies(self, name: str) -> str:
        """Load required skill bodies before the requested skill body.

        Missing dependencies are included as explicit warnings so callers can
        keep running while preserving the tolerant skill-loading contract.
        """
        parts: list[str] = []
        visited: set[str] = set()

        def append_skill(skill_name: str) -> None:
            normalized_name = self._normalize(skill_name)
            if normalized_name in visited:
                return
            visited.add(normalized_name)

            try:
                skill = self.resolve(skill_name)
            except KeyError:
                parts.append(f"<!-- Missing required skill: {skill_name} -->")
                return

            for dependency in skill.metadata.requires_skills:
                append_skill(dependency)
            parts.append(skill.body)

        append_skill(name)
        return "\n\n".join(part for part in parts if part)

    def render_catalog(self, *, budget_chars: int = 8000) -> str:
        """Render every model-visible skill with its complete description.

        ``budget_chars`` is retained as an advisory compatibility argument.
        The final provider prompt gate owns physical capacity; catalog assembly
        never decides which activation evidence the model may see.
        """
        del budget_chars
        visible_skills = [skill for skill in self._skills.values() if _model_catalog_visible(skill)]
        if not visible_skills:
            return ""

        header = (
            "You have the following skills available. "
            "Each skill is a progressive-disclosure capability capsule for a task domain."
        )
        footer = (
            "\nA skill can package references, templates, scripts, workflow definitions, and subagent definitions. "
            "Loading a skill adds context and guidance only. Executable components still run through their governed "
            "runtime: workflows via `preview_workflow`/`start_workflow`, subagents via "
            "`spawn_subagent`/`delegate_to_agent`, and scripts through approved sandbox/code execution.\n"
            "When a user request matches a skill, FIRST call `load_skill` "
            "with the Skill name above to load its instructions and component guidance.\n"
            "Load only the skill that matches the current task.\n"
            "Do NOT speculatively load multiple skills before deciding which one is relevant.\n"
            "Do NOT guess what the skill contains — always read it first.\n"
            "Folder-based skills may contain auxiliary files under `references/`, `templates/`, `scripts/`, "
            "`assets/`, `evals/`, `workflows/`, and `subagents/`. "
            "Use `read_file` on the skill folder when needed; reading component files does not execute them.\n"
            "If no skill matches the current task, use your tools directly without loading a skill."
        )
        table_header = "\n| Skill | Description | File |\n|-------|-------------|------|\n"
        rows = [
            f"| {skill.metadata.name} | {skill.metadata.description or ''} | {skill.relative_path} |"
            for skill in visible_skills
        ]
        return header + table_header + "\n".join(rows) + footer

    def skills_for_paths(self, paths: list[str] | tuple[str, ...]) -> list[ParsedSkill]:
        """Return model-visible skills whose declared path globs match any path."""
        normalized_paths = [_normalize_match_path(path) for path in paths if _normalize_match_path(path)]
        if not normalized_paths:
            return []
        matches: list[ParsedSkill] = []
        for skill in self._skills.values():
            if not _model_catalog_visible(skill):
                continue
            patterns = tuple(skill.metadata.paths or ())
            if not patterns:
                continue
            if any(_skill_path_matches(path, pattern) for path in normalized_paths for pattern in patterns):
                matches.append(skill)
        return matches

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("_", "-").replace(" ", "-")
