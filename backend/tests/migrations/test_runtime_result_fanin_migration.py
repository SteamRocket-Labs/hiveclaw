from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User

from tests.migrations.conftest import (
    _alembic_downgrade,
    _alembic_upgrade,
    _async_url,
    _bootstrap_current_head,
    insert_chat_session_at_schema_revision,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "runtime_result_fanin_0717.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("runtime_result_fanin_0717", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_result_fanin_revision_contract():
    migration = _load_migration()
    assert migration.revision == "runtime_result_fanin_0717"
    assert migration.down_revision == "runtime_root_ledger_0716"
    assert set(migration.RUNTIME_RESULT_TABLES) == {
        "runtime_result_objects",
        "runtime_result_mailbox_cursors",
        "runtime_result_integration_pages",
    }


@pytest_asyncio.fixture(scope="module")
async def runtime_result_parent_pg_url(pg_container):
    database = "runtimeresultfaninparent"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database}"])
    if code != 0:
        pytest.fail(f"failed to create {database}: {output}")
    url = make_url(_async_url(pg_container)).set(database=database).render_as_string(hide_password=False)
    _bootstrap_current_head(url)
    _alembic_downgrade(url, "runtime_root_ledger_0716")
    yield url


@pytest.mark.usefixtures("runtime_result_parent_pg_url")
async def test_runtime_result_upgrade_backfills_legacy_payload_losslessly(runtime_result_parent_pg_url: str):
    engine = create_async_engine(runtime_result_parent_pg_url)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    second_session_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    second_outbox_id = uuid.uuid4()
    source_run_id = str(uuid.uuid4())
    summary = "legacy decisive tail:" + ("证据" * 30_000)
    artifacts = [{"artifact_id": "artifact-1", "path": "workspace/report.md"}]
    metadata = {
        "budget_run_id": str(uuid.uuid4()),
        "model_context": {"complete": True, "tail": "decisive"},
        "private_result_detail": "must move out of outbox",
    }
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        db.add(Tenant(id=tenant_id, name="tenant", slug=f"tenant-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"user-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@example.test",
                password_hash="x",
                display_name="Runtime Result Migration User",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                sponsor_user_id=user_id,
                name="agent",
                role_description="migration fixture",
            )
        )
        await db.flush()
        await insert_chat_session_at_schema_revision(
            db,
            id=session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            title="session",
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
        await insert_chat_session_at_schema_revision(
            db,
            id=second_session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            title="second session",
            source_channel="web",
            session_kind="human_chat",
            actor_type="user",
            runtime_source="web_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
        await db.commit()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO runtime_notification_outbox "
                "(id, tenant_id, source_kind, source_run_id, parent_session_id, parent_agent_id, parent_user_id, "
                "terminal_status, task_type, summary, delivery_mode, artifacts_json, metadata_json, payload_rank, status) "
                "VALUES (:id, :tenant_id, 'subagent', :source_run_id, :session_id, :agent_id, :user_id, "
                "'completed', 'subagent', :summary, 'parent_continuation', CAST(:artifacts AS jsonb), "
                "CAST(:metadata AS jsonb), 100, 'pending')"
            ),
            {
                "id": outbox_id,
                "tenant_id": tenant_id,
                "source_run_id": source_run_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "summary": summary,
                "artifacts": json.dumps(artifacts),
                "metadata": json.dumps(metadata),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO runtime_notification_outbox "
                "(id, tenant_id, source_kind, source_run_id, parent_session_id, parent_agent_id, parent_user_id, "
                "terminal_status, task_type, summary, delivery_mode, artifacts_json, metadata_json, payload_rank, status) "
                "VALUES (:id, :tenant_id, 'subagent', :source_run_id, :session_id, :agent_id, :user_id, "
                "'completed', 'subagent', :summary, 'parent_continuation', CAST(:artifacts AS jsonb), "
                "CAST(:metadata AS jsonb), 100, 'pending')"
            ),
            {
                "id": second_outbox_id,
                "tenant_id": tenant_id,
                "source_run_id": source_run_id,
                "session_id": second_session_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "summary": summary,
                "artifacts": json.dumps(artifacts),
                "metadata": json.dumps(metadata),
            },
        )
    await engine.dispose()

    _alembic_upgrade(runtime_result_parent_pg_url, "head")

    engine = create_async_engine(runtime_result_parent_pg_url)
    async with engine.connect() as connection:
        schema = await connection.run_sync(
            lambda sync_connection: {
                "tables": set(inspect(sync_connection).get_table_names()),
                "outbox_columns": {
                    column["name"] for column in inspect(sync_connection).get_columns("runtime_notification_outbox")
                },
            }
        )
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT outbox.result_ref, outbox.result_sha256, outbox.result_size_bytes, "
                        "outbox.mailbox_sequence, outbox.metadata_json, result.payload_bytes, result.sha256 "
                        "FROM runtime_notification_outbox outbox "
                        "JOIN runtime_result_objects result ON result.id=outbox.result_object_id "
                        "WHERE outbox.id=:id"
                    ),
                    {"id": outbox_id},
                )
            )
            .mappings()
            .one()
        )
        shared_result_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT outbox.id, outbox.result_object_id, outbox.result_ref "
                        "FROM runtime_notification_outbox outbox "
                        "WHERE outbox.id = ANY(:ids) ORDER BY outbox.id"
                    ),
                    {"ids": [outbox_id, second_outbox_id]},
                )
            )
            .mappings()
            .all()
        )
        result_object_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM runtime_result_objects "
                    "WHERE tenant_id=:tenant_id AND source_kind='subagent' AND source_run_id=:source_run_id"
                ),
                {"tenant_id": tenant_id, "source_run_id": source_run_id},
            )
        ).scalar_one()
        rls_rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:tables) ORDER BY relname"
                ),
                {"tables": list(_load_migration().RUNTIME_RESULT_TABLES)},
            )
        ).all()
    await engine.dispose()

    assert set(_load_migration().RUNTIME_RESULT_TABLES) <= schema["tables"]
    assert "summary" not in schema["outbox_columns"]
    assert "artifacts_json" not in schema["outbox_columns"]
    assert row["mailbox_sequence"] == 1
    assert row["result_ref"].endswith(f"/{row['result_sha256']}")
    assert row["result_sha256"] == row["sha256"]
    assert row["result_size_bytes"] == len(bytes(row["payload_bytes"]))
    payload = json.loads(bytes(row["payload_bytes"]).decode("utf-8"))
    assert payload == {
        "artifacts": artifacts,
        "metadata": metadata,
        "schema": "hive.runtime_result.v1",
        "summary": summary,
    }
    assert row["metadata_json"] == {"budget_run_id": metadata["budget_run_id"]}
    assert len(shared_result_rows) == 2
    assert len({result["result_object_id"] for result in shared_result_rows}) == 1
    assert len({result["result_ref"] for result in shared_result_rows}) == 1
    assert result_object_count == 1
    assert len(rls_rows) == 3
    assert all(enabled and forced for _, enabled, forced in rls_rows)

    _alembic_downgrade(runtime_result_parent_pg_url, "runtime_root_ledger_0716")
    engine = create_async_engine(runtime_result_parent_pg_url)
    async with engine.connect() as connection:
        downgraded_schema = await connection.run_sync(
            lambda sync_connection: {
                "tables": set(inspect(sync_connection).get_table_names()),
                "outbox_columns": {
                    column["name"] for column in inspect(sync_connection).get_columns("runtime_notification_outbox")
                },
            }
        )
        restored_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT id, summary, artifacts_json, metadata_json "
                        "FROM runtime_notification_outbox WHERE id = ANY(:ids) ORDER BY id"
                    ),
                    {"ids": [outbox_id, second_outbox_id]},
                )
            )
            .mappings()
            .all()
        )
    await engine.dispose()

    assert "summary" in downgraded_schema["outbox_columns"]
    assert "artifacts_json" in downgraded_schema["outbox_columns"]
    assert "result_object_id" not in downgraded_schema["outbox_columns"]
    assert set(_load_migration().RUNTIME_RESULT_TABLES).isdisjoint(downgraded_schema["tables"])
    assert len(restored_rows) == 2
    assert all(restored["summary"] == summary for restored in restored_rows)
    assert all(restored["artifacts_json"] == artifacts for restored in restored_rows)
    assert all(restored["metadata_json"] == metadata for restored in restored_rows)
