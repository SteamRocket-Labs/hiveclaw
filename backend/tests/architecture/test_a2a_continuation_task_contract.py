"""Anti-drift contract for the ``a2a_continuation`` RuntimeTask type.

DAY1-A2A-CONT-RETURN-001 happened because one allowlist (the continuation
coordinator's task-type choice) drifted from another (the completion-outbox
eligibility predicate). These assertions pin every authoritative allowlist
that must move together so a future change cannot silently recreate that
production break, and pin the inverse: an ordinary top-level ``web_chat_turn``
must never become completion-outbox eligible (no self-notification).
"""

from __future__ import annotations


def test_a2a_continuation_is_registered_in_every_executable_chat_allowlist() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_TASK_TYPES, COMPLETION_OUTBOX_PENDING_SQL, RuntimeTask
    from app.services.runtime_task_claim_service import LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES
    from app.services.runtime_task_service import _RESTART_RESUMABLE_TASK_TYPES
    from app.services.runtime_task_worker import SUPPORTED_RUNTIME_TASK_TYPES, _parse_task_type_limits
    from app.services.web_chat_runtime import A2A_CONTINUATION_TASK_TYPE, EXECUTABLE_CHAT_TASK_TYPES
    from app.config import get_settings

    task_type = A2A_CONTINUATION_TASK_TYPE
    assert task_type == "a2a_continuation"

    # Executable-chat surface: admission, active-run detection, worker dispatch.
    assert task_type in EXECUTABLE_CHAT_TASK_TYPES
    assert task_type in SUPPORTED_RUNTIME_TASK_TYPES
    assert task_type in LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES
    assert task_type in _RESTART_RESUMABLE_TASK_TYPES

    # Durable completion-return eligibility.
    assert task_type in COMPLETION_OUTBOX_TASK_TYPES
    assert f"'{task_type}'" in COMPLETION_OUTBOX_PENDING_SQL

    # DB-level contracts mirrored by the a2a_continuation_task_0828 migration.
    constraint = next(
        arg for arg in RuntimeTask.__table_args__ if getattr(arg, "name", None) == "ck_runtime_tasks_task_type"
    )
    assert task_type in str(constraint.sqltext)
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

    source_kind_constraint = next(
        arg
        for arg in RuntimeNotificationOutbox.__table_args__
        if getattr(arg, "name", None) == "ck_runtime_notification_outbox_source_kind"
    )
    assert task_type in str(source_kind_constraint.sqltext)
    unique_index = next(
        arg
        for arg in RuntimeTask.__table_args__
        if getattr(arg, "name", None) == "uq_runtime_tasks_active_web_chat_session"
    )
    assert task_type in str(unique_index.dialect_options["postgresql"]["where"])
    assert task_type in str(unique_index.dialect_options["sqlite"]["where"])

    # Worker quota: the default deployment can always claim continuation runs.
    limits = _parse_task_type_limits(get_settings().RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS)
    assert limits.get(task_type, 0) >= 1


def test_ordinary_web_chat_turn_remains_completion_outbox_ineligible() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_TASK_TYPES, COMPLETION_OUTBOX_PENDING_SQL

    assert "web_chat_turn" not in COMPLETION_OUTBOX_TASK_TYPES
    assert "'web_chat_turn'" not in COMPLETION_OUTBOX_PENDING_SQL


def test_every_completion_outbox_task_type_is_db_constraint_legal() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_TASK_TYPES, RuntimeTask

    constraint = next(
        arg for arg in RuntimeTask.__table_args__ if getattr(arg, "name", None) == "ck_runtime_tasks_task_type"
    )
    constraint_text = str(constraint.sqltext)
    missing = [task_type for task_type in COMPLETION_OUTBOX_TASK_TYPES if task_type not in constraint_text]
    assert missing == []
