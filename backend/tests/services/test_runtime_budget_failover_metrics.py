from __future__ import annotations


def test_runtime_budget_failover_metric_is_low_cardinality_and_rendered() -> None:
    from app.services.runtime_budget_failover_metrics import (
        record_runtime_budget_root_failure,
        render_runtime_budget_failover_prometheus,
        reset_runtime_budget_failover_metrics,
    )

    reset_runtime_budget_failover_metrics()
    record_runtime_budget_root_failure(source="web", decision="interactive_degraded")
    record_runtime_budget_root_failure(source="slack", decision="interactive_degraded")
    record_runtime_budget_root_failure(source="tenant-specific-unbounded-value", decision="fail_closed")

    rendered = render_runtime_budget_failover_prometheus()
    assert 'runtime_budget_root_failures_total{decision="interactive_degraded",source="interactive"} 2' in rendered
    assert 'runtime_budget_root_failures_total{decision="fail_closed",source="other"} 1' in rendered
    assert "tenant-specific-unbounded-value" not in rendered
