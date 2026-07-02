from __future__ import annotations


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

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "runtime")
    assert main_mod._volume_bound_startup_enabled() is True


def test_api_role_path_boundary_allows_control_plane_and_rejects_volume_paths():
    import app.main as main_mod

    assert main_mod._api_role_allows_path("/api/health") is True
    assert main_mod._api_role_allows_path("/api/auth/login") is True
    assert main_mod._api_role_allows_path("/api/agents") is True
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/runs") is True
    assert main_mod._api_role_allows_path("/ws/chat/agent-1") is True

    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/transcript") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/workbench") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/files/download") is False
    assert main_mod._api_role_allows_path("/api/tools/execute") is False
