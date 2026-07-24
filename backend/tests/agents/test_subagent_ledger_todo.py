"""§9 P0: ledger_todo_id threading for the synchronous subagent core.

Covers the lightweight-worker half of the P0 contract (peer delegation is
covered in test_orchestrator_ledger_todo.py):

* ``spawn_subagent(..., ledger_todo_id=...)`` stamps the parent todo with the
  worker's spec name as owner and writes the terminal status back on completion;
* owner mismatch at completion time fails closed (no flip);
* public background execution is covered by the durable RuntimeTask/outbox tests,
  not by an in-process fire-and-forget branch in this low-level core.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import SubagentSpawnContext, explorer_spec, spawn_subagent
from app.config import get_settings
from app.services.agent_work_ledger import (
    load_agent_work_ledger,
    upsert_agent_work_ledger_todo,
)

PARENT_SESSION = "parent-sess"


@pytest.fixture()
def ledger_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    return tmp_path


def _ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        parent_session_id=PARENT_SESSION,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _ok_invoke(content: str = "digest"):
    async def invoke(_request):
        return SimpleNamespace(content=content, tokens_used=1)

    return invoke


def _seed_todo(parent_id: uuid.UUID) -> str:
    created = upsert_agent_work_ledger_todo(
        agent_id=parent_id,
        title="spawn a worker for me",
        status="pending",
        session_id=PARENT_SESSION,
    )
    return created["item"]["id"]


def _todo(parent_id: uuid.UUID, item_id: str) -> dict:
    ledger = load_agent_work_ledger(agent_id=parent_id, session_id=PARENT_SESSION)
    items = (ledger or {}).get("todo_items") or []
    return next(item for item in items if item.get("id") == item_id)


async def test_sync_spawn_writes_back_completed(ledger_root):
    ctx = _ctx()
    todo_id = _seed_todo(ctx.parent_agent_id)

    handle = await spawn_subagent(ctx, explorer_spec("worker-1"), "task", ledger_todo_id=todo_id, invoke=_ok_invoke())

    assert handle.result is not None and handle.result.ok
    todo = _todo(ctx.parent_agent_id, todo_id)
    assert todo["owner"] == "worker-1"
    assert todo["status"] == "completed"


async def test_failed_spawn_releases_todo_to_pending(ledger_root):
    ctx = _ctx(trace_id="thr-ledger-fail")
    todo_id = _seed_todo(ctx.parent_agent_id)

    async def failing_invoke(_request):
        raise RuntimeError("worker blew up")

    handle = await spawn_subagent(
        ctx,
        explorer_spec("bg-worker"),
        "task",
        ledger_todo_id=todo_id,
        invoke=failing_invoke,
    )

    assert handle.result is not None and not handle.result.ok
    assert _todo(ctx.parent_agent_id, todo_id)["status"] == "pending"


async def test_write_back_owner_mismatch_fails_closed(ledger_root):
    ctx = _ctx(trace_id="thr-ledger-hijack")
    todo_id = _seed_todo(ctx.parent_agent_id)

    hijacked = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_invoke(_request):
        hijacked.set()
        await proceed.wait()
        return SimpleNamespace(content="late", tokens_used=1)

    worker = asyncio.create_task(
        spawn_subagent(
            ctx,
            explorer_spec("bg-worker"),
            "task",
            ledger_todo_id=todo_id,
            invoke=slow_invoke,
        )
    )
    await hijacked.wait()
    # Mid-flight reassignment to someone else.
    upsert_agent_work_ledger_todo(
        agent_id=ctx.parent_agent_id,
        item_id=todo_id,
        owner="someone-else",
        status="in_progress",
        session_id=PARENT_SESSION,
    )
    proceed.set()
    await worker

    todo = _todo(ctx.parent_agent_id, todo_id)
    assert todo["owner"] == "someone-else"
    assert todo["status"] == "in_progress"  # NOT flipped by the stale worker
