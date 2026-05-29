from __future__ import annotations


def test_agent_update_ignores_platform_managed_heartbeat_fields() -> None:
    from app.schemas.schemas import AgentUpdate

    update = AgentUpdate(
        name="Ops Agent",
        heartbeat_enabled=False,
        heartbeat_interval_minutes=999,
        heartbeat_active_hours="00:00-00:01",
    )

    assert update.model_dump(exclude_unset=True) == {"name": "Ops Agent"}


def test_create_digital_employee_tool_does_not_expose_heartbeat_controls() -> None:
    from app.tools.handlers.hr import create_digital_employee

    properties = create_digital_employee.tool_meta.parameters["properties"]

    assert "heartbeat_enabled" not in properties
    assert "heartbeat_interval_minutes" not in properties
    assert "heartbeat_active_hours" not in properties
