"""Check the budget transaction on the authenticated private Web send routes."""

from collections import defaultdict
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.chat_session import ChatSession
from app.models.runtime_budget import RuntimeBudgetPolicy, RuntimeBudgetRun
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import configure


@pytest.mark.parametrize("existing", [False, True], ids=["new-session", "existing-session"])
@pytest.mark.parametrize("policy_matches", [True, False], ids=["configured-policy", "builtin-fallback"])
async def test_send_creates_budget_root_in_the_policy_transaction(
    existing, policy_matches, member_request, owner_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    *_, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        policy = RuntimeBudgetPolicy(
            tenant_id=seed.tenant_id,
            name="F3 scoped policy",
            scope_type="source_profile",
            profile="web_chat_turn" if policy_matches else "scheduled",
            max_tokens=42_000_000,
            max_team_sessions=7,
            default_child_token_reservation=125_000,
            default_llm_call_token_reservation=150_000,
            policy_json={"source": "f3-fixture"},
        )
        db.add(policy)
        await db.commit()
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

    run = receipt.json() if existing else receipt.json()["run"]
    async with owner_sessionmaker() as db:
        root = (await db.scalars(select(RuntimeBudgetRun).where(
            RuntimeBudgetRun.tenant_id == seed.tenant_id,
            RuntimeBudgetRun.root_runtime_task_id == UUID(run["run_id"]),
        ))).one()
    if policy_matches:
        assert root.policy_id == policy.id
        assert root.max_tokens == 42_000_000
        assert root.max_team_sessions == 7
        assert root.policy_snapshot == {
            "policy_id": str(policy.id),
            "scope_type": "source_profile",
            "source": None,
            "profile": "web_chat_turn",
            "max_team_sessions": 7,
            "default_child_token_reservation": 125_000,
            "default_llm_call_token_reservation": 150_000,
            "policy_json": {"source": "f3-fixture"},
        }
    else:
        assert root.policy_id is None
        assert root.max_tokens == 50_000_000
        assert root.max_team_sessions == 4
        assert root.fail_mode == "require_confirmation"
        assert root.policy_snapshot == {
            "policy_id": None,
            "scope_type": "tenant_default",
            "source": None,
            "profile": None,
            "max_team_sessions": 4,
            "default_child_token_reservation": 200_000,
            "default_llm_call_token_reservation": 200_000,
            "policy_json": {"source": "built_in_fallback", "profile": "interactive", "requested_profile": "web_chat_turn"},
        }
