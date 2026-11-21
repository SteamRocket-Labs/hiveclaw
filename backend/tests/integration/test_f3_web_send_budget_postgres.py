"""Check the budget transaction on the authenticated private Web send routes."""

from collections import defaultdict

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models.chat_session import ChatSession
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import configure


@pytest.mark.parametrize("existing", [False, True], ids=["new-session", "existing-session"])
async def test_send_creates_budget_root_in_the_policy_transaction(
    existing, member_request, owner_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    *_, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    url = f"/api/agents/{seed.agent_id}/sessions"
    if existing:
        async with owner_sessionmaker() as db:
            session = ChatSession(
                tenant_id=seed.tenant_id, agent_id=seed.agent_id, user_id=seed.user_id,
                source_channel="web", audience_kind="private",
            )
            db.add(session)
            await db.commit()
        url += f"/{session.id}"

    operations = defaultdict(list)
    committed = []

    def cursor(conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("SET LOCAL app.current_tenant_id"):
            operations[conn].append("bypass" if "'BYPASS'" in statement else "tenant")
        elif "FROM runtime_budget_policies" in statement:
            operations[conn].append("policy")
        elif statement.startswith("INSERT INTO runtime_budget_runs "):
            operations[conn].append("budget-root")
        elif statement.startswith("INSERT INTO runtime_tasks "):
            operations[conn].append("task")

    def commit(conn):
        committed.append(operations[conn])

    event.listen(Engine, "before_cursor_execute", cursor)
    event.listen(Engine, "commit", commit)
    try:
        receipt = await client.post(f"{url}/runs", json={"content": "Hello from F3"})
    finally:
        event.remove(Engine, "commit", commit)
        event.remove(Engine, "before_cursor_execute", cursor)

    assert receipt.status_code == 201, receipt.text
    budget_transactions = [ops for ops in operations.values() if {"policy", "budget-root"}.intersection(ops)]
    assert budget_transactions == [["bypass", "policy", "budget-root"]]
    (task_transaction,) = [ops for ops in committed if "task" in ops]
    assert task_transaction == ["tenant", "task"]
    assert committed.index(budget_transactions[0]) < committed.index(task_transaction)
