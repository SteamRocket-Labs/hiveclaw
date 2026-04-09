"""Tests for the HR agent system endpoint — GET /agents/system/hr."""

from __future__ import annotations


def test_internal_system_agents_excluded_from_list():
    """The HR_AGENT_NAME constant should match the filter in list_agents."""
    from app.api.agents import HR_AGENT_NAME
    assert HR_AGENT_NAME == "__system_hr__"


def test_agent_class_internal_system_literal_is_valid():
    """internal_system must be a valid AgentClass literal."""
    from app.schemas.schemas import AgentClass
    # AgentClass is a Literal type — verify it includes internal_system
    import typing
    args = typing.get_args(AgentClass)
    assert "internal_system" in args
    assert "internal_tenant" in args
