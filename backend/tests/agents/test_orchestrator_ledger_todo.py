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
    from app.core.execution_context import ExecutionPrincipal

    owner_id = uuid.uuid4()
    tenant_id = getattr(target, "tenant_id", None) or uuid.uuid4()
    target.tenant_id = tenant_id
    return AgentDelegationRequest(
        target=target,
        target_model=SimpleNamespace(provider="anthropic", model="claude-x"),
        conversation_messages=[{"role": "user", "content": "do the delegated thing"}],
        owner_id=owner_id,
        session_id="child-sess",
        parent_agent_id=parent_id,
        parent_session_id=PARENT_SESSION,
        ledger_todo_id=ledger_todo_id,
        execution_principal=ExecutionPrincipal(
            tenant_id=tenant_id,
            source_agent_id=parent_id,
            requester_user_id=owner_id,
            root_session_id=PARENT_SESSION,
            delegation_chain=(f"agent:{parent_id}",),
        ).to_evidence(),
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


# ── T1.3 (§8.1 #5) — ledger_todo_id exposed on the delegate contract ──


def test_delegate_to_agent_schema_exposes_ledger_todo_id():
    """``delegate_async`` has threaded ``ledger_todo_id`` since 切口③; T1.3
    exposes it on the tool schema so the parent model can bind the delegation
    to its own work-ledger todo."""
    from app.services.agent_tools import get_combined_openai_tools

    schema = next(
        t["function"]["parameters"] for t in get_combined_openai_tools() if t["function"]["name"] == "delegate_to_agent"
    )
    properties = schema["properties"]
    assert "ledger_todo_id" in properties
    assert properties["ledger_todo_id"]["type"] == "string"


@pytest.mark.asyncio
async def test_delegate_tool_args_thread_ledger_todo_id(monkeypatch):
    """messaging._delegate_to_agent_async passes args.ledger_todo_id through to
    delegate_async (the service half is covered by the stamp/write-back tests
    above)."""
    from app.services.agent_tool_domains import messaging

    captured: dict = {}

    async def fake_resolve(from_agent_id, agent_name, target_agent_id=None):
        source = SimpleNamespace(name="parent", creator_id=uuid.uuid4(), tenant_id=None)
        target = _stub_target()
        model = SimpleNamespace(provider="openai", model="x")
        return source, target, model, None

    async def fake_delegate_async(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(task_id="t-1", target_name="child-agent", trace_id="tr-1")

    monkeypatch.setattr(messaging, "_resolve_target_agent_runtime", fake_resolve)
    monkeypatch.setattr(orchestrator, "delegate_async", fake_delegate_async)

    out = await messaging._delegate_to_agent_async(
        uuid.uuid4(),
        {"agent_name": "child-agent", "message": "do the thing", "ledger_todo_id": "todo-7"},
    )
    assert "t-1" in out
    assert captured["ledger_todo_id"] == "todo-7"
