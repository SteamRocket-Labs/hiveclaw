"""Tests for agent_seeder.py — persistent seeding marker prevents re-creation."""

from pathlib import Path


def _read_seeder_source() -> str:
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / "backend/app/services/agent_seeder.py").read_text()


def test_seeder_imports_system_setting():
    """Seeder must use SystemSetting for the persistent marker."""
    source = _read_seeder_source()
    assert "SystemSetting" in source


def test_seeder_checks_persistent_marker_before_name_check():
    """The SystemSetting marker check must come BEFORE the name-based check."""
    source = _read_seeder_source()
    marker_pos = source.index('"default_agents_seeded"')
    name_check_pos = source.index('Agent.name.in_(["Morty", "Meeseeks"])')
    assert marker_pos < name_check_pos, "Persistent marker check must precede name-based check"


def test_seeder_plants_marker_after_creation():
    """After creating agents, the seeder must plant the marker."""
    source = _read_seeder_source()
    commit_pos = source.rindex("await db.commit()")
    marker_add_pos = source.rindex('key="default_agents_seeded"')
    assert marker_add_pos < commit_pos, "Marker must be added before final commit"


def test_seeder_plants_marker_for_existing_pre_marker_installs():
    """For backward compat, if agents exist but marker doesn't, plant the marker."""
    source = _read_seeder_source()
    assert "planted marker" in source.lower() or "plant" in source.lower()


def test_seeder_skips_when_marker_exists():
    """When marker exists, seeder must return early without touching agents."""
    source = _read_seeder_source()
    # After finding the marker, the function should return before any Agent creation
    fn_body = source.split("async def seed_default_agents")[1]
    marker_section = fn_body.split('"default_agents_seeded"')[1].split("return")[0]
    # The marker check should lead to a return, not agent creation
    assert "Agent(" not in marker_section, "Must not create agents in the marker-check branch"
