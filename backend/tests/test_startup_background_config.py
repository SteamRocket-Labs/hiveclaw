from __future__ import annotations


def test_heavy_startup_background_work_is_opt_in_by_default(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("CHANNEL_STREAM_STARTUP_ENABLED", raising=False)
    monkeypatch.delenv("T0_STARTUP_BACKFILL_ENABLED", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.CHANNEL_STREAM_STARTUP_ENABLED is False
    assert settings.T0_STARTUP_BACKFILL_ENABLED is False

    get_settings.cache_clear()


def test_channel_stream_startup_helper_respects_setting(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", False)
    assert main_mod._channel_stream_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", True)
    assert main_mod._channel_stream_startup_enabled() is True
