from __future__ import annotations


def test_agent_list_queries_have_covering_indexes():
    from app.models.agent import Agent, AgentPermission

    agent_indexes = {index.name: tuple(column.name for column in index.columns) for index in Agent.__table__.indexes}
    permission_indexes = {
        index.name: tuple(column.name for column in index.columns) for index in AgentPermission.__table__.indexes
    }

    assert agent_indexes["ix_agents_tenant_active_created_at"] == (
        "tenant_id",
        "deleted_at",
        "deactivated_at",
        "agent_class",
        "created_at",
    )
    assert agent_indexes["ix_agents_creator_tenant_active_created_at"] == (
        "creator_id",
        "tenant_id",
        "deleted_at",
        "deactivated_at",
        "created_at",
    )
    assert permission_indexes["ix_agent_permissions_scope_lookup"] == (
        "scope_type",
        "scope_id",
        "agent_id",
    )
