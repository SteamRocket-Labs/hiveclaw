"""Unit tests for Tier 1-6 soft routing reminder."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_routing_state():
    from app.services.deep_research.routing_reminder import reset_session_state

    reset_session_state()
    yield
    reset_session_state()


def _kwargs(
    session_id="sess-A",
    available=("web_search", "deep_research_run"),
    intent=("Investor due diligence on RWA market.",),
):
    return {
        "session_id": session_id,
        "available_tool_names": available,
        "intent_hints": intent,
    }


def test_reminder_does_not_fire_below_threshold():
    """T1-6: under 3 web_search calls, no reminder appended."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    for _ in range(2):
        result = maybe_inject_routing_reminder("[results]", tool_name="web_search", **_kwargs())
        assert "routing-reminder" not in result


def test_reminder_fires_after_three_web_searches_with_intent_and_pack():
    """T1-6: 3rd web_search with deep-research intent + pack visibility triggers the soft nudge."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    for _ in range(2):
        maybe_inject_routing_reminder("[results]", tool_name="web_search", **_kwargs())

    third = maybe_inject_routing_reminder("[third results]", tool_name="web_search", **_kwargs())
    assert "routing-reminder" in third
    assert "deep_research_run" in third
    assert "[third results]" in third


def test_reminder_is_one_shot_per_session():
    """T1-6: once the reminder fires, subsequent web_search calls do not duplicate it."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    for _ in range(3):
        maybe_inject_routing_reminder("[results]", tool_name="web_search", **_kwargs())
    fourth = maybe_inject_routing_reminder("[fourth]", tool_name="web_search", **_kwargs())
    assert "routing-reminder" not in fourth


def test_reminder_skipped_when_deep_research_pack_not_visible():
    """T1-6: agent without deep_research_pack never sees the nudge — would be misleading."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    no_pack = ("web_search", "write_file")
    for _ in range(4):
        result = maybe_inject_routing_reminder(
            "[results]",
            tool_name="web_search",
            session_id="sess-B",
            available_tool_names=no_pack,
            intent_hints=("Investor due diligence on RWA market.",),
        )
        assert "routing-reminder" not in result


def test_reminder_skipped_without_deep_research_intent():
    """T1-6: without intent signal, do not nudge — agents legitimately do casual web_search."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    for _ in range(4):
        result = maybe_inject_routing_reminder(
            "[results]",
            tool_name="web_search",
            session_id="sess-C",
            available_tool_names=("web_search", "deep_research_run"),
            intent_hints=("look up the weather forecast",),
        )
        assert "routing-reminder" not in result


def test_reminder_skipped_when_deep_research_already_called():
    """T1-6: once deep_research_* fires this session, no nudge — the agent already routed correctly."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    maybe_inject_routing_reminder("[dr]", tool_name="deep_research_run", **_kwargs(session_id="sess-D"))
    for _ in range(4):
        result = maybe_inject_routing_reminder("[results]", tool_name="web_search", **_kwargs(session_id="sess-D"))
        assert "routing-reminder" not in result


def test_reminder_responds_to_chinese_intent():
    """T1-6: Chinese deep-research intent triggers the same soft reminder."""
    from app.services.deep_research.routing_reminder import maybe_inject_routing_reminder

    intent_zh = ("用户要求做一份 RWA 行业研究和尽职调查报告",)
    for _ in range(3):
        result = maybe_inject_routing_reminder(
            "[results]",
            tool_name="web_search",
            session_id="sess-E",
            available_tool_names=("web_search", "deep_research_run"),
            intent_hints=intent_zh,
        )
    assert "routing-reminder" in result
