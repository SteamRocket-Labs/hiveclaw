"""Skill registry and catalog renderer."""

from __future__ import annotations

from collections import OrderedDict

from .types import ParsedSkill

_CATALOG_DESCRIPTION_CHAR_LIMIT = 250


def _model_catalog_visible(skill: ParsedSkill) -> bool:
    return not skill.metadata.hidden and not skill.metadata.disable_model_invocation


def _catalog_description(skill: ParsedSkill, *, max_chars: int = _CATALOG_DESCRIPTION_CHAR_LIMIT) -> str:
    description = skill.metadata.description or ""
    if len(description) <= max_chars:
        return description
    return description[:max_chars].rstrip() + "..."


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
        """Render skill catalog with budget-aware truncation.

        Three degradation levels (aligned with Claude Code's skill listing strategy):
        1. Full descriptions (if within budget)
        2. Truncated descriptions (system skills preserved, others truncated)
        3. Names-only for non-system skills (extreme budget pressure)
        """
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
            "Folder-based skills may contain auxiliary files. "
            "Use `read_file` on the skill folder when needed.\n"
            "If no skill matches the current task, use your tools directly without loading a skill."
        )
        table_header = "\n| Skill | Description | File |\n|-------|-------------|------|\n"
        overhead = len(header) + len(footer) + len(table_header) + 10
        row_budget = budget_chars - overhead

        # Level 1: try full descriptions
        full_rows = [
            f"| {s.metadata.name} | {_catalog_description(s)} | {s.relative_path} |"
            for s in visible_skills
        ]
        if sum(len(r) + 1 for r in full_rows) <= row_budget:
            return header + table_header + "\n".join(full_rows) + footer

        # Level 2: truncate non-system skill descriptions
        system_skills = [s for s in visible_skills if s.metadata.is_system]
        user_skills = [s for s in visible_skills if not s.metadata.is_system]

        system_rows = [
            f"| {s.metadata.name} | {_catalog_description(s)} | {s.relative_path} |"
            for s in system_skills
        ]
        system_chars = sum(len(r) + 1 for r in system_rows)
        remaining = row_budget - system_chars
        max_desc = max(20, remaining // max(len(user_skills), 1) - 20) if user_skills else 0

        if max_desc >= 20:
            user_rows = []
            for s in user_skills:
                desc = s.metadata.description
                if len(desc) > max_desc:
                    desc = desc[:max_desc] + "..."
                user_rows.append(f"| {s.metadata.name} | {desc} | {s.relative_path} |")
            return header + table_header + "\n".join(system_rows + user_rows) + footer

        # Level 3: names-only for non-system
        user_rows = [f"| {s.metadata.name} | — | {s.relative_path} |" for s in user_skills]
        return header + table_header + "\n".join(system_rows + user_rows) + footer

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("_", "-").replace(" ", "-")
