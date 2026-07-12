from __future__ import annotations


def test_business_task_product_routes_cover_detail_cancel_retry_and_reconciliation() -> None:
    from app.api.tasks import router

    routes = {(route.path, method) for route in router.routes for method in getattr(route, "methods", set())}

    assert ("/agents/{agent_id}/tasks/{task_id}", "GET") in routes
    assert ("/agents/{agent_id}/tasks/{task_id}/cancel", "POST") in routes
    assert ("/agents/{agent_id}/tasks/{task_id}/retry", "POST") in routes
    assert ("/agents/{agent_id}/tasks/{task_id}/reconcile", "POST") in routes


def test_business_task_task_out_carries_canonical_runtime_projection() -> None:
    from app.schemas.schemas import TaskOut

    fields = TaskOut.model_fields
    assert {
        "runtime_status",
        "runtime_phase",
        "runtime_summary",
        "runtime_request_id",
        "reflection_session_id",
        "recovery_state",
        "recovery_message",
        "actions",
        "dependencies",
        "stages",
    } <= set(fields)
