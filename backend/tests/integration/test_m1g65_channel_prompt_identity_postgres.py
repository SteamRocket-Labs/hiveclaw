"""Prompt identity upgrades preserve terminal deliveries and every provider receipt."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text


async def test_populated_prompt_identity_upgrade_retains_receipts_and_legacy_dedupe(owner_sessionmaker):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/channel_prompt_identity_1018.py"
    spec = importlib.util.spec_from_file_location("channel_prompt_identity", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "prompt_upgrade_" + uuid4().hex

    def check(connection):
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(text("CREATE TABLE channel_delivery_outbox (id integer PRIMARY KEY, tenant_id uuid, "
            "runtime_task_id uuid, delivery_kind text, target_hash text, receipt text, "
            "CONSTRAINT uq_channel_delivery_outbox_runtime_target "
            "UNIQUE (tenant_id, runtime_task_id, delivery_kind, target_hash))"))
        for index, kind in enumerate(("terminal_result", "interactive_prompt"), 1):
            connection.execute(text("INSERT INTO channel_delivery_outbox VALUES "
                "(:id, :tenant, :run, :kind, 'target', :receipt)"),
                {"id": index, "tenant": tenant, "run": run, "kind": kind, "receipt": f"provider-receipt-{index}"})
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text("SELECT receipt, prompt_key FROM channel_delivery_outbox ORDER BY id")).all() == [
            ("provider-receipt-1", ""), ("provider-receipt-2", "")]
        for index in (3, 4):
            connection.execute(text("INSERT INTO channel_delivery_outbox VALUES "
                "(:id, :tenant, :run, 'interactive_prompt', 'target', :receipt, :prompt_key)"),
                {"id": index, "tenant": tenant, "run": run, "receipt": f"provider-receipt-{index}", "prompt_key": str(uuid4())})
        connection.execute(text("INSERT INTO channel_delivery_outbox "
            "(id, tenant_id, runtime_task_id, delivery_kind, target_hash, receipt) VALUES "
            "(5, :tenant, :run, 'terminal_result', 'target', 'duplicate') "
            "ON CONFLICT ON CONSTRAINT uq_channel_delivery_outbox_runtime_target DO NOTHING"), {"tenant": tenant, "run": run})
        migration.downgrade()
        migration.upgrade()
        assert connection.execute(text("SELECT receipt FROM channel_delivery_outbox ORDER BY id")).scalars().all() == [
            f"provider-receipt-{index}" for index in range(1, 5)]
    tenant, run = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        await (await db.connection()).run_sync(check)
        await db.rollback()
