"""A recoverable compute identity must survive upgrade and refused rollback."""

import json

import pytest
from sqlalchemy import text

from app.database import pin_rls_tenant_context
from app.models.execution_environment import ExecutionEnvironment
from tests.integration import conftest as integration
from tests.integration.test_m2ad_preflight_migration_postgres import migrate
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_compute_column_preserves_rows_and_refuses_unresolved_resource_loss(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    parent = "ingress_event_namespace_1023"
    result = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert result.returncode == 0, result.stderr
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        await db.execute(text("""INSERT INTO execution_environments
            (id,tenant_id,agent_id,provider_key,desired_state,observed_state,generation,revision,fence_counter)
            VALUES (:id,:tenant,:agent,'local_os_sandbox','ready','ready',1,1,0)"""),
            {"id": seed["agent_id"], "tenant": seed["tenant_id"], "agent": seed["agent_id"]})
        await db.commit()
        before = await db.scalar(text("SELECT to_jsonb(e) FROM execution_environments e"))
    result = migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert result.returncode == 0, result.stderr
    binding = {"schema": "hive.compute-binding.v1", "state": "creation_unknown", "resource_id": None,
        "creation_lease_id": "retained-unconfirmed-creation"}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        after = await db.scalar(text("SELECT to_jsonb(e) FROM execution_environments e"))
        assert {key: value for key, value in after.items() if key != "resource_binding"} == before
        assert after["resource_binding"] is None
        await db.execute(text("UPDATE execution_environments SET resource_binding=CAST(:binding AS json)"),
            {"binding": json.dumps(binding)})
        await db.commit()
    refused = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert refused.returncode != 0 and "Retain or reconcile compute resource bindings" in refused.stderr
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        assert await db.scalar(text("SELECT resource_binding FROM execution_environments")) == binding
        assert (await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid='execution_environments'::regclass"))).one() == (True, True)
        environment = await db.get(ExecutionEnvironment, seed["agent_id"])
        environment.resource_binding = None
        await db.commit()
    result = migrate(migrated_pg_url, "downgrade", parent, tmp_path)
    assert result.returncode == 0, result.stderr
    result = migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert result.returncode == 0, result.stderr
