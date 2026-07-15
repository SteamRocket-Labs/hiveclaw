from __future__ import annotations


def test_import_all_models_registers_personal_knowledge_tables() -> None:
    from app.database import Base
    from app.models import import_all_models

    import_all_models()

    expected_tables = {
        "knowledge_documents",
        "knowledge_segments",
        "knowledge_entities",
        "knowledge_assertions",
        "knowledge_links",
        "knowledge_index_jobs",
        "knowledge_grants",
    }

    assert expected_tables <= set(Base.metadata.tables)


def test_knowledge_models_are_scope_and_tenant_scoped() -> None:
    from app.models.knowledge import (
        KnowledgeAssertion,
        KnowledgeDocument,
        KnowledgeEntity,
        KnowledgeGrant,
        KnowledgeIndexJob,
        KnowledgeLink,
        KnowledgeSegment,
    )

    for model in (
        KnowledgeDocument,
        KnowledgeSegment,
        KnowledgeEntity,
        KnowledgeAssertion,
        KnowledgeLink,
        KnowledgeIndexJob,
        KnowledgeGrant,
    ):
        table = model.__table__
        assert table.columns["tenant_id"].nullable is False
        assert table.columns["scope_type"].nullable is False
        assert table.columns["scope_id"].nullable is False
        assert any(fk.target_fullname == "tenants.id" for fk in table.columns["tenant_id"].foreign_keys)


def test_knowledge_document_identity_is_one_truth_per_person_scope_source_hash() -> None:
    from app.models.knowledge import KnowledgeDocument

    unique_constraints = [
        constraint
        for constraint in KnowledgeDocument.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]

    assert any(
        {column.name for column in constraint.columns} == {"tenant_id", "scope_type", "scope_id", "source_sha256"}
        for constraint in unique_constraints
    )


def test_knowledge_grants_use_resource_and_grantee_scale_for_a2a_permissions() -> None:
    from app.models.knowledge import KnowledgeGrant

    table = KnowledgeGrant.__table__

    for column_name in (
        "resource_type",
        "resource_id",
        "grantee_type",
        "grantee_id",
        "permission",
        "sensitivity_ceiling",
        "binding_key",
    ):
        assert table.columns[column_name].nullable is False

    unique_constraints = [
        constraint for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert any(
        {column.name for column in constraint.columns}
        == {
            "tenant_id",
            "scope_type",
            "scope_id",
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            "permission",
            "binding_key",
        }
        for constraint in unique_constraints
    )
    check_names = {
        constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert {
        "ck_knowledge_grant_sensitivity_ceiling",
        "ck_knowledge_grant_agent_binding",
        "ck_knowledge_grant_resource_binding",
        "ck_knowledge_grant_revoke_actor",
    } <= check_names
