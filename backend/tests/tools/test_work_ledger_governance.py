"""Governance + registration tests for the agent-authored Work Ledger tools.

docs/agent-task-cognitive-scaffold.md §5.6 / §8 invariant 1 — the load-bearing
contract of 切口①: ``track_todo`` / ``record_finding`` / ``read_ledger`` are
*cognitive* actions, NOT governance actions. They must never be ``sensitive``,
never carry a ``plan_gate_action_kind``, and never be hard-gated by the service
gate. This is exactly what distinguishes them from ``manage_tasks`` (sensitive +
``start_long_task`` + create-即-execute). These tests pin that boundary so a
future edit that mistags them fails loudly.
"""

from __future__ import annotations

import importlib

import pytest

WORK_LEDGER_TOOLS = ("track_todo", "record_finding", "read_ledger")
WRITE_TOOLS = ("track_todo", "record_finding")


def setup_function():
    from app.tools.decorator import clear_registry

    clear_registry()


def teardown_function():
    from app.tools.collector import HANDLER_MODULES
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))


def _collect():
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))
    return collect_tools()


def test_work_ledger_tools_are_registered():
    collected = _collect()
    names = {tool["function"]["name"] for tool in collected.openai_tools}
    for tool_name in WORK_LEDGER_TOOLS:
        assert tool_name in names, f"{tool_name} missing from collected openai_tools"


def test_work_ledger_tools_are_executable():
    collected = _collect()
    for tool_name in WORK_LEDGER_TOOLS:
        assert collected.exec_registry._executors.get(tool_name) is not None


def test_write_tools_are_not_sensitive():
    collected = _collect()
    for tool_name in WRITE_TOOLS:
        assert tool_name not in collected.sensitive_tools, f"{tool_name} must not be governance=sensitive"


def test_read_ledger_is_safe_read_only_parallel():
    collected = _collect()
    assert "read_ledger" in collected.safe_tools
    assert "read_ledger" in collected.read_only_names
    assert "read_ledger" in collected.parallel_safe_names


def test_write_tools_are_not_read_only():
    """track_todo / record_finding mutate the ledger; they must not advertise read_only."""
    collected = _collect()
    for tool_name in WRITE_TOOLS:
        assert tool_name not in collected.read_only_names


def test_work_ledger_tools_carry_no_plan_gate_tag():
    """The decorator-level fact: none of the three is tagged plan_gate_action_kind."""
    _collect()
    from app.tools.decorator import get_all_registered_tools

    registered = get_all_registered_tools()
    for tool_name in WORK_LEDGER_TOOLS:
        meta, _fn = registered[tool_name]
        assert meta.plan_gate_action_kind == "", f"{tool_name} must not carry a plan_gate_action_kind"


def test_work_ledger_tools_are_not_in_plan_gated_set():
    _collect()
    from app.tools.plan_gate_registry import plan_gated_tool_action_kinds

    tagged = plan_gated_tool_action_kinds()
    for tool_name in WORK_LEDGER_TOOLS:
        assert tool_name not in tagged


@pytest.mark.parametrize("tool_name", WORK_LEDGER_TOOLS)
def test_work_ledger_tools_are_never_hard_gated(tool_name):
    _collect()
    from app.tools.plan_gate_registry import hard_gated_action_kind

    assert hard_gated_action_kind(tool_name) is None
    # Even an "add"/"create"-flavored payload must not flip them into a gate.
    assert hard_gated_action_kind(tool_name, {"action": "add", "type": "failure"}) is None
