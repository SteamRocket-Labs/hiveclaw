from __future__ import annotations


def _removed_slug() -> str:
    return "_".join(("deep", "research"))


def _removed_pack() -> str:
    return f"{_removed_slug()}_pack"


def test_removed_handler_is_not_collected() -> None:
    from app.tools.collector import HANDLER_MODULES

    assert f"app.tools.handlers.{_removed_slug()}" not in HANDLER_MODULES


def test_removed_runtime_group_is_absent() -> None:
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    names = {group.name for group in RUNTIME_TOOL_GROUPS}
    exposed_tools = {tool for group in RUNTIME_TOOL_GROUPS for tool in group.tools}

    assert _removed_pack() not in names
    assert not any(tool.startswith(f"{_removed_slug()}_") for tool in exposed_tools)


def test_plan_mode_handoffs_do_not_register_removed_target() -> None:
    from app.services.plan_mode_registry import register_plan_mode_handoffs
    from app.services.plan_mode_service import PlanModeService

    service = PlanModeService()
    register_plan_mode_handoffs(service)

    assert _removed_slug() not in service._handoff_handlers
