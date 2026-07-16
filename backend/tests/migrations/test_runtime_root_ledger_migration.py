"""Static and frozen-contract guards for the Group 3 root ledger revision."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "runtime_root_ledger_0716.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("runtime_root_ledger_0716", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_root_ledger_revision_is_additive_and_reinstalls_frozen_contract() -> None:
    module = _load_migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert module.revision == "runtime_root_ledger_0716"
    assert module.down_revision == "session_v2_projection_epoch_0716"
    assert module.RUNTIME_ROOT_LEDGER_TABLES == ("runtime_root_items",)
    assert "migration_snapshots.runtime_root_ledger_contract_0716" in source
    assert "build_previous_session_event_contract_function_sql" in source
    assert "app.services.session_event_contract" not in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "REVOKE EXECUTE" in source


def test_frozen_contract_accepts_parent_or_bound_child_session_only() -> None:
    from migration_snapshots.runtime_root_ledger_contract_0716 import (
        build_session_event_contract_function_sql,
    )

    sql = build_session_event_contract_function_sql()

    assert "legacy_run.parent_session_id=NEW.session_id::text" in sql
    assert "legacy_run.parent_agent_id=NEW.agent_id" in sql
    assert "legacy_run.child_session_id=NEW.session_id::text" in sql
    assert "COALESCE(legacy_run.child_agent_id, legacy_run.parent_agent_id)=NEW.agent_id" in sql
    assert "legacy_run.tenant_id=NEW.tenant_id" in sql


async def test_runtime_root_ledger_upgrade_has_exact_columns_constraints_and_forced_rls(
    revision_parent_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("runtime_root_items")
                    },
                    "uniques": {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_unique_constraints("runtime_root_items")
                    },
                }
            )
            rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE oid = 'runtime_root_items'::regclass"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert "runtime_root_items" in schema["tables"]
    assert {
        "tenant_id",
        "root_runtime_task_id",
        "runtime_task_id",
        "intent_key",
        "work_type",
        "target_ref",
        "path_json",
        "state",
        "admission_disposition",
        "budget_reservation_key",
        "approval_ref",
        "result_refs_json",
        "recovery_claimed_by",
        "recovery_claim_expires_at",
        "recovery_attempt_count",
        "next_recovery_at",
        "version",
    } <= schema["columns"]
    assert "uq_runtime_root_items_root_intent" in schema["uniques"]
    assert rls == (True, True)


async def test_runtime_task_legacy_event_authority_pairs_parent_and_child_agent_sessions(
    revision_parent_migrated_pg_url: str,
) -> None:
    from uuid import uuid4

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id, user_id, parent_agent_id, child_agent_id, parent_session_id, child_session_id, run_id = (
        uuid4() for _ in range(7)
    )
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="Child Authority", slug=f"child-authority-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"child-authority-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@child-authority.test",
                    password_hash="x",
                    display_name="Child Authority",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add_all(
                [
                    Agent(
                        id=parent_agent_id,
                        tenant_id=tenant_id,
                        name="Parent Agent",
                        creator_id=user_id,
                        sponsor_user_id=user_id,
                    ),
                    Agent(
                        id=child_agent_id,
                        tenant_id=tenant_id,
                        name="Child Agent",
                        creator_id=user_id,
                        sponsor_user_id=user_id,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ChatSession(
                        id=parent_session_id,
                        tenant_id=tenant_id,
                        agent_id=parent_agent_id,
                        user_id=user_id,
                    ),
                    ChatSession(
                        id=child_session_id,
                        tenant_id=tenant_id,
                        agent_id=child_agent_id,
                        user_id=user_id,
                        parent_session_id=parent_session_id,
                        root_session_id=parent_session_id,
                    ),
                ]
            )
            db.add(
                RuntimeTask(
                    id=run_id,
                    tenant_id=tenant_id,
                    task_type="delegation",
                    status="running",
                    parent_agent_id=parent_agent_id,
                    child_agent_id=child_agent_id,
                    parent_session_id=str(parent_session_id),
                    child_session_id=str(child_session_id),
                    writer_generation=1,
                )
            )
            await db.commit()

        insert_event = text(
            """
            INSERT INTO chat_transcript_events(
              id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,
              item_type,item_status,actor_type,event_type,visibility_scope,
              listed_surface,metadata_json,projection_status,projection_attempts
            ) VALUES (
              :id,:sequence,:tenant_id,:agent_id,:session_id,:run_id,1,
              'event','succeeded','runtime','legacy_child_authority',
              'operator','ops','{}'::jsonb,'not_requested',0
            )
            """
        )

        async def write(*, agent_id, session_id, sequence: int) -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    insert_event,
                    {
                        "id": uuid4(),
                        "sequence": sequence,
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "run_id": run_id,
                    },
                )

        await write(agent_id=parent_agent_id, session_id=parent_session_id, sequence=1)
        await write(agent_id=child_agent_id, session_id=child_session_id, sequence=1)
        with pytest.raises(DBAPIError):
            await write(agent_id=parent_agent_id, session_id=child_session_id, sequence=2)
        with pytest.raises(DBAPIError):
            await write(agent_id=child_agent_id, session_id=parent_session_id, sequence=2)
    finally:
        await engine.dispose()
