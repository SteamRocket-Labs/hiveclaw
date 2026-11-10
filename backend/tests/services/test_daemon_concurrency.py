"""C1 — daemon fanout caps: per-agent dispatch queues behind a semaphore
instead of stampeding the shared DB pool."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_daemon_semaphore_families_are_independent(monkeypatch) -> None:
    from app.config import get_settings
    from app.services.daemon_concurrency import get_daemon_semaphore, reset_daemon_semaphores_for_tests

    settings = get_settings()
    monkeypatch.setattr(settings, "HEARTBEAT_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "TRIGGER_MAX_CONCURRENT", 3)
    monkeypatch.setattr(settings, "DREAM_MAX_CONCURRENT", 2)
    reset_daemon_semaphores_for_tests()

    assert get_daemon_semaphore("heartbeat") is get_daemon_semaphore("heartbeat")
    assert get_daemon_semaphore("heartbeat") is not get_daemon_semaphore("trigger")
    assert get_daemon_semaphore("trigger")._value == 3
    assert get_daemon_semaphore("dream")._value == 2


def test_fanout_settings_have_env_defaults() -> None:
    from app.config import Settings

    defaults = Settings(_env_file=None)
    assert defaults.HEARTBEAT_MAX_CONCURRENT == 4
    assert defaults.TRIGGER_MAX_CONCURRENT == 8
    assert defaults.DREAM_MAX_CONCURRENT == 2


def test_daemon_fanout_dispatch_sites_are_bounded() -> None:
    """Every per-agent dispatch reaches its family's shared concurrency cap.

    Trigger runs are deliberately absent: a bounded in-process fanout still had
    no lease and no completion callback, which is how 2,107 trigger rows sat in
    ``running`` for 38 days. They now queue for the RuntimeTask worker instead —
    see ``tests/services/test_trigger_dispatch_accountability.py``.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    heartbeat_source = (root / "app" / "services" / "heartbeat.py").read_text(encoding="utf-8")
    trigger_source = (root / "app" / "services" / "trigger_daemon.py").read_text(encoding="utf-8")
    evolution_source = (root / "app" / "services" / "evolution_daemon.py").read_text(encoding="utf-8")
    runtime_worker_source = (root / "app" / "services" / "runtime_task_worker.py").read_text(encoding="utf-8")
    terminal_processor_source = (
        root / "app" / "services" / "direct_invocation_terminal_boundary_processor.py"
    ).read_text(encoding="utf-8")

    def bounded_count(source: str, family: str) -> int:
        return len(re.findall(rf'run_bounded\(\s*"{family}"', source))

    from app.services.heartbeat import _execute_heartbeat
    import inspect

    heartbeat_entry = inspect.getsource(_execute_heartbeat)
    assert 'async with get_daemon_semaphore("heartbeat"):' in heartbeat_entry
    assert heartbeat_entry.index("asyncio.timeout(") < heartbeat_entry.index('get_daemon_semaphore("heartbeat")')
    assert heartbeat_source.count("asyncio.create_task(_execute_heartbeat(") == 2, (
        "heartbeat resume + tick must both enter the deadline and shared concurrency cap"
    )
    assert bounded_count(trigger_source, "trigger") == 0, "trigger runs must be queued for the worker, not fanned out"
    assert "_queue_trigger_run_for_worker(" in trigger_source
    assert 'task.task_type == "trigger"' in runtime_worker_source
    assert "enqueue_due_dream" not in trigger_source
    assert "enqueue_due_dream" in terminal_processor_source, (
        "trigger-side Dream must wait for the required terminal boundary before enqueueing"
    )
    assert "reconcile_due_dream_runtime_tasks" in evolution_source
    assert 'task.task_type == "dream"' in runtime_worker_source
    assert "_dispatch_async_runtime_task(" in runtime_worker_source
    assert "asyncio.create_task(_invoke_agent_for_triggers(" not in trigger_source
    assert "asyncio.create_task(run_dream(" not in trigger_source
    assert "asyncio.create_task(run_dream(" not in evolution_source
