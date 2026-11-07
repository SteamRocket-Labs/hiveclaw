"""Storage-only encryption writes cannot restore a handed-over credential."""

import asyncio
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.util.concurrency import await_only

from app.config import get_settings
from app.models.tool import AgentTool
from app.services.agent_tool_config_storage import decrypt_agent_tool_config, migrate_agent_tool_config_rows
from app.services.secrets_provider import FernetSecretsProvider
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


async def test_storage_rotation_uses_the_post_handover_value(owner_sessionmaker, monkeypatch):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"api_key": "sk-e2e-fake-before-rotation"})
        db.add(assignment)
        await db.commit()
        assignment_id = assignment.id
        rotating = FernetSecretsProvider("rotation-" + uuid4().hex,
            previous_master_keys=[get_settings().SECRETS_MASTER_KEY])
        reached, resume = asyncio.Event(), asyncio.Event()
        await offboard(state)

        async def rotate():
            async with owner_sessionmaker() as operator:
                connection = await operator.connection()

                def apply(bind):
                    execute = bind.execute
                    config_reads = 0

                    def observe(statement, *args, **kwargs):
                        nonlocal config_reads
                        sql = str(statement.compile(dialect=postgresql.dialect()))
                        if "FROM agents" in sql and "FOR NO KEY UPDATE" in sql:
                            reached.set()
                        result = execute(statement, *args, **kwargs)
                        if "SELECT id, config FROM agent_tools" in sql:
                            config_reads += 1
                            if config_reads == 2 and not reached.is_set():
                                reached.set()
                                await_only(resume.wait())
                        return result

                    monkeypatch.setattr(bind, "execute", observe)
                    return migrate_agent_tool_config_rows(bind, provider=rotating, apply=True)

                result = await connection.run_sync(apply)
                await operator.commit()
                return result

        task = asyncio.create_task(rotate())
        try:
            await asyncio.wait_for(reached.wait(), 10)
            await db.commit()
        finally:
            resume.set()
        result = await asyncio.wait_for(task, 15)
        assert result["rewritten_rows"] >= 1
        raw = await db.scalar(text("SELECT config FROM agent_tools WHERE id = :id"), {"id": assignment_id})
        assert decrypt_agent_tool_config(raw, provider=rotating) == {"api_key": ""}
