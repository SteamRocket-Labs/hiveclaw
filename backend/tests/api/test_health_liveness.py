from __future__ import annotations

import json
from datetime import datetime, timezone
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
    assert "daemons" not in response.components
    assert "rls_runtime_role" not in response.components


@pytest.mark.asyncio
async def test_health_response_never_carries_component_snapshot_text(monkeypatch) -> None:
    from app.main import health_check
    from app.services import runtime_control_bus, runtime_task_worker, web_chat_stream_bus
    from app.services.code_execution.probe import sandbox_probe_health_from_setting
    from app.services.daemon_liveness import mark_daemon_error, mark_daemon_started, reset_daemon_liveness
    from app.services.rls_runtime_guard import (
        RlsRuntimeRoleSnapshot,
        set_runtime_rls_role_snapshot_for_tests,
    )

    secret = "postgresql://user:pw@db.internal/hive"

    reset_daemon_liveness()
    mark_daemon_started("trigger_daemon")
    mark_daemon_error("trigger_daemon", f"worker blew up on {secret}")
    set_runtime_rls_role_snapshot_for_tests(
        RlsRuntimeRoleSnapshot(
            role_name=f"role_{secret.split('@')[-1]}",
            superuser=False,
            bypassrls=False,
            enforcement="warn",
            checked=False,
            error=f"connection failed: {secret}",
        )
    )

    # Persisted sandbox probe report: the probe command failed but its first
    # stdout line still landed in evidence.microvm_uname (probe.py stores it
    # even on failure), and the report carries an error dict with free text.
    stored_probe = {
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "report": {
            "kind": "code_execution_sandbox_probe.v1",
            "provider": "vercel_sandbox",
            "network_policy": "deny-all",
            "runtime": "python3.13",
            "passed": False,
            "checks": [],
            "evidence": {
                "microvm_uname": f"Linux-6.1 sandbox init aborted at {secret}",
                "network_denied": False,
                "workspace_round_trip": False,
            },
            "error": {"type": "RuntimeError", "message": f"probe bootstrap failed: {secret}"},
        },
        "trend": {
            "total_runs": 1,
            "passed_runs": 0,
            "failed_runs": 1,
            "pass_rate": 0.0,
            "latest_passed": False,
            "consecutive_failures": 1,
            "latest_provider": "vercel_sandbox",
            "latest_runtime": "python3.13",
        },
    }

    async def persisted_latest_sandbox_probe_health():
        return sandbox_probe_health_from_setting(stored_probe)

    monkeypatch.setattr("app.main.latest_sandbox_probe_health", persisted_latest_sandbox_probe_health)
    monkeypatch.setattr(
        runtime_control_bus,
        "_STATE",
        {**runtime_control_bus._STATE, "last_error": f"projection_sweeper:RuntimeError:{secret}"},
    )
    monkeypatch.setattr(
        runtime_task_worker,
        "_STATE",
        {
            **runtime_task_worker._STATE,
            "last_error": f"workflow:RuntimeError:{secret}",
            "reconcile_notes": [f"claim lost backoff anchor {secret}"],
        },
    )
    monkeypatch.setattr(
        web_chat_stream_bus,
        "_FORWARDER_STATE",
        {**web_chat_stream_bus._FORWARDER_STATE, "last_error": f"RuntimeError: {secret}"},
    )

    expected_identity = {
        "schema": "hive.build_identity.v1",
        "status": "unavailable",
        "error": "FileNotFoundError",
    }
    monkeypatch.setattr("app.build_identity.current_build_identity", lambda: expected_identity)

    response = await health_check()

    payload = json.dumps(response.model_dump(mode="json"))
    assert secret not in payload
    assert "pw@db.internal" not in payload

    # Status still derives from every component internally.
    assert response.status == "degraded"

    # The public body is exactly the projection its consumers read.
    from app.config import get_settings
    from app.main import _process_role

    settings = get_settings()

    assert response.version == settings.APP_VERSION
    assert set(response.components) == {"build_identity", "process_role"}
    assert response.components["build_identity"] == expected_identity
    assert response.components["process_role"] == {"role": _process_role()}


@pytest.mark.asyncio
async def test_sandbox_probe_read_failure_does_not_leak_error_text(monkeypatch) -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()
    secret = "postgresql://user:pw@db.internal/hive"
    monkeypatch.setenv("HIVE_CODE_EXEC_PROBE_SCHEDULER_ENABLED", "1")

    async def failing_latest_sandbox_probe_health():
        raise RuntimeError(f"sandbox probe unavailable: {secret}")

    monkeypatch.setattr("app.main.latest_sandbox_probe_health", failing_latest_sandbox_probe_health)

    response = await health_check()

    payload = json.dumps(response.model_dump(mode="json"))
    assert secret not in payload
    assert response.status == "degraded"
    assert set(response.components) == {"build_identity", "process_role"}


@pytest.mark.asyncio
async def test_health_omits_rls_runtime_role_details() -> None:
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
    assert "rls_runtime_role" not in response.components


@pytest.mark.asyncio
async def test_health_omits_code_execution_sandbox_probe_details(monkeypatch) -> None:
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
    assert "code_execution_sandbox_probe" not in response.components


@pytest.mark.asyncio
async def test_health_omits_db_pool_details() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    assert response.status == "ok"
    assert "db_pool" not in response.components


@pytest.mark.asyncio
async def test_health_omits_event_loop_details() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    assert "event_loop" not in response.components


@pytest.mark.asyncio
async def test_health_omits_runtime_control_bus_details() -> None:
    from app.main import health_check
    from app.services.daemon_liveness import reset_daemon_liveness
    from app.services.rls_runtime_guard import reset_runtime_rls_role_guard_for_tests

    reset_daemon_liveness()
    reset_runtime_rls_role_guard_for_tests()

    response = await health_check()

    assert "runtime_control_bus" not in response.components


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
    assert "db_pool" not in response.components


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
