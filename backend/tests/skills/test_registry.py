"""Tests for SkillRegistry catalog budget control."""

from __future__ import annotations

from pathlib import Path

from app.skills.registry import SkillRegistry
from app.skills.types import ParsedSkill, SkillMetadata


def _make_skill(
    name: str,
    description: str,
    *,
    is_system: bool = False,
    disable_model_invocation: bool = False,
    user_invocable: bool = True,
    hidden: bool = False,
    paths: tuple[str, ...] = (),
) -> ParsedSkill:
    return ParsedSkill(
        metadata=SkillMetadata(
            name=name,
            description=description,
            is_system=is_system,
            disable_model_invocation=disable_model_invocation,
            user_invocable=user_invocable,
            hidden=hidden,
            paths=paths,
        ),
        body="# " + name,
        file_path=Path("skills/" + name + ".md"),
        relative_path=name + ".md",
    )


class TestCatalogVisibility:
    """Catalog assembly preserves all model-visible activation evidence."""

    def test_small_catalog_renders_fully(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("web_search", "Search the web"))
        reg.register(_make_skill("file_ops", "File operations"))
        result = reg.render_catalog(budget_chars=4000)
        assert "web_search" in result
        assert "Search the web" in result
        assert "file_ops" in result

    def test_large_catalog_preserves_descriptions_past_advisory_budget(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "System skill", is_system=True))
        for i in range(20):
            reg.register(_make_skill("user_skill_" + str(i), "A" * 200))
        result = reg.render_catalog(budget_chars=2000)
        assert "System skill" in result
        assert "user_skill_0" in result
        assert "A" * 200 in result

    def test_system_and_user_skills_are_both_complete(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "Important system description that must survive", is_system=True))
        for i in range(30):
            reg.register(_make_skill("u" + str(i), "X" * 300))
        result = reg.render_catalog(budget_chars=1500)
        assert "Important system description that must survive" in result
        assert "X" * 300 in result

    def test_small_advisory_budget_does_not_reduce_descriptions_to_names_only(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("core", "System", is_system=True))
        for i in range(50):
            reg.register(_make_skill("s" + str(i), "Y" * 500))
        result = reg.render_catalog(budget_chars=800)
        assert "s0" in result
        assert "Y" * 500 in result

    def test_empty_registry(self) -> None:
        reg = SkillRegistry()
        assert reg.render_catalog() == ""

    def test_default_budget_is_generous(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("a", "Short desc"))
        result = reg.render_catalog()
        assert "Short desc" in result

    def test_catalog_names_workflow_and_subagent_resource_dirs_with_governed_runtime_boundary(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("automation", "Automation guidance"))

        result = reg.render_catalog()

        assert "`workflows/`" in result
        assert "`subagents/`" in result
        assert "preview_workflow" in result
        assert "start_workflow" in result
        assert "spawn_subagent" in result
        assert "delegate_to_agent" in result

    def test_skill_access_catalog_filters_hidden_and_model_disabled_skills(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("visible", "Visible to the model"))
        reg.register(_make_skill("disabled", "Do not list", disable_model_invocation=True))
        reg.register(_make_skill("hidden", "Also do not list", hidden=True))

        result = reg.render_catalog()

        assert "visible" in result
        assert "disabled" not in result
        assert "hidden" not in result

    def test_catalog_preserves_each_complete_description(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("verbose", "B" * 400))

        result = reg.render_catalog(budget_chars=4000)

        assert "B" * 400 in result

    def test_catalog_preserves_decisive_description_tail_past_advisory_budget(self) -> None:
        reg = SkillRegistry()
        decisive_tail = "DECISIVE_SKILL_ACTIVATION_TAIL"
        description = ("Complete activation evidence. " * 80) + decisive_tail
        reg.register(_make_skill("complete-skill", description))

        result = reg.render_catalog(budget_chars=300)

        assert description in result
        assert decisive_tail in result

    def test_skills_for_paths_matches_declared_path_globs(self) -> None:
        reg = SkillRegistry()
        reg.register(_make_skill("python", "Python work", paths=("backend/**/*.py",)))
        reg.register(_make_skill("reports", "Report work", paths=("workspace/reports/",)))
        reg.register(_make_skill("web", "Web work", paths=("frontend/**/*.tsx",)))

        matches = reg.skills_for_paths(["backend/app/main.py", "workspace/reports/q1.md"])

        assert [skill.metadata.name for skill in matches] == ["python", "reports"]
