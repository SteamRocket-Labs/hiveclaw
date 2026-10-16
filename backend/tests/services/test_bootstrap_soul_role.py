"""Only the exact platform bootstrap snapshot follows canonical role changes."""

from app.runtime.prompt_sections.identity import build_identity_section
from app.services.bootstrap_soul import build_bootstrap_soul, project_bootstrap_soul_role


def test_changed_or_cleared_role_replaces_the_bootstrap_copy_without_mutating_its_source():
    source = build_bootstrap_soul("研究员", "Old assigned role\nwith multiple lines")
    original = source
    current = "Compare original evidence and cite sources."
    prompt = build_identity_section("研究员", current, soul_text=source)
    assert current in prompt and "Old assigned role" not in prompt
    assert "Diligent and detail-oriented" in prompt
    assert "Sensitive operations require creator approval" in prompt
    title_stripped = source.partition("\n\n")[2]
    assembled = build_identity_section("研究员", current, soul_text=title_stripped)
    assert current in assembled and "Old assigned role" not in assembled
    assert source == original
    assert "Old assigned role" in build_identity_section("研究员", soul_text=source)
    cleared = build_identity_section("研究员", "", soul_text=source)
    assert "Old assigned role" not in cleared


def test_custom_personality_and_frozen_charters_remain_exactly_available():
    custom = build_bootstrap_soul("Researcher", "Old mission") + "\n## Company Charter\nNever disclose private documents.\n"
    assert project_bootstrap_soul_role(custom, "New assignment") == custom
    prompt = build_identity_section("Researcher", "New assignment", soul_text=custom)
    assert custom in prompt and "### Role\nNew assignment" in prompt
    edited = build_bootstrap_soul("Researcher", "Old mission").replace("Diligent and detail-oriented", "Owner-authored working style")
    assert project_bootstrap_soul_role(edited, "New assignment") == edited
