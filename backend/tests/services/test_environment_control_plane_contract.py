"""Change A contract tests for the Agent Environment control plane."""

from __future__ import annotations

def test_environment_domain_models_are_registered_with_runtime_task_links():
    from app.database import Base
    from app.models import import_all_models
    from app.models.runtime_task import RuntimeTask

    import_all_models()

    assert {
        "execution_environments",
        "environment_sessions",
        "environment_leases",
        "environment_checkpoints",
    } <= set(Base.metadata.tables)
    assert {
        "environment_id",
        "environment_session_id",
        "environment_lease_id",
        "environment_checkpoint_id",
    } <= set(RuntimeTask.__table__.columns.keys())
    for column_name in (
        "environment_id",
        "environment_session_id",
        "environment_lease_id",
        "environment_checkpoint_id",
    ):
        assert RuntimeTask.__table__.columns[column_name].nullable is True
