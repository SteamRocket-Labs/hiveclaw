from __future__ import annotations


def test_recovery_manifest_metrics_expose_typed_status_and_reason_labels() -> None:
    from app.runtime.recovery_manifest_metrics import (
        record_recovery_manifest_event,
        render_recovery_manifest_prometheus,
        reset_recovery_manifest_metrics,
        snapshot_recovery_manifest_metrics,
    )

    reset_recovery_manifest_metrics()
    record_recovery_manifest_event(operation="load", status="quarantined", reason="integrity_mismatch")
    record_recovery_manifest_event(operation="load", status="quarantined", reason="integrity_mismatch")
    record_recovery_manifest_event(operation="persist", status="written")

    assert snapshot_recovery_manifest_metrics() == {
        "load:quarantined:integrity_mismatch": 2,
        "persist:written:none": 1,
    }
    rendered = render_recovery_manifest_prometheus()
    assert "# TYPE recovery_manifest_events_total counter" in rendered
    assert (
        'recovery_manifest_events_total{operation="load",status="quarantined",reason="integrity_mismatch"} 2'
        in rendered
    )
    assert 'recovery_manifest_events_total{operation="persist",status="written",reason="none"} 1' in rendered


def test_recovery_manifest_metrics_bound_untrusted_reason_cardinality() -> None:
    from app.runtime.recovery_manifest_metrics import (
        record_recovery_manifest_event,
        reset_recovery_manifest_metrics,
        snapshot_recovery_manifest_metrics,
    )

    reset_recovery_manifest_metrics()
    record_recovery_manifest_event(
        operation="load",
        status="unavailable",
        reason="unexpected/provider-specific/path/with-secret",
    )

    assert snapshot_recovery_manifest_metrics() == {"load:unavailable:other": 1}


def test_typed_unavailable_recovery_result_is_observable() -> None:
    from app.runtime.recovery_manifest_metrics import (
        reset_recovery_manifest_metrics,
        snapshot_recovery_manifest_metrics,
    )
    from app.runtime.recovery_manifest_store import unavailable_recovery_result

    reset_recovery_manifest_metrics()

    result = unavailable_recovery_result("session_context_unavailable")

    assert result.status == "unavailable"
    assert snapshot_recovery_manifest_metrics() == {
        "load:unavailable:session_context_unavailable": 1,
    }
