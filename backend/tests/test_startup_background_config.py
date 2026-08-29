from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_heavy_startup_background_work_is_opt_in_by_default(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("CORE_DAEMON_STARTUP_ENABLED", raising=False)
    monkeypatch.delenv("CHANNEL_STREAM_STARTUP_ENABLED", raising=False)
    monkeypatch.delenv("T0_STARTUP_BACKFILL_ENABLED", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.CORE_DAEMON_STARTUP_ENABLED is False
    assert settings.CHANNEL_STREAM_STARTUP_ENABLED is False
    assert settings.T0_STARTUP_BACKFILL_ENABLED is False

    get_settings.cache_clear()


def test_core_daemon_startup_helper_respects_setting(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "CORE_DAEMON_STARTUP_ENABLED", False)
    assert main_mod._core_daemon_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "CORE_DAEMON_STARTUP_ENABLED", True)
    assert main_mod._core_daemon_startup_enabled() is True


def test_channel_stream_startup_helper_respects_setting(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", False)
    assert main_mod._channel_stream_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", True)
    assert main_mod._channel_stream_startup_enabled() is True


def test_api_role_disables_volume_bound_startup(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "api")
    assert main_mod._volume_bound_startup_enabled() is False
    assert main_mod._schema_bootstrap_startup_enabled() is False
    assert main_mod._data_bootstrap_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "runtime")
    assert main_mod._volume_bound_startup_enabled() is True
    assert main_mod._schema_bootstrap_startup_enabled() is True
    assert main_mod._data_bootstrap_startup_enabled() is True


def test_web_chat_stream_forwarder_runs_in_every_websocket_serving_role(monkeypatch):
    import app.main as main_mod

    for role in ("runtime", "api"):
        monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", role)
        assert main_mod._web_chat_stream_startup_enabled() is True

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "read_model")
    assert main_mod._web_chat_stream_startup_enabled() is False


def test_api_role_path_boundary_allows_control_plane_and_rejects_volume_paths():
    import app.main as main_mod

    assert main_mod._api_role_allows_path("/api/health") is True
    assert main_mod._api_role_allows_path("/api/auth/login") is True
    assert main_mod._api_role_allows_path("/api/notifications/unread-count") is True
    assert main_mod._api_role_allows_path("/api/v1/notifications/unread-count") is True
    assert main_mod._api_role_allows_path("/api/agents") is True
    assert main_mod._api_role_allows_path("/api/agents/") is True
    assert main_mod._api_role_allows_path("/api/v1/agents/") is True
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/runs") is True
    assert main_mod._api_role_allows_path("/ws/chat/agent-1") is True

    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/transcript") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/workbench") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/files/download") is False
    assert main_mod._api_role_allows_path("/api/enterprise/knowledge-base/files") is False
    assert main_mod._api_role_allows_path("/api/v1/enterprise/knowledge-base/upload") is False
    assert main_mod._api_role_allows_path("/api/tools/execute") is False


def test_runtime_resume_runs_as_background_startup_task_not_lifespan_blocker():
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")
    lifespan_source = source.split("async def lifespan", 1)[1]
    pre_background_task_setup = lifespan_source.split("# Start background tasks", 1)[0]
    background_task_setup = lifespan_source.split("startup_background_tasks = [", 1)[1].split(
        "for name, coro in startup_background_tasks:", 1
    )[0]

    assert "resume_persisted_subagent_runs" not in pre_background_task_setup
    assert "_resume_runtime_tasks_after_startup(" in background_task_setup
    assert "_run_after_startup_resume_gate(" in background_task_setup
    assert "reconcile_stuck_approval_tickets" in source


def test_skill_workspace_maintenance_runs_after_startup_without_blocking_the_event_loop():
    project_root = Path(__file__).resolve().parents[2]
    main_source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")
    lifespan_source = main_source.split("async def lifespan", 1)[1]
    pre_background_task_setup = lifespan_source.split("# Start background tasks", 1)[0]
    background_task_setup = lifespan_source.split("startup_background_tasks = [", 1)[1].split(
        "for name, coro in startup_background_tasks:", 1
    )[0]

    assert "await cleanup_retired_builtin_skills()" not in pre_background_task_setup
    assert "await push_default_skills_to_existing_agents()" not in pre_background_task_setup
    assert "skill_workspace_maintenance_ready = False" in pre_background_task_setup
    assert "skill_workspace_maintenance_ready = _volume_bound_startup_enabled()" in pre_background_task_setup
    assert "if skill_workspace_maintenance_ready:" in background_task_setup
    assert "skill_workspace_maintenance" in background_task_setup
    assert "_maintain_skill_workspaces_after_startup()" in background_task_setup

    seeder_source = (project_root / "backend/app/services/skill_seeder.py").read_text(encoding="utf-8")
    cleanup_source = seeder_source.split("async def cleanup_retired_builtin_skills", 1)[1].split(
        "def _push_default_skill_packages_to_agent", 1
    )[0]
    push_source = seeder_source.split("async def push_default_skills_to_existing_agents", 1)[1]

    assert "await asyncio.to_thread" in cleanup_source
    assert "await asyncio.to_thread" in push_source


@pytest.mark.asyncio
async def test_runtime_task_worker_waits_for_startup_resume_gate():
    import app.main as main_mod

    resume_done = asyncio.Event()
    calls: list[str] = []

    async def worker_loop():
        calls.append("worker_started")

    worker_task = asyncio.create_task(main_mod._run_after_startup_resume_gate(resume_done, worker_loop()))
    await asyncio.sleep(0)
    assert calls == []

    resume_done.set()
    await asyncio.wait_for(worker_task, timeout=1)
    assert calls == ["worker_started"]
