"""A — the Plan Mode guidance section teaches the agent to SUGGEST planning, never
to enter Plan Mode itself, and is shown only on live interactive surfaces."""

from __future__ import annotations

from app.runtime.prompt_sections.plan_mode_guidance import (
    build_plan_mode_guidance_section,
    should_show_plan_mode_guidance,
)


def test_guidance_is_suggest_only_never_auto_enters():
    text = build_plan_mode_guidance_section()
    # The defining contract: suggest, never enter; the user decides.
    assert "never enter" in text.lower()
    assert "user's explicit choice" in text.lower()
    assert "SUGGEST" in text


def test_guidance_has_both_should_and_should_not_lists():
    text = build_plan_mode_guidance_section()
    assert "Suggest planning first when" in text
    assert "Do NOT suggest" in text


def test_guidance_gated_to_interactive_surfaces():
    # Live interactive surfaces — a user is present to act on the suggestion.
    assert should_show_plan_mode_guidance(source="web") is True
    assert should_show_plan_mode_guidance(source="web_chat") is True
    assert should_show_plan_mode_guidance(channel="feishu") is True
    # Unattended runs — no user to suggest to.
    assert should_show_plan_mode_guidance(source="trigger") is False
    assert should_show_plan_mode_guidance(source="heartbeat") is False
    assert should_show_plan_mode_guidance(source="agent") is False
    assert should_show_plan_mode_guidance() is False


def test_dynamic_suffix_includes_guidance_for_web_but_not_trigger():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    web = build_dynamic_prompt_suffix(source="web")
    trigger = build_dynamic_prompt_suffix(source="trigger")

    assert "When to Suggest Planning First" in web
    assert "When to Suggest Planning First" not in trigger
    # A trigger run gets the autonomous section instead — never the suggest-to-user one.
    assert "Autonomous Work" in trigger
