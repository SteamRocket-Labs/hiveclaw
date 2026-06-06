"""P1-W2-4 — evolution_daemon decoupling.

The self-evolution loop (heartbeat + workspace sync) was previously inline
in `trigger_daemon`. Splitting it lets a slow heartbeat tick or workspace
volume I/O run without delaying trigger evaluations.

Tests assert the contract:
  - `start_evolution_daemon` exists and is callable as an async coroutine
  - the heartbeat loop is registered with the configured interval
  - `start_trigger_daemon` no longer touches heartbeat/workspace symbols
    (the contract is enforced by source inspection so it stays cheap and
    doesn't require booting the loop)
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from app.services import evolution_daemon


# ── Module shape ──────────────────────────────────────────────


def test_module_exports_start_function() -> None:
    assert hasattr(evolution_daemon, "start_evolution_daemon")
    assert inspect.iscoroutinefunction(evolution_daemon.start_evolution_daemon)


def test_module_exports_heartbeat_loop() -> None:
    """The internal loop is the testable seam — kept as a callable so future
    tests can drive a single iteration via monkeypatching `asyncio.sleep`."""
    assert inspect.iscoroutinefunction(evolution_daemon._heartbeat_loop)


def test_default_interval_matches_legacy_value() -> None:
    """60s preserves the historical cadence so this refactor is observably
    behaviour-preserving for existing deployments."""
    assert evolution_daemon._HEARTBEAT_INTERVAL_SECONDS == 60


def test_interval_sourced_from_settings() -> None:
    """P1-W2-5 — cadence must come from Settings, not a hardcoded literal,
    so dev/staging can override via HEARTBEAT_TICK_SECONDS env var."""
    from app.config import get_settings

    assert evolution_daemon._HEARTBEAT_INTERVAL_SECONDS == get_settings().HEARTBEAT_TICK_SECONDS


def test_settings_exposes_heartbeat_default_minutes() -> None:
    """Per-agent heartbeat interval default must be readable from Settings
    so the heartbeat dispatcher fallback and CLAUDE.md stay in sync."""
    from app.config import get_settings

    assert get_settings().HEARTBEAT_DEFAULT_INTERVAL_MINUTES == 120


# ── Behaviour: one tick fires + cleanup runs + sleeps ─────────


@pytest.mark.asyncio
async def test_heartbeat_loop_invokes_tick_and_cleanup_per_iteration(monkeypatch) -> None:
    """Verify the loop wires heartbeat tick + pending-reply cleanup per tick.

    We hijack asyncio.sleep on the second iteration to break out, then
    assert each component fired exactly once.
    """
    tick_calls: list[None] = []
    cleanup_calls: list[None] = []
    sleep_calls: list[float] = []

    async def fake_tick() -> None:
        tick_calls.append(None)

    async def fake_cleanup(_db) -> None:
        cleanup_calls.append(None)

    class _StubDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    def fake_session():
        return _StubDB()

    monkeypatch.setattr("app.services.heartbeat._heartbeat_tick", fake_tick)
    monkeypatch.setattr(
        "app.services.pending_reply_service.cleanup_expired_replies",
        fake_cleanup,
    )
    monkeypatch.setattr("app.database.async_session", fake_session)

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(evolution_daemon.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await evolution_daemon._heartbeat_loop()

    assert len(tick_calls) == 1
    assert len(cleanup_calls) == 1
    assert sleep_calls == [evolution_daemon._HEARTBEAT_INTERVAL_SECONDS]


@pytest.mark.asyncio
async def test_heartbeat_tick_failure_does_not_break_cleanup(monkeypatch) -> None:
    """A heartbeat exception must not prevent the cleanup pass — both are
    independent best-effort operations."""
    cleanup_calls: list[None] = []

    async def boom_tick() -> None:
        raise RuntimeError("heartbeat blew up")

    async def fake_cleanup(_db) -> None:
        cleanup_calls.append(None)

    class _StubDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    def fake_session():
        return _StubDB()

    monkeypatch.setattr("app.services.heartbeat._heartbeat_tick", boom_tick)
    monkeypatch.setattr(
        "app.services.pending_reply_service.cleanup_expired_replies",
        fake_cleanup,
    )
    monkeypatch.setattr("app.database.async_session", fake_session)

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(evolution_daemon.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await evolution_daemon._heartbeat_loop()

    assert len(cleanup_calls) == 1


# ── Trigger daemon must no longer own heartbeat/workspace ─────


def test_trigger_daemon_no_longer_starts_heartbeat_or_workspace() -> None:
    """Fail loud if heartbeat / workspace logic creeps back into trigger_daemon.

    Source-level grep — cheap, doesn't boot the daemon, and stays accurate
    even when wider refactors land.
    """
    src = Path(
        Path(__file__).parent.parent.parent
        / "app"
        / "services"
        / "trigger_daemon.py"
    ).read_text(encoding="utf-8")

    # The lifespan symbols live in evolution_daemon now.
    assert "_workspace_sync_loop" not in src
    assert "_workspace_full_sweep_loop" not in src
    assert "start_redis_listener" not in src
    assert "_heartbeat_tick" not in src
    assert "cleanup_expired_replies" not in src
