"""Empty profiles retain the static route; resident prompts retain their bytes."""

from pathlib import Path

from app.runtime.prompt_builder import build_dynamic_prompt_suffix
from app.runtime.prompt_sections.memory import build_memory_section

SNAPSHOT = "[Profile Plane — Resident Memory]\n\n### self (self/self.md)\n# Self\n\nReviewed prior work."


def test_nonempty_memory_section_changes_only_background_framing_from_add842d8():
    expected = Path(__file__).with_name("fixtures").joinpath("a2mf_memory_add842d8.txt").read_bytes()
    expected = expected.replace(b"### Current Memory State", b"### Background Memory").replace(
        b"- Treat memory as a pointer to evidence, not as current workspace truth.",
        b"- Use remembered preferences and past work as background when they are relevant to the current request.",
    )
    assert build_memory_section(SNAPSHOT).encode("utf-8") == expected
    suffix = build_dynamic_prompt_suffix(memory_snapshot=SNAPSHOT)
    assert suffix.startswith(expected.decode("utf-8") + "\n\n")
    assert suffix.count("### Using Memory Tools") == 1


def test_empty_memory_has_a_static_route_without_a_resident_snapshot():
    ledger = []
    suffix = build_dynamic_prompt_suffix(context_section_ledger=ledger)
    assert "### Using Memory Tools" in suffix
    assert "under `memory/knowledge/`" in suffix and "under `memory/t2/sessions/`" in suffix
    assert "List the directory first, then `read_file(path=...)`" in suffix
    # Grok reviews 1 and 2: no empty-folder claim is true here. An empty
    # memory/knowledge/ says nothing about sessions, and an empty
    # memory/t2/sessions/ says nothing about readable T0 source.md files or
    # search_memory(scope=sessions), so the route makes no such claim.
    assert "nothing to recall" not in suffix
    assert "no past session to read" not in suffix
    assert "### Background Memory" not in suffix and "(no memory loaded)" not in suffix
    assert all(entry["name"] != "memory_snapshot" for entry in ledger)
    route = next(entry for entry in ledger if entry["name"] == "memory_tools_route")
    assert route["platform_constant"] is True
