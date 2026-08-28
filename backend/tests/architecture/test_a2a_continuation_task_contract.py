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


def test_completion_producer_converges_at_the_shared_terminal_seam() -> None:
    """The a2a_continuation normal producer must be atomic with the terminal write.

    Every executable-chat finalizer branch converges on
    ``_apply_terminal_task_update_and_settle``; the shared completion-outbox
    producer's only direct same-transaction caller in web-chat finalization
    is that seam's ``a2a_continuation`` branch, so no finalizer branch can
    commit a terminal ``a2a_continuation`` without its durable intent. The
    sweep re-enters the SAME shared producer only as idempotent crash/legacy
    recovery. Metadata can never select the parent-agent route for any task
    type; the legacy target-session/owner metadata fallback for existing
    non-``a2a_continuation`` types is unchanged and out of this contract's scope.
    """

    import ast
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    runtime_source = (backend / "app/services/web_chat_runtime.py").read_text(encoding="utf-8")
    # No finalizer may call the raw enqueue directly; the producer is reachable
    # only through the shared seam.
    assert "enqueue_completion_notification" not in runtime_source
    tree = ast.parse(runtime_source)
    producer_owners: list[str] = []
    seam_calls: set[str] = set()
    for function in (n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "produce_terminal_task_completion_notification" in calls:
            producer_owners.append(function.name)
        if function.name == "_apply_terminal_task_update_and_settle":
            seam_calls = calls
    assert producer_owners == ["_apply_terminal_task_update_and_settle"]
    assert "produce_terminal_task_completion_notification" in seam_calls
    assert "settle_runtime_task_terminal" in seam_calls

    outbox_source = (backend / "app/services/runtime_notification_outbox.py").read_text(encoding="utf-8")
    # Metadata never selects the parent-agent route for any task type.
    assert 'metadata.get("parent_agent_id")' not in outbox_source
    outbox_tree = ast.parse(outbox_source)
    reconciler_calls: set[str] = set()
    for function in (n for n in ast.walk(outbox_tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
        if function.name == "reconcile_terminal_tasks_once":
            reconciler_calls = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
            }
    # The sweep is crash/legacy recovery over the same shared producer.
    assert "produce_terminal_task_completion_notification" in reconciler_calls
