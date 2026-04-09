"""Tests for SkillRegistry catalog budget control."""

from __future__ import annotations

from pathlib import Path

from app.skills.registry import SkillRegistry
from app.skills.types import ParsedSkill, SkillMetadata


def _make_skill(name: str, description: str, *, is_system: bool = False) -> ParsedSkill:
    return ParsedSkill(
        metadata=SkillMetadata(name=name, description=description, is_system=is_system),
        body="# " + name,
        file_path=Path("skills/" + name + ".md"),
        relative_path=name + ".md",
    )


class TestCatalogBudgetControl:
    """render_catalog respects budget_chars and degrades gracefully."""

    def test_small_catalog_renders_fully(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("web_search", "Search the web"))
        reg.register(_make_skill("file_ops", "File operations"))
        result = reg.render_catalog(budget_chars=4000)
        assert "web_search" in result
        assert "Search the web" in result
        assert "file_ops" in result

    def test_large_catalog_truncates_descriptions(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "System skill", is_system=True))
        for i in range(20):
            reg.register(_make_skill("user_skill_" + str(i), "A" * 200))
        result = reg.render_catalog(budget_chars=2000)
        # System skill description preserved
        assert "System skill" in result
        # User skills present but descriptions truncated
        assert "user_skill_0" in result
        assert "A" * 200 not in result

    def test_system_skills_never_truncated(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "Important system description that must survive", is_system=True))
        for i in range(30):
            reg.register(_make_skill("u" + str(i), "X" * 300))
        result = reg.render_catalog(budget_chars=1500)
        assert "Important system description that must survive" in result

    def test_extreme_budget_shows_names_only(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "System", is_system=True))
        for i in range(50):
            reg.register(_make_skill("s" + str(i), "Y" * 500))
        result = reg.render_catalog(budget_chars=800)
        # Names should still be present
        assert "s0" in result
        # But full descriptions should not
        assert "Y" * 500 not in result

    def test_empty_registry(self) -> None:
        reg = SkillRegistry()
        assert reg.render_catalog() == ""

    def test_default_budget_is_generous(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("a", "Short desc"))
        result = reg.render_catalog()
        assert "Short desc" in result
