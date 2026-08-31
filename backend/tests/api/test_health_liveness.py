from __future__ import annotations

from pathlib import Path

import pytest


def test_build_identity_is_deterministic_and_changes_with_source(tmp_path: Path) -> None:
    from app.build_identity import source_build_identity

    source = tmp_path / "app"
    source.mkdir()
    module = source / "main.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")

    first = source_build_identity(tmp_path, included_paths=("app",))
    second = source_build_identity(tmp_path, included_paths=("app",))
    module.write_text("VALUE = 2\n", encoding="utf-8")
    changed = source_build_identity(tmp_path, included_paths=("app",))

    assert first == second
    assert first["revision"] == f"source-sha256:{first['sha256']}"
    assert first["sha256"] != changed["sha256"]


@pytest.mark.asyncio
async def test_health_includes_server_derived_build_identity(monkeypatch) -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()
    expected = {
        "schema": "hive.build_identity.v1",
        "status": "ok",
        "revision": "source-sha256:" + "a" * 64,
        "sha256": "a" * 64,
        "file_count": 7,
    }
    monkeypatch.setattr("app.build_identity.current_build_identity", lambda: expected)

    response = await health_check()

    assert response.components["build_identity"] == expected


@pytest.mark.asyncio
async def test_health_reports_degraded_when_daemon_crashed() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import mark_daemon_crashed, mark_daemon_started, reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()
    mark_daemon_started("trigger_daemon")
    mark_daemon_crashed("trigger_daemon", RuntimeError("boom"))

    response = await health_check()

    assert response.status == "degraded"
    assert response.components["daemons"]["trigger_daemon"]["state"] == "crashed"
    assert response.components["daemons"]["trigger_daemon"]["last_error"] == "boom"
    assert response.components["rls_runtime_role"]["runtime_role_checked"] is False


@pytest.mark.asyncio
async def test_health_includes_rls_runtime_role_component() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import (
        RlsRuntimeRoleSnapshot,
        set_runtime_rls_role_snapshot_for_tests,
    )

    reset_daemon_liveness()
    set_runtime_rls_role_snapshot_for_tests(
        RlsRuntimeRoleSnapshot(
            role_name="app_rls",
            superuser=False,
            bypassrls=False,
            enforcement="strict",
            checked=True,
        )
    )

    response = await health_check()

    assert response.status == "ok"
    assert response.components["rls_runtime_role"] == {
        "status": "ok",
        "runtime_role_checked": True,
        "role_name": "app_rls",
        "superuser": False,
        "bypassrls": False,
        "enforcement": "strict",
        "violations": [],
    }


@pytest.mark.asyncio
async def test_health_includes_code_execution_sandbox_probe_component(monkeypatch) -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    async def fake_latest_health():
        return {
            "status": "ok",
            "provider": "vercel_sandbox",
            "age_seconds": 120,
            "network_denied": True,
            "workspace_round_trip": True,
        }

    monkeypatch.setattr("app.main.latest_sandbox_probe_health", fake_latest_health)

    response = await health_check()

    assert response.status == "ok"
    assert response.components["code_execution_sandbox_probe"]["provider"] == "vercel_sandbox"
    assert response.components["code_execution_sandbox_probe"]["network_denied"] is True


@pytest.mark.asyncio
async def test_health_includes_db_pool_component() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    pool = response.components["db_pool"]
    assert {
        "size",
        "checked_out",
        "checked_in",
        "overflow",
        "max_overflow",
        "pool_timeout_seconds",
        "capacity",
        "saturation_pct",
    } <= set(pool)


@pytest.mark.asyncio
async def test_health_includes_event_loop_component() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    loop_stats = response.components["event_loop"]
    assert {"last_lag_ms", "max_lag_ms", "sample_interval_seconds", "samples", "running"} <= set(loop_stats)


@pytest.mark.asyncio
async def test_health_includes_runtime_control_bus_component() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    runtime_control_bus = response.components["runtime_control_bus"]
    assert {"running", "received", "last_type", "last_error", "restart_count"} <= set(runtime_control_bus)


@pytest.mark.asyncio
async def test_health_degrades_when_db_pool_saturated(monkeypatch) -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    def fake_snapshot() -> dict:
        return {
            "size": 20,
            "checked_out": 30,
            "checked_in": 0,
            "overflow": 10,
            "max_overflow": 10,
            "pool_timeout_seconds": 30,
            "capacity": 30,
            "saturation_pct": 100.0,
        }

    monkeypatch.setattr("app.database.snapshot_db_pool", fake_snapshot)

    response = await health_check()

    assert response.status == "degraded"
    assert response.components["db_pool"]["saturation_pct"] == 100.0


def test_prometheus_exports_daemon_liveness_metrics() -> None:
    from app.memory.metrics import render_prometheus
    from app.services.daemon_liveness import (
        mark_daemon_crashed,
        mark_daemon_started,
        mark_daemon_tick,
        reset_daemon_liveness,
    )

    reset_daemon_liveness()
    mark_daemon_started("trigger_daemon")
    mark_daemon_tick("trigger_daemon")
    mark_daemon_crashed("workflow_daemon", RuntimeError("worker died"))

    text = render_prometheus()

    assert 'hive_daemon_liveness_up{name="trigger_daemon"} 1' in text
    assert 'hive_daemon_liveness_up{name="workflow_daemon"} 0' in text
    assert 'hive_daemon_crash_total{name="workflow_daemon"} 1' in text
    assert 'hive_daemon_last_heartbeat_age_seconds{name="trigger_daemon"}' in text


def test_core_daemon_loops_are_wired_to_liveness_registry() -> None:
    root = Path(__file__).resolve().parents[2]

    trigger_source = (root / "app" / "services" / "trigger_daemon.py").read_text(encoding="utf-8")
    workflow_source = (root / "app" / "services" / "workflow_daemon.py").read_text(encoding="utf-8")
    evolution_source = (root / "app" / "services" / "evolution_daemon.py").read_text(encoding="utf-8")
    main_source = (root / "app" / "main.py").read_text(encoding="utf-8")

    assert 'mark_daemon_tick("trigger_daemon")' in trigger_source
    assert 'mark_daemon_tick("workflow_daemon")' in workflow_source
    assert 'mark_daemon_tick("evolution_daemon")' in evolution_source
    assert "mark_daemon_crashed(t.get_name(), exc)" in main_source
