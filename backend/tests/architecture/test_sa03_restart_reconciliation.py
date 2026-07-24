from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.runtime_reconciliation import runtime_reconciliation_view
from app.services.runtime_task_service import build_restart_reconciliation_metadata


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_startup_resumes_safe_trigger_and_heartbeat_runs_before_orphan_reconciliation() -> None:
    source = (BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    trigger_resume = source.index("resumed_trigger_ids = await resume_persisted_trigger_runs")
    heartbeat_resume = source.index("resumed_heartbeat_ids = await resume_persisted_heartbeat_runs")
    resumed_exclusion = source.index("*resumed_heartbeat_ids")
    orphan_reconciliation = source.index("reconcile_orphaned_runtime_tasks(exclude_task_ids=set(resumed_task_ids))")

    assert trigger_resume < orphan_reconciliation
    assert heartbeat_resume < orphan_reconciliation
    assert resumed_exclusion < orphan_reconciliation


@pytest.mark.parametrize(
    ("task_type", "blocker"),
    [
        ("trigger", "session_bound_mutating_trigger"),
        ("heartbeat", "direct_core_audit_session_bound"),
    ],
)
def test_session_bound_trigger_and_heartbeat_runs_are_visible_but_not_blindly_retryable(
    task_type: str,
    blocker: str,
) -> None:
    task_id = str(uuid4())
    metadata = build_restart_reconciliation_metadata(
        {"resume_after_restart": True},
        task_type=task_type,
        task_id=task_id,
        blocker=blocker,
        summary=f"{task_type} run requires operator reconciliation",
        trace_id=f"trace-{task_type}",
        session_id=f"session-{task_type}",
    )
    task = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        task_type=task_type,
        status="needs_reconciliation",
        parent_agent_id=uuid4(),
        child_agent_id=None,
        child_agent_name=None,
        trace_id=f"trace-{task_type}",
        parent_session_id=None,
        child_session_id=f"session-{task_type}",
        result_summary=None,
        metadata_json=metadata,
        created_at=None,
        started_at=None,
        completed_at=None,
    )

    view = runtime_reconciliation_view(task)

    assert metadata["needs_reconciliation"] is True
    assert metadata["completion_journal"][-1]["status"] == "needs_reconciliation"
    assert "reconciliation_retry_allowed" not in metadata
    assert view["task_type"] == task_type
    assert view["reason"] == blocker
    assert view["side_effect_risk"] == "mutating"
    assert view["retry_allowed"] is False
