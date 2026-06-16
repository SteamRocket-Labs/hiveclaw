from __future__ import annotations

from pathlib import Path

import pytest


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
