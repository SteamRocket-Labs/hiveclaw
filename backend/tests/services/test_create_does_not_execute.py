"""F-2 core invariant — writing the agent board never triggers execution.

docs/multi-agent-mainline-F-design.md §1.2 / §3.3 / §5 (invariant 5):

* ``track_todo`` is a cognitive write — adding a todo must NOT start any
  execution (no ``execute_task``, no RuntimeTask, no background async task).
  This is the defining difference from the retired ``manage_tasks`` create
  path, which used to ``asyncio.create_task(execute_task(...))``.
* ``manage_tasks`` is no longer reachable as an agent tool (retired from the
  LLM tool face); only the REST layer keeps its own explicit ``execute_task``
  trigger (human push entry), which this change leaves untouched.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_track_todo_add_never_executes(tmp_path, monkeypatch):
    """track_todo(add) writes the ledger only — no execute_task, no async spawn."""
    import app.services.agent_work_ledger as ledger_mod
    from app.tools.handlers.work_ledger import track_todo
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    # Isolate the ledger writes to tmp by pointing AGENT_DATA_DIR there.
    monkeypatch.setattr(
        ledger_mod,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    # Spy on execute_task: if the cognitive write ever calls it, this fires.
    execute_calls: list[tuple] = []

    async def _spy_execute_task(*args, **kwargs):  # pragma: no cover - must not run
        execute_calls.append((args, kwargs))

    import app.services.task_executor as task_executor_mod

    monkeypatch.setattr(task_executor_mod, "execute_task", _spy_execute_task)

    agent_id = uuid4()
    request = ToolExecutionRequest(
        tool_name="track_todo",
        arguments={"action": "add", "title": "Investigate the auth bug"},
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            workspace=tmp_path,
            session_id="sess-1",
        ),
    )

    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
    raw = await track_todo(request)
    # Give any (wrongly) spawned background task a chance to register.
    await asyncio.sleep(0)
    tasks_after = {t for t in asyncio.all_tasks() if not t.done()}

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["action"] == "added"

    # No execution side effects whatsoever.
    assert execute_calls == [], "track_todo must never trigger execute_task"
    spawned = tasks_after - tasks_before - {asyncio.current_task()}
    assert not spawned, f"track_todo spawned background task(s): {spawned}"

    # The todo really was persisted (write happened, execution did not).
    ledger = ledger_mod.load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    assert ledger is not None
    assert [item["title"] for item in ledger["todo_items"]] == ["Investigate the auth bug"]


def test_manage_tasks_not_agent_callable():
    """manage_tasks is retired from the LLM tool face and the execution registry."""
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.decorator import clear_registry, get_all_registered_tools

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))
    try:
        collected = collect_tools()
        names = {tool["function"]["name"] for tool in collected.openai_tools}
        # Not an LLM-callable tool.
        assert "manage_tasks" not in names
        # Not registered as a decorator-level tool either.
        assert "manage_tasks" not in get_all_registered_tools()
        # Not in the execution registry (cannot be dispatched by name).
        assert collected.exec_registry._executors.get("manage_tasks") is None
    finally:
        clear_registry()
        for module_name in HANDLER_MODULES:
            importlib.reload(importlib.import_module(module_name))


def test_manage_tasks_service_function_still_exists_for_rest():
    """The CRUD service _manage_tasks survives for REST reuse (api/tasks.py).

    Retiring the *tool* must not delete the underlying service function — the
    human push entry (REST) still calls it, then triggers execute_task itself.
    """
    from app.services.agent_tool_domains.tasks import _manage_tasks

    assert callable(_manage_tasks)
    # And it no longer imports asyncio to self-spawn execution.
    source = Path(__file__).resolve().parents[2] / "app" / "services" / "agent_tool_domains" / "tasks.py"
    text = source.read_text(encoding="utf-8")
    assert "asyncio.create_task(execute_task" not in text
