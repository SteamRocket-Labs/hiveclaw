from __future__ import annotations


def test_import_all_models_registers_session_feedback_table() -> None:
    from app.database import Base
    from app.models import import_all_models

    import_all_models()

    assert "session_feedback_events" in Base.metadata.tables
