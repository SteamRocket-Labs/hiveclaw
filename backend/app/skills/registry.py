"""Skill registry and catalog renderer."""

from __future__ import annotations

from collections import OrderedDict

from .types import ParsedSkill


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

    def render_catalog(self, *, budget_chars: int = 8000) -> str:
        """Render skill catalog with budget-aware truncation.

        Three degradation levels (aligned with Claude Code's skill listing strategy):
        1. Full descriptions (if within budget)
        2. Truncated descriptions (system skills preserved, others truncated)
        3. Names-only for non-system skills (extreme budget pressure)
        """
        if not self._skills:
            return ""

        header = (
            "You have the following skills available. "
            "Each skill defines specific instructions for a task domain."
        )
        footer = (
            "\nWhen a user request matches a skill, FIRST call `load_skill` "
            "with the Skill name above to load the full instructions.\n"
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
            f"| {s.metadata.name} | {s.metadata.description} | {s.relative_path} |"
            for s in self._skills.values()
        ]
        if sum(len(r) + 1 for r in full_rows) <= row_budget:
            return header + table_header + "\n".join(full_rows) + footer

        # Level 2: truncate non-system skill descriptions
        system_skills = [s for s in self._skills.values() if s.metadata.is_system]
        user_skills = [s for s in self._skills.values() if not s.metadata.is_system]

        system_rows = [
            f"| {s.metadata.name} | {s.metadata.description} | {s.relative_path} |"
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
