from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint


def test_agent_model_references_are_tenant_bound_in_metadata() -> None:
    from app.models.agent import Agent, AgentTemplate
    from app.models.llm import LLMModel

    agent_foreign_keys = {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in Agent.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    model_unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in LLMModel.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    template_foreign_keys = {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in AgentTemplate.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert agent_foreign_keys["fk_agents_primary_model_tenant"] == (
        ("tenant_id", "primary_model_id"),
        ("llm_models.tenant_id", "llm_models.id"),
    )
    assert agent_foreign_keys["fk_agents_fallback_model_tenant"] == (
        ("tenant_id", "fallback_model_id"),
        ("llm_models.tenant_id", "llm_models.id"),
    )
    assert model_unique_constraints["uq_llm_models_tenant_id_id"] == ("tenant_id", "id")
    assert template_foreign_keys["fk_agent_templates_model_tenant"] == (
        ("tenant_id", "model_id"),
        ("llm_models.tenant_id", "llm_models.id"),
    )
