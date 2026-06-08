"""F-2 single-board convergence — Work Ledger is the ONLY agent board.

docs/multi-agent-mainline-F-design.md §1.2 / §2.3 / §5: the agent-facing
DB-Task tools (``manage_tasks`` / ``list_tasks`` / ``get_task``) are retired
from the LLM tool face; the Work Ledger trio (``track_todo`` / ``read_ledger``
/ ``record_finding``) is the sole agent board.  Status on that board is the
single CC-aligned enum ``pending / in_progress / completed``.

These tests pin the retirement (so re-adding a DB-task tool to the agent face
fails loudly) and the status normalization contract.
"""

from __future__ import annotations

import importlib
from uuid import uuid4

RETIRED_DB_TASK_TOOLS = {"manage_tasks", "list_tasks", "get_task"}
WORK_LEDGER_TOOLS = {"track_todo", "read_ledger", "record_finding"}


def _collect():
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))
    return collect_tools()


def teardown_function():
    from app.tools.collector import HANDLER_MODULES
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))


def test_track_todo_is_only_board_tool():
    """The agent default tool face exposes the ledger trio and NOT the DB-task tools."""
    collected = _collect()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    # Retired: the DB-Task tools are no longer LLM-callable.
    leaked = RETIRED_DB_TASK_TOOLS & names
    assert not leaked, f"retired DB-task tools still on the agent face: {leaked}"

    # Present: the Work Ledger trio is the sole agent board.
    assert WORK_LEDGER_TOOLS <= names, f"missing ledger tools: {WORK_LEDGER_TOOLS - names}"


def test_ledger_status_enum_single(tmp_path):
    """Any of done/doing/complete/running collapses to the single CC enum on disk."""
    from app.services.agent_work_ledger import (
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    # raw status fed in  ->  persisted (single enum) status expected
    cases = [
        ("done", "completed"),
        ("complete", "completed"),
        ("doing", "pending"),  # "doing" is not a known active alias → defaults to pending
        ("running", "in_progress"),
        ("in_progress", "in_progress"),
        ("pending", "pending"),
    ]

    item_ids = []
    for raw, _expected in cases:
        result = upsert_agent_work_ledger_todo(
            agent_id=agent_id,
            title=f"todo-for-{raw}",
            status=raw,
            data_root=tmp_path,
        )
        item_ids.append(result["item"]["id"])
        # The returned item is already normalized.
        assert result["item"]["status"] == _expected, raw

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    persisted = {item["id"]: item["status"] for item in ledger["todo_items"]}
    for (raw, expected), item_id in zip(cases, item_ids):
        assert persisted[item_id] == expected, f"{raw} persisted as {persisted[item_id]}, expected {expected}"

    # The board only ever holds the three CC-aligned values.
    assert set(persisted.values()) <= {"pending", "in_progress", "completed"}
