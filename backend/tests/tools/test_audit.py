"""Tests for tool coverage audit."""

from __future__ import annotations

from app.tools.audit import audit_tool_coverage


def test_coverage_report_has_no_orphans():
    """Every registered tool must have at least one discovery path.

    This is a regression gate: any new tool that lacks a pack, a SKILL.md
    declared_tools entry, AND a prompt section mention will fail this test.
    The fix is never to special-case this assertion — always add the tool to
    the appropriate pack / skill / prompt section so the LLM can discover it.
    """
    report = audit_tool_coverage()
    assert report.total_tools > 0, "expected some tools to be registered"
    assert report.orphans == frozenset(), (
        f"Found {len(report.orphans)} functional orphan tool(s): {sorted(report.orphans)}. "
        f"Add each to backend/app/tools/packs.py OR declare in a SKILL.md "
        f"tools: list OR mention by name in a prompt section."
    )


def test_coverage_as_dict_schema():
    report = audit_tool_coverage()
    d = report.as_dict()
    assert d["total_tools"] == report.total_tools
    assert set(d.keys()) == {
        "total_tools",
        "orphans",
        "unregistered_system_skill_dirs",
        "covered_by_core",
        "covered_by_pack_only",
        "covered_by_skill_only",
        "covered_by_prompt_only",
    }


def test_every_system_skill_template_is_registered_in_builtin_skills():
    """Every SKILL.md under templates/system_skills/ must be declared in
    BUILTIN_SKILLS, otherwise skill_seeder will never push it to DB and
    agents will never see it in their skill catalog (silent orphan skill).
    """
    report = audit_tool_coverage()
    assert report.unregistered_system_skill_dirs == frozenset(), (
        f"Found {len(report.unregistered_system_skill_dirs)} system_skills folder(s) "
        f"missing from BUILTIN_SKILLS: {sorted(report.unregistered_system_skill_dirs)}. "
        f"Add an entry in backend/app/services/skill_seeder.py BUILTIN_SKILLS and "
        f"extend the seed_skills() system_skills whitelist."
    )
