"""§9 P0: ledger_todo_id threading through delegation (切口③ 收尾).

Pure-filesystem unit tests (tmp data_root) — the work ledger is a file
artifact, no DB involved. Contract under test:

* ``AgentDelegationRequest.ledger_todo_id`` exists and ``delegate_to_agent``
  / ``delegate_async`` thread it through;
* spawn stamps the parent todo with the child as ``owner`` (assign half);
* completion writes the terminal status back onto the parent ledger with
  ``expected_owner`` so a stray write cannot flip someone else's todo
  (fail-closed via PermissionError);
* failure releases the todo back to ``pending`` instead of completing it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents import orchestrator
from app.agents.orchestrator import AgentDelegationRequest, _delegate
from app.config import get_settings
from app.services.agent_work_ledger import (
    load_agent_work_ledger,
    upsert_agent_work_ledger_todo,
)

PARENT_SESSION = "parent-sess"


@pytest.fixture()
def ledger_root(tmp_path, monkeypatch):
    """Point the ledger's default data root at a tmp dir.

    Production write-back uses the default resolution (settings.AGENT_DATA_DIR);
    redirecting the setting keeps the code path identical to production.
    """
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    return tmp_path


def _stub_target() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="child-agent", role_description="stub")


def _request(parent_id: uuid.UUID, target: SimpleNamespace, ledger_todo_id: str | None) -> AgentDelegationRequest:
    return AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(provider="anthropic", model="claude-x"),
        conversation_messages=[{"role": "user", "content": "do the delegated thing"}],
        owner_id=uuid.uuid4(),
        session_id="child-sess",
        parent_agent_id=parent_id,
        parent_session_id=PARENT_SESSION,
        ledger_todo_id=ledger_todo_id,
    )


def _seed_todo(parent_id: uuid.UUID) -> str:
    created = upsert_agent_work_ledger_todo(
        agent_id=parent_id,
        title="delegate me",
        status="pending",
        session_id=PARENT_SESSION,
    )
    return created["item"]["id"]


def _todo(parent_id: uuid.UUID, item_id: str) -> dict:
    ledger = load_agent_work_ledger(agent_id=parent_id, session_id=PARENT_SESSION)
    items = (ledger or {}).get("todo_items") or []
    return next(item for item in items if item.get("id") == item_id)


def _ok_invoke(content: str = "done"):
    async def invoke(_invocation):
        return SimpleNamespace(content=content, tokens_used=1)

    return invoke


async def test_delegation_completion_writes_back_completed(ledger_root, monkeypatch):
    parent_id = uuid.uuid4()
    target = _stub_target()
    todo_id = _seed_todo(parent_id)
    monkeypatch.setattr(orchestrator, "invoke_agent", _ok_invoke())

    result = await _delegate(_request(parent_id, target, todo_id))

    assert result.failed is False
    todo = _todo(parent_id, todo_id)
    assert todo["status"] == "completed"
    assert todo["owner"] == str(target.id)


async def test_delegation_spawn_stamps_owner(ledger_root, monkeypatch):
    parent_id = uuid.uuid4()
    target = _stub_target()
    todo_id = _seed_todo(parent_id)

    captured: dict = {}

    async def invoke(_invocation):
        # Owner must already be stamped while the child is running.
        captured["todo_mid_flight"] = _todo(parent_id, todo_id)
        return SimpleNamespace(content="ok", tokens_used=1)

    monkeypatch.setattr(orchestrator, "invoke_agent", invoke)
    await _delegate(_request(parent_id, target, todo_id))

    assert captured["todo_mid_flight"]["owner"] == str(target.id)
    assert captured["todo_mid_flight"]["status"] == "in_progress"


async def test_delegation_failure_releases_todo_to_pending(ledger_root, monkeypatch):
    parent_id = uuid.uuid4()
    target = _stub_target()
    todo_id = _seed_todo(parent_id)

    async def failing_invoke(_invocation):
        raise RuntimeError("child blew up")

    monkeypatch.setattr(orchestrator, "invoke_agent", failing_invoke)
    result = await _delegate(_request(parent_id, target, todo_id))

    assert result.failed is True
    todo = _todo(parent_id, todo_id)
    assert todo["status"] == "pending"


async def test_write_back_owner_mismatch_fails_closed(ledger_root, monkeypatch):
    """If someone else owns the todo by completion time, the write-back must
    NOT flip it (record_delegated_todo_status raises PermissionError, which
    the delegation path logs and swallows — ledger is an observation surface,
    never a control surface)."""
    parent_id = uuid.uuid4()
    target = _stub_target()
    todo_id = _seed_todo(parent_id)

    async def hijacking_invoke(_invocation):
        # Mid-flight, the owner changes (e.g. user reassigned the todo).
        upsert_agent_work_ledger_todo(
            agent_id=parent_id,
            item_id=todo_id,
            owner="someone-else",
            status="in_progress",
            session_id=PARENT_SESSION,
        )
        return SimpleNamespace(content="ok", tokens_used=1)

    monkeypatch.setattr(orchestrator, "invoke_agent", hijacking_invoke)
    result = await _delegate(_request(parent_id, target, todo_id))

    assert result.failed is False  # delegation itself succeeded
    todo = _todo(parent_id, todo_id)
    assert todo["owner"] == "someone-else"
    assert todo["status"] == "in_progress"  # NOT flipped to completed


async def test_no_ledger_todo_id_means_no_ledger_writes(ledger_root, monkeypatch):
    parent_id = uuid.uuid4()
    target = _stub_target()
    todo_id = _seed_todo(parent_id)
    monkeypatch.setattr(orchestrator, "invoke_agent", _ok_invoke())

    await _delegate(_request(parent_id, target, None))

    todo = _todo(parent_id, todo_id)
    assert todo["status"] == "pending"
    assert "owner" not in todo
