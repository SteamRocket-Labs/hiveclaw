"""Tests for the global tool-secret scrub planner (functional core)."""

from __future__ import annotations

from app.scripts.scrub_global_tool_secrets import ScrubAction, plan_global_secret_scrub
from app.services.tool_config_service import MASKED_SECRET_SENTINEL

_SCHEMA = {"fields": [{"key": "exa_api_key", "type": "password"}, {"key": "search_engine", "type": "select"}]}


def test_plan_flags_nonempty_secret() -> None:
    rows = [("id1", "web_search", {"exa_api_key": "exa-REAL", "search_engine": "auto"}, _SCHEMA)]
    actions = plan_global_secret_scrub(rows)
    assert actions == [ScrubAction("id1", "web_search", "exa_api_key")]


def test_plan_ignores_empty_secret() -> None:
    rows = [("id1", "web_search", {"exa_api_key": "", "search_engine": "auto"}, _SCHEMA)]
    assert plan_global_secret_scrub(rows) == []


def test_plan_ignores_nonsecret_field() -> None:
    rows = [("id1", "web_search", {"search_engine": "tavily"}, _SCHEMA)]
    assert plan_global_secret_scrub(rows) == []


def test_plan_ignores_already_masked_value() -> None:
    """An already-scrubbed/masked row must not be flagged again (idempotent)."""
    rows = [("id1", "web_search", {"exa_api_key": MASKED_SECRET_SENTINEL}, _SCHEMA)]
    assert plan_global_secret_scrub(rows) == []


def test_plan_handles_missing_schema_and_config() -> None:
    rows = [("id1", "t", None, None), ("id2", "u", {"exa_api_key": "k"}, None)]
    assert plan_global_secret_scrub(rows) == []  # no schema => no password fields => nothing to scrub


def test_plan_flags_multiple_secrets_across_tools() -> None:
    multi = {"fields": [{"key": "exa_api_key", "type": "password"}, {"key": "tavily_api_key", "type": "password"}]}
    rows = [
        ("id1", "web_search", {"exa_api_key": "a", "tavily_api_key": "b"}, multi),
        ("id2", "firecrawl", {"api_key": "c"}, {"fields": [{"key": "api_key", "type": "password"}]}),
    ]
    actions = plan_global_secret_scrub(rows)
    assert {(a.tool_name, a.secret_key) for a in actions} == {
        ("web_search", "exa_api_key"),
        ("web_search", "tavily_api_key"),
        ("firecrawl", "api_key"),
    }
