"""Audit enum rollback must remain readable by the previous application model."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Enum, select, text, type_coerce
from sqlalchemy.exc import DBAPIError

from app.database import pin_rls_tenant_context
from app.models.activity_log import AgentActivityLog
from tests.integration import conftest as integration
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    # DDL cases own fresh databases; unrelated audit history is never removed.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


def migrate(url, action, target, root):
    result = subprocess.run([sys.executable, "-B", "-m", "alembic", action, target],
        cwd=Path(__file__).resolve().parents[2], env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True, timeout=60)
    (root / f"enum-{action}-{target}.log").write_text(result.stdout + result.stderr)
    return result


def legacy_action_type():
    return Enum(*(value for value in AgentActivityLog.action_type.type.enums if value != "action_preflight"),
        name="activity_action_enum")


async def test_empty_enum_rollback_blocks_new_labels_until_reupgrade(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        old = AgentActivityLog(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            owner_user_id=seed["user_id"], root_session_id=seed["session_id"],
            action_type="tool_call", summary="Existing audit record", detail_json={"original": True})
        db.add(old)
        await db.commit()
        old_id = old.id
    downgraded = migrate(migrated_pg_url, "downgrade", "private_browser_control_1023", tmp_path)
    assert downgraded.returncode == 0, downgraded.stderr
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        assert await db.scalar(select(type_coerce(AgentActivityLog.action_type, legacy_action_type())).where(
            AgentActivityLog.id == old_id)) == "tool_call"
        with pytest.raises(DBAPIError) as denied:
            async with db.begin_nested():
                db.add(AgentActivityLog(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    action_type="action_preflight", summary="A previous writer cannot add an unreadable label"))
                await db.flush()
        assert getattr(denied.value.orig.__cause__, "constraint_name", None) == "ck_agent_activity_logs_legacy_action_type"
        assert (await db.get(AgentActivityLog, old_id)).detail_json == {"original": True}
    restored = migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert restored.returncode == 0, restored.stderr
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        current = AgentActivityLog(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            action_type="action_preflight", summary="Restored reader and writer")
        db.add(current)
        await db.flush()
        current_id = current.id
        assert await db.scalar(select(AgentActivityLog.action_type).where(AgentActivityLog.id == current_id)) == "action_preflight"
        await db.commit()


async def test_populated_enum_rollback_preserves_evidence_and_refuses_an_incompatible_reader(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        row = AgentActivityLog(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            owner_user_id=seed["user_id"], root_session_id=seed["session_id"], action_type="action_preflight",
            summary="Required preflight audit evidence", detail_json={"retained": "complete"})
        db.add(row)
        await db.commit()
        row_id = row.id
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        with pytest.raises(LookupError):
            await db.scalar(select(type_coerce(AgentActivityLog.action_type, legacy_action_type())).where(
                AgentActivityLog.id == row_id))
    refused = migrate(migrated_pg_url, "downgrade", "private_browser_control_1023", tmp_path)
    assert refused.returncode != 0 and "Retain action_preflight audit evidence" in refused.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "tool_preflight_audit_1023"
        retained = await db.get(AgentActivityLog, row_id)
        assert retained.action_type == "action_preflight" and retained.detail_json == {"retained": "complete"}
