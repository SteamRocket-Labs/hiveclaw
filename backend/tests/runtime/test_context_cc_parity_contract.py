from __future__ import annotations

from pathlib import Path


def test_full_lifecycle_context_map_is_documented_in_runtime_instructions() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    text = "\n".join(
        [
            (repo_root / "AGENTS.md").read_text(encoding="utf-8"),
            (repo_root / "CLAUDE.md").read_text(encoding="utf-8"),
        ]
    )

    assert "Full Lifecycle Parity" in text
    assert "CC `CLAUDE.md` / project instructions" in text
    assert "Hive `soul.md`" in text
    assert "Skill, Sub-agent, Workflow, and Hooks" in text
    assert "Memory / Iter self-evolution system" in text


def test_skill_catalog_stays_out_of_frozen_prefix() -> None:
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    prefix = build_frozen_prompt_prefix(
        agent_context="## Identity\nAgent soul",
        context_window_tokens=128_000,
    )

    assert "Agent soul" in prefix
    assert "Tool Governance" in prefix
    assert "Available Skills" not in prefix
    assert "## Skill Catalog" not in prefix


def test_runtime_prompt_sections_do_not_privilege_vendor_identity() -> None:
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section
    from app.runtime.prompt_sections.system import build_system_section
    from app.runtime.prompt_sections.tools import build_tools_section

    runtime_text = "\n\n".join(
        [
            build_system_section(),
            build_tools_section(),
            build_executing_actions_section(),
        ]
    )

    forbidden_identity_phrases = (
        "You are Claude",
        "You are ChatGPT",
        "Claude should",
        "OpenAI should",
        "Anthropic-only",
        "Claude-only",
    )
    assert not any(phrase in runtime_text for phrase in forbidden_identity_phrases)
