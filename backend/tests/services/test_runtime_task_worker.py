from __future__ import annotations

from types import SimpleNamespace


def test_worker_claimable_task_types_cover_v3_runtime_planes():
    import app.services.runtime_task_worker as worker

    assert "web_chat_turn" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "workflow" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "delegation" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "business_task" in worker.SUPPORTED_RUNTIME_TASK_TYPES


def test_worker_claim_batch_is_capped_by_active_web_chat_runs(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 7)

    assert worker._claim_batch_size_for_available_slots() == 1

    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 8)

    assert worker._claim_batch_size_for_available_slots() == 0


def test_worker_claim_batch_uses_configured_batch_when_capacity_allows(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)

    assert worker._claim_batch_size_for_available_slots() == 4


def test_worker_task_type_limit_parser_caps_claimable_types(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS="web_chat_turn=2,workflow=1,delegation=3",
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=8,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)
    monkeypatch.setattr(worker, "_active_dispatched_task_type_counts", lambda: {"web_chat_turn": 2, "workflow": 0})

    assert worker._task_type_capacity_remaining("web_chat_turn") == 0
    assert worker._task_type_capacity_remaining("workflow") == 1
    assert worker._claimable_task_types_for_available_capacity() == ("workflow", "delegation")
