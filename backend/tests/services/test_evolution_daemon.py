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


def test_module_exports_post_heartbeat_maintenance() -> None:
    assert inspect.iscoroutinefunction(evolution_daemon.run_heartbeat_evolution_maintenance)


@pytest.mark.asyncio
async def test_heartbeat_maintenance_sweeps_expired_provisional_trials(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    agent_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / "agent"
    sweep_calls = []

    async def fake_runtime_config(_agent_id):
        return SimpleNamespace(tenant_resolution_error=None, skill_candidate_loop_enabled=False)

    async def fake_ensure_workspace(_agent_id, *, tenant_id):
        assert tenant_id == str(tenant_id_value)
        return workspace

    async def fake_model(_agent_id, _tenant_id):
        return None

    def fake_sweep(path):
        sweep_calls.append(path)
        return {"checked": 2, "expired": 1, "needs_review": 1, "results": []}

    async def fake_sync(_agent_id, _tenant_id):
        return SimpleNamespace(synced=0, skipped=0, reason="test")

    tenant_id_value = tenant_id
    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", fake_runtime_config)
    monkeypatch.setattr("app.tools.workspace.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr(evolution_daemon, "_resolve_agent_model", fake_model)
    monkeypatch.setattr("app.services.provisional_trial.sweep_expired_provisional_trials", fake_sweep)
    monkeypatch.setattr("app.memory.enhancement.sync_t3_to_memory_enhancement", fake_sync)

    report = await evolution_daemon.run_heartbeat_evolution_maintenance(
        agent_id=agent_id,
        tenant_id=tenant_id,
        outcome_type="noop",
        current_session_id="heartbeat-session",
    )

    assert sweep_calls == [workspace]
    assert report["provisional_trial_sweep"] == {
        "checked": 2,
        "expired": 1,
        "needs_review": 1,
        "results": [],
    }


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

        async def execute(self, *_a, **_k):
            # Absorb the RLS GUC statement (SET LOCAL app.current_tenant_id = ...)
            # emitted by enter_rls_bypass before the cleanup runs.
            return None

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

        async def execute(self, *_a, **_k):
            # Absorb the RLS GUC statement (SET LOCAL app.current_tenant_id = ...)
            # emitted by enter_rls_bypass before the cleanup runs.
            return None

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


@pytest.mark.asyncio
async def test_heartbeat_loop_drains_personal_kb_jobs_per_iteration(monkeypatch) -> None:
    drain_calls: list[None] = []

    async def fake_tick() -> None:
        return None

    async def fake_cleanup(_db) -> None:
        return None

    async def fake_drain() -> None:
        drain_calls.append(None)

    class _StubDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *_a, **_k):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr("app.services.heartbeat._heartbeat_tick", fake_tick)
    monkeypatch.setattr("app.services.pending_reply_service.cleanup_expired_replies", fake_cleanup)
    monkeypatch.setattr("app.database.async_session", lambda: _StubDB())
    monkeypatch.setattr(evolution_daemon, "_drain_personal_kb_jobs", fake_drain, raising=False)

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(evolution_daemon.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await evolution_daemon._heartbeat_loop()

    assert drain_calls == [None]


# ── Trigger daemon must no longer own heartbeat/workspace ─────


def test_trigger_daemon_no_longer_starts_heartbeat_or_workspace() -> None:
    """Fail loud if heartbeat / workspace logic creeps back into trigger_daemon.

    Source-level grep — cheap, doesn't boot the daemon, and stays accurate
    even when wider refactors land.
    """
    src = Path(Path(__file__).parent.parent.parent / "app" / "services" / "trigger_daemon.py").read_text(
        encoding="utf-8"
    )

    # The lifespan symbols live in evolution_daemon now.
    assert "_workspace_sync_loop" not in src
    assert "_workspace_full_sweep_loop" not in src
    assert "start_redis_listener" not in src
    assert "_heartbeat_tick" not in src
    assert "cleanup_expired_replies" not in src


def test_heartbeat_source_no_longer_owns_peripheral_evolution_jobs() -> None:
    src = Path(Path(__file__).parent.parent.parent / "app" / "services" / "heartbeat.py").read_text(encoding="utf-8")

    assert "run_skill_distillation_cycle" not in src
    assert "run_skill_curator_pass" not in src
    assert "run_scene_wiki_curation_tick" not in src
    assert "record_dream_activity" not in src
    assert "should_dream" not in src
    assert "run_dream" not in src
    assert "validate_and_normalize_t3" not in src
    assert "sync_t3_to_memory_enhancement" not in src


def test_evolution_daemon_has_no_retired_scene_wiki_curation_lane() -> None:
    root = Path(__file__).parent.parent.parent
    src = (root / "app" / "services" / "evolution_daemon.py").read_text(encoding="utf-8")

    assert "run_scene_wiki_curation_tick" not in src
    assert '"scene_wiki_curation"' not in src
    assert not (root / "app" / "services" / "memory_curation.py").exists()


def test_evolution_daemon_uses_model_owned_skill_lifecycle_review() -> None:
    root = Path(__file__).parent.parent.parent
    source = (root / "app" / "services" / "evolution_daemon.py").read_text(encoding="utf-8")

    assert "review_skill_lifecycle_with_model" in source
    assert "run_skill_curator_pass" not in source
    assert "model=model" in source


def test_hook_setup_schedules_evolution_maintenance_after_heartbeat() -> None:
    from app.runtime import hooks_setup
    from app.runtime.hooks import HookEvent

    specs = [
        spec
        for spec in hooks_setup._MEMORY_HOOK_CONFIGURATION  # noqa: SLF001 - tests the registration contract.
        if spec["event"] == HookEvent.HEARTBEAT_TICK_END.value
    ]

    assert any(spec["handler"] == "t0_heartbeat_tick_end" for spec in specs)
    assert any(spec["handler"] == "evolution_maintenance_on_heartbeat" for spec in specs)


@pytest.mark.asyncio
async def test_heartbeat_hook_persists_dream_admission_before_detaching_maintenance(monkeypatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _evolution_maintenance_on_heartbeat

    agent_id = uuid4()
    tenant_id = uuid4()
    sequence: list[str] = []

    async def durable_admission(**kwargs):
        assert kwargs == {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "outcome_type": "action_taken",
        }
        sequence.append("durable_admission")

    def capture_task(coro, *, name):
        assert name == f"heartbeat_evolution_maintenance:{agent_id}"
        sequence.append("maintenance_detached")
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(evolution_daemon, "record_and_enqueue_heartbeat_dream", durable_admission)
    monkeypatch.setattr("app.runtime.hooks_setup.asyncio.create_task", capture_task)

    await _evolution_maintenance_on_heartbeat(
        HookContext(
            event=HookEvent.HEARTBEAT_TICK_END,
            agent_id=str(agent_id),
            metadata={"tenant_id": str(tenant_id), "outcome": "action_taken"},
        )
    )

    assert sequence == ["durable_admission", "maintenance_detached"]


@pytest.mark.asyncio
async def test_drain_personal_kb_jobs_smoke_runs_real_body(monkeypatch) -> None:
    """B6: the drain body must execute end-to-end with a session factory —
    guards the asynccontextmanager import and the two-phase worker handoff."""
    captured: dict[str, object] = {}

    class _StubSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _StubBypass:
        def __init__(self, db, *, reason: str):
            captured["reason"] = reason

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _StubSummary:
        attempted = 0

    async def fake_claim_and_process(self, session, **kwargs):
        captured["session_is_none"] = session is None
        factory = kwargs.get("session_factory")
        assert factory is not None, "drain must hand a session factory to the two-phase worker"
        # The factory itself must be an async context manager (this is where
        # the missing asynccontextmanager import used to explode).
        async with factory() as db:
            captured["db"] = db
        captured["limit"] = kwargs.get("limit")
        return _StubSummary()

    monkeypatch.setattr("app.database.async_session", lambda: _StubSession())
    monkeypatch.setattr("app.database.enter_rls_bypass", lambda db, *, reason: _StubBypass(db, reason=reason))
    monkeypatch.setattr(
        "app.services.personal_knowledge_service.PersonalKnowledgeService.claim_and_process_stuck_jobs",
        fake_claim_and_process,
    )

    await evolution_daemon._drain_personal_kb_jobs()

    assert captured["session_is_none"] is True
    assert isinstance(captured["db"], _StubBypass)
    assert "personal-kb import job drain" in str(captured["reason"])
