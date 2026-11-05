"""Web MCP arguments are redacted before the real company approval path."""
import json
from dataclasses import asdict, is_dataclass

import pytest
from sqlalchemy import select, text

from tests.integration.conftest import _route_default_app_engine  # noqa: F401
from tests.services.pasted_secret_corpus_r3 import KEY
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments
from tests.services.test_pasted_secret_web_entries import principals, stored_rows
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_mcp_import_redacts_before_executor_approval_and_all_records(owner_sessionmaker, monkeypatch):
    import app.api.commands as api
    from app.models.capability_policy import CapabilityPolicy
    from app.models.audit import ApprovalRequest
    from app.services.capability_group_policy_service import set_tenant_capability_group_policy
    from app.services.approval_ticket import hash_tool_input

    ids = await _seed_session(owner_sessionmaker)
    config = {
        "headers": {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
                    "Accept": "application/json", "X-Endpoint": "https://example.com/mcp"},
        "env": {"API_KEY": KEY, "NODE_ENV": "production", "LOG_LEVEL": "debug",
                "SERVICE_URL": "https://example.com/mcp"},
    }
    captured = []
    execute_tool = api.execute_tool

    async def capture(tool_name, arguments, **kwargs):
        captured.append(arguments)
        return await execute_tool(tool_name, arguments, **kwargs)

    monkeypatch.setattr(api, "execute_tool", capture)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        await set_tenant_capability_group_policy(db, agent.tenant_id, "mcp_admin_pack", enabled=True)
        db.add(CapabilityPolicy(tenant_id=agent.tenant_id, agent_id=agent.id,
                               capability="agent.tool.install", allowed=True, requires_approval=True))
        await db.commit()
        result = await api.execute_agent_command(
            agent.id, "mcp", api.ExecuteCommandIn(
                arguments={"action": "import_server", "server_id": "synthetic-server", "config": config},
                session_id=str(session.id), origin="web"), user, db)
        approvals = (await db.scalars(select(ApprovalRequest).where(ApprovalRequest.agent_id == agent.id))).all()
        response = result["result"]
        response = json.loads(response.text if hasattr(response, "text") else response) if not isinstance(response, dict) else response
        evidence = {"result": {**result, "result": asdict(result["result"]) if is_dataclass(result["result"]) else result["result"]},
                    "response": response, "executor_arguments": captured,
                    "rows": await stored_rows(db, session.id, agent.id),
                    "approvals": [{column.name: getattr(row, column.name) for column in row.__table__.columns}
                                  for row in approvals]}
        for table in ("audit_logs", "agent_activity_logs", "security_audit_events"):
            records = await db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t WHERE row_to_json(t)::text LIKE :aid"), {"aid": f"%{agent.id}%"})
            evidence[table] = [json.loads(row) for row in records.scalars()]
        assert response["status"] == "approval_required"
        assert approvals, "the real company approval gate must persist its request"
        assert captured, "the command must reach the real tool execution boundary"
        assert_no_fragments(evidence, KEY)
        assert result["pasted_credential_notice"] is True
        expected = {**config, "headers": {**config["headers"], "Authorization": "Bearer [REDACTED_SECRET]"},
                    "env": {**config["env"], "API_KEY": "[REDACTED_SECRET]"}}
        assert captured[0]["config"] == expected
        assert approvals[0].normalized_arguments["config"] == expected
        assert approvals[0].input_hash == hash_tool_input("import_mcp_server", approvals[0].normalized_arguments)
