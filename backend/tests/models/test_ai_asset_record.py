from sqlalchemy import CheckConstraint, UniqueConstraint


def test_ai_asset_record_is_a_thin_tenant_control_index() -> None:
    from app.models.ai_asset import AIAssetRecord

    columns = set(AIAssetRecord.__table__.columns.keys())
    assert {
        "id",
        "tenant_id",
        "asset_type",
        "native_entity_id",
        "native_key",
        "native_locator_json",
        "display_name",
        "owner_type",
        "owner_id",
        "visibility_scope",
        "lifecycle_status",
        "active_revision_id",
        "content_hash",
        "source_type",
        "source_ref",
        "trust_state",
        "dependencies_json",
        "compatibility_json",
        "admission_state",
        "quarantine_reason",
        "usage_count",
        "last_used_at",
        "usage_evidence_json",
        "projection_status",
        "projection_error",
    } <= columns

    constraints = AIAssetRecord.__table__.constraints
    assert any(isinstance(item, UniqueConstraint) and item.name == "uq_ai_asset_native_key" for item in constraints)
    assert {item.name for item in constraints if isinstance(item, CheckConstraint)} >= {
        "ck_ai_asset_type",
        "ck_ai_asset_lifecycle_status",
        "ck_ai_asset_projection_status",
    }


def test_config_revision_carries_parent_and_rollback_evidence() -> None:
    from app.models.config_revision import ConfigRevision

    assert {"parent_revision_id", "rollback_of_revision_id"} <= set(ConfigRevision.__table__.columns.keys())
