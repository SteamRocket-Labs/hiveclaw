from app.services.runtime_result_metrics import (
    record_runtime_result_observed,
    record_runtime_result_page,
    render_runtime_result_prometheus,
    reset_runtime_result_metrics,
)


def test_runtime_result_metrics_are_bounded_and_render_complete_counter_contract():
    reset_runtime_result_metrics()

    record_runtime_result_observed(source_kind="subagent", size_bytes=17)
    record_runtime_result_observed(source_kind="future-provider", size_bytes=5)
    record_runtime_result_page(
        delivery_mode="parent_continuation",
        outcome="prepared",
        item_count=25,
    )
    record_runtime_result_page(
        delivery_mode="future-delivery-mode",
        outcome="future-outcome",
        item_count=3,
    )

    rendered = render_runtime_result_prometheus()

    assert "# TYPE runtime_results_observed_total counter" in rendered
    assert "# TYPE runtime_result_bytes_observed_total counter" in rendered
    assert 'runtime_results_observed_total{source_kind="subagent"} 1' in rendered
    assert 'runtime_result_bytes_observed_total{source_kind="subagent"} 17' in rendered
    assert 'runtime_results_observed_total{source_kind="other"} 1' in rendered
    assert 'runtime_result_bytes_observed_total{source_kind="other"} 5' in rendered
    assert (
        'runtime_result_integration_pages_total{delivery_mode="parent_continuation",outcome="prepared"} 1' in rendered
    )
    assert (
        'runtime_result_integration_items_total{delivery_mode="parent_continuation",outcome="prepared"} 25' in rendered
    )
    assert 'runtime_result_integration_pages_total{delivery_mode="other",outcome="other"} 1' in rendered
    assert 'runtime_result_integration_items_total{delivery_mode="other",outcome="other"} 3' in rendered
