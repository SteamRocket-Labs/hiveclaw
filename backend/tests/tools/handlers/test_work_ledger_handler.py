"""Tests for the agent-authored Work Ledger tools (切口①).

docs/agent-task-cognitive-scaffold.md §5.2/§7: ``track_todo`` / ``record_finding``
/ ``read_ledger`` are *cognitive* actions. They write/read the existing governed
Work Ledger via the real service, scoped by agent_id under ``AGENT_DATA_DIR``.
The load-bearing invariant (§5.4 Delta-3, §8.1): writing a todo must produce
ledger state only and **never** trigger execution.

These tests use the real service + a temp data_root (no service mocks, per the
testing discipline).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


def _request(
    tmp_path: Path,
    arguments: dict,
    *,
    agent_id: uuid.UUID | None = None,
    session_id: str | None = "sess-1",
):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="track_todo",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id or uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path / "workspace",
            session_id=session_id,
        ),
    )


def _settings_patch(mp: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The service holds its own module-level reference to get_settings (``from
    # app.config import get_settings``), so patch the binding it actually calls
    # to redirect AGENT_DATA_DIR at the real prod code path (handler passes no
    # data_root). This exercises the same wiring production uses, isolated to tmp.
    mp.setattr(
        "app.services.agent_work_ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_track_todo_add_writes_todo_and_has_no_execution_side_effects(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    agent_id = uuid.uuid4()

    # Spy: if the cognitive write ever spawns execution, asyncio.create_task fires.
    import asyncio

    created_tasks: list = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        created_tasks.append(coro)
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", _spy_create_task)
    _settings_patch(monkeypatch, tmp_path)

    result = await track_todo(
        _request(tmp_path, {"action": "add", "title": "Collect Q3 revenue figures"}, agent_id=agent_id)
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["action"] == "added"
    item_id = payload["item"]["id"]
    assert item_id

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    assert ledger is not None
    titles = [item["title"] for item in ledger["todo_items"]]
    assert "Collect Q3 revenue figures" in titles

    # The defining invariant: writing a todo triggered NO execution.
    assert created_tasks == []


@pytest.mark.asyncio
async def test_track_todo_update_changes_status_in_place(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    added = json.loads(
        await track_todo(_request(tmp_path, {"action": "add", "title": "Write the summary"}, agent_id=agent_id))
    )
    item_id = added["item"]["id"]

    updated = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "update", "item_id": item_id, "status": "in_progress"},
                agent_id=agent_id,
            )
        )
    )
    assert updated["ok"] is True
    assert updated["action"] == "updated"

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    assert len(ledger["todo_items"]) == 1  # update, not a second todo
    assert ledger["todo_items"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_track_todo_add_requires_title(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import track_todo

    _settings_patch(monkeypatch, tmp_path)
    result = await track_todo(_request(tmp_path, {"action": "add"}))
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "title" in payload["error"].lower()


@pytest.mark.asyncio
async def test_track_todo_update_unknown_item_reports_error(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import track_todo

    _settings_patch(monkeypatch, tmp_path)
    result = await track_todo(
        _request(tmp_path, {"action": "update", "item_id": "does-not-exist", "status": "completed"})
    )
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()


@pytest.mark.asyncio
async def test_record_finding_records_three_types(tmp_path, monkeypatch):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import record_finding

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    def _req(args):
        from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

        return ToolExecutionRequest(
            tool_name="record_finding",
            arguments=args,
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=uuid.uuid4(),
                tenant_id=str(uuid.uuid4()),
                workspace=tmp_path / "workspace",
                session_id="sess-1",
            ),
        )

    finding = json.loads(
        await record_finding(_req({"type": "finding", "summary": "API caps at 100 rows/page", "trust": "verified"}))
    )
    assert finding["ok"] is True
    assert finding["type"] == "finding"

    question = json.loads(
        await record_finding(_req({"type": "open_question", "summary": "Is pagination cursor stable?"}))
    )
    assert question["ok"] is True
    assert question["type"] == "open_question"

    failure = json.loads(
        await record_finding(
            _req(
                {
                    "type": "failure",
                    "summary": "Export returned 503",
                    "next_strategy": "retry with backoff",
                }
            )
        )
    )
    assert failure["ok"] is True
    assert failure["type"] == "failure"

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    assert [f["summary"] for f in ledger["findings"]] == ["API caps at 100 rows/page"]
    assert ledger["open_questions"] == ["Is pagination cursor stable?"]
    assert [f["error"] for f in ledger["failures"]] == ["Export returned 503"]
    assert ledger["failures"][0]["next_strategy"] == "retry with backoff"


@pytest.mark.asyncio
async def test_record_finding_rejects_unknown_type(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import record_finding
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    _settings_patch(monkeypatch, tmp_path)
    request = ToolExecutionRequest(
        tool_name="record_finding",
        arguments={"type": "nonsense", "summary": "x"},
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=None,
            workspace=tmp_path / "workspace",
        ),
    )
    payload = json.loads(await record_finding(request))
    assert payload["ok"] is False
    assert "type" in payload["error"].lower()


@pytest.mark.asyncio
async def test_read_ledger_returns_phase_todos_findings(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import read_ledger, record_finding, track_todo
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    def _req(tool_name, args):
        return ToolExecutionRequest(
            tool_name=tool_name,
            arguments=args,
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=uuid.uuid4(),
                tenant_id=str(uuid.uuid4()),
                workspace=tmp_path / "workspace",
                session_id="sess-1",
            ),
        )

    await track_todo(_req("track_todo", {"action": "add", "title": "Step one"}))
    await record_finding(_req("record_finding", {"type": "finding", "summary": "Found something"}))

    view = json.loads(await read_ledger(_req("read_ledger", {})))
    assert view["ok"] is True
    ledger_view = view["ledger"]
    assert ledger_view is not None
    assert any(item["title"] == "Step one" for item in ledger_view["todo_items"])
    assert any(f["summary"] == "Found something" for f in ledger_view["findings"])


@pytest.mark.asyncio
async def test_general_work_ledger_is_scoped_to_session_by_default(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import read_ledger, track_todo
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    def _req(tool_name: str, args: dict, *, session_id: str):
        return ToolExecutionRequest(
            tool_name=tool_name,
            arguments=args,
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=uuid.uuid4(),
                tenant_id=str(uuid.uuid4()),
                workspace=tmp_path / "workspace",
                session_id=session_id,
            ),
        )

    await track_todo(_req("track_todo", {"action": "add", "title": "Session A only"}, session_id="sess-a"))
    await track_todo(_req("track_todo", {"action": "add", "title": "Session B only"}, session_id="sess-b"))

    view_a = json.loads(await read_ledger(_req("read_ledger", {}, session_id="sess-a")))["ledger"]
    view_b = json.loads(await read_ledger(_req("read_ledger", {}, session_id="sess-b")))["ledger"]

    titles_a = {item["title"] for item in view_a["todo_items"]}
    titles_b = {item["title"] for item in view_b["todo_items"]}
    assert titles_a == {"Session A only"}
    assert titles_b == {"Session B only"}


@pytest.mark.asyncio
async def test_read_ledger_absent_returns_present_false(tmp_path, monkeypatch):
    from app.tools.handlers.work_ledger import read_ledger
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    _settings_patch(monkeypatch, tmp_path)
    request = ToolExecutionRequest(
        tool_name="read_ledger",
        arguments={},
        context=ToolExecutionContext(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=None,
            workspace=tmp_path / "workspace",
        ),
    )
    view = json.loads(await read_ledger(request))
    assert view["ok"] is True
    assert view["ledger"] is None


@pytest.mark.asyncio
async def test_two_agents_ledgers_are_isolated(tmp_path, monkeypatch):
    """tenant/agent scope (§8 invariant 5): each agent writes its own scoped file."""
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    _settings_patch(monkeypatch, tmp_path)
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    await track_todo(_request(tmp_path, {"action": "add", "title": "A-only todo"}, agent_id=agent_a))

    ledger_a = load_agent_work_ledger(agent_id=agent_a, session_id="sess-1", data_root=tmp_path)
    ledger_b = load_agent_work_ledger(agent_id=agent_b, data_root=tmp_path)
    assert ledger_a is not None
    assert [item["title"] for item in ledger_a["todo_items"]] == ["A-only todo"]
    assert ledger_b is None  # agent B never wrote — no cross-agent leakage


@pytest.mark.asyncio
async def test_track_todo_add_with_owner_persists_owner(tmp_path, monkeypatch):
    """切口③: track_todo passes an optional owner through to the ledger todo."""
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    payload = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "add", "title": "Delegate research", "owner": "researcher-bot"},
                agent_id=agent_id,
            )
        )
    )
    assert payload["ok"] is True
    assert payload["item"]["owner"] == "researcher-bot"

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    assert ledger["todo_items"][0]["owner"] == "researcher-bot"


# ── T1.3 (§8.1 #4) — dependency DAG exposed on the tool contract ──


def test_track_todo_schema_exposes_blocks_and_blocked_by():
    """The service has carried ``blocks``/``blocked_by`` since 切口③; T1.3
    exposes them on the tool schema so the model can express the dependency
    DAG (CC Task parity: ``blocks``/``blockedBy``)."""
    from app.services.agent_tools import get_combined_openai_tools

    schema = next(
        t["function"]["parameters"] for t in get_combined_openai_tools() if t["function"]["name"] == "track_todo"
    )
    properties = schema["properties"]
    assert "blocks" in properties
    assert "blockedBy" in properties
    assert properties["blocks"]["type"] == "array"
    assert properties["blockedBy"]["type"] == "array"


@pytest.mark.asyncio
async def test_track_todo_persists_blocks_and_blocked_by(tmp_path, monkeypatch):
    """Handler → real service → ledger round-trip for the dependency DAG."""
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    first = json.loads(
        await track_todo(_request(tmp_path, {"action": "add", "title": "Gather data"}, agent_id=agent_id))
    )
    blocker_id = first["item"]["id"]

    payload = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "add", "title": "Write report", "blockedBy": [blocker_id]},
                agent_id=agent_id,
            )
        )
    )
    assert payload["ok"] is True
    assert payload["item"]["blockedBy"] == [blocker_id]

    update = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "update", "item_id": blocker_id, "blocks": [payload["item"]["id"]]},
                agent_id=agent_id,
            )
        )
    )
    assert update["ok"] is True
    assert update["item"]["blocks"] == [payload["item"]["id"]]

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    by_id = {item["id"]: item for item in ledger["todo_items"]}
    assert by_id[blocker_id]["blocks"] == [payload["item"]["id"]]
    assert by_id[payload["item"]["id"]]["blockedBy"] == [blocker_id]


@pytest.mark.asyncio
async def test_track_todo_can_clear_blocked_by_dependencies(tmp_path, monkeypatch):
    """Empty dependency arrays are intentional updates, not missing fields."""
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.tools.handlers.work_ledger import track_todo

    agent_id = uuid.uuid4()
    _settings_patch(monkeypatch, tmp_path)

    blocker = json.loads(
        await track_todo(_request(tmp_path, {"action": "add", "title": "Gather source"}, agent_id=agent_id))
    )
    blocked = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "add", "title": "Draft report", "blockedBy": [blocker["item"]["id"]]},
                agent_id=agent_id,
            )
        )
    )

    cleared = json.loads(
        await track_todo(
            _request(
                tmp_path,
                {"action": "update", "item_id": blocked["item"]["id"], "blockedBy": []},
                agent_id=agent_id,
            )
        )
    )
    assert cleared["ok"] is True
    assert cleared["item"]["blockedBy"] == []

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id="sess-1", data_root=tmp_path)
    by_id = {item["id"]: item for item in ledger["todo_items"]}
    assert by_id[blocked["item"]["id"]]["blockedBy"] == []
