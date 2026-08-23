"""C1 — daemon fanout caps: per-agent dispatch queues behind a semaphore
instead of stampeding the shared DB pool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_run_bounded_caps_concurrency_and_completes_all(monkeypatch) -> None:
    from app.config import get_settings
    from app.services.daemon_concurrency import reset_daemon_semaphores_for_tests, run_bounded

    monkeypatch.setattr(get_settings(), "HEARTBEAT_MAX_CONCURRENT", 2)
    reset_daemon_semaphores_for_tests()

    in_flight = 0
    peak = 0
    done: list[int] = []

    async def work(index: int) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        done.append(index)

    await asyncio.gather(*(run_bounded("heartbeat", work(i)) for i in range(6)))

    assert peak <= 2, f"expected at most 2 concurrent heartbeat executions, saw {peak}"
    assert sorted(done) == list(range(6)), "queued tasks must run, not drop"


@pytest.mark.asyncio
async def test_run_bounded_families_are_independent(monkeypatch) -> None:
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
    """Wiring pin: every per-agent create_task dispatch goes through run_bounded.

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

    def bounded_count(source: str, family: str) -> int:
        return len(re.findall(rf'run_bounded\(\s*"{family}"', source))

    assert bounded_count(heartbeat_source, "heartbeat") >= 2, "heartbeat resume + tick dispatch must be bounded"
    assert bounded_count(trigger_source, "trigger") == 0, "trigger runs must be queued for the worker, not fanned out"
    assert "_queue_trigger_run_for_worker(" in trigger_source
    assert 'task.task_type == "trigger"' in runtime_worker_source
    assert "enqueue_due_dream" in trigger_source, "trigger-side Dream must enqueue a durable RuntimeTask"
    assert "reconcile_due_dream_runtime_tasks" in evolution_source
    assert 'task.task_type == "dream"' in runtime_worker_source
    assert "_dispatch_async_runtime_task(" in runtime_worker_source
    assert "asyncio.create_task(_execute_heartbeat(" not in heartbeat_source
    assert "asyncio.create_task(_invoke_agent_for_triggers(" not in trigger_source
    assert "asyncio.create_task(run_dream(" not in trigger_source
    assert "asyncio.create_task(run_dream(" not in evolution_source
