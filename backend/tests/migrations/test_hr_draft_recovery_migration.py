from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hr_draft_recovery_0712.py"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_hr_draft_recovery_migration_backfills_only_unconfirmed_preview_ttl() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "hr_draft_recovery_0712"' in source
    assert 'down_revision = "tenant_null_semantics_0712"' in source
    assert "status = 'awaiting_confirmation'" in source
    assert "expires_at IS NULL" in source
    assert "INTERVAL '7 days'" in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "def downgrade" in source


async def test_hr_draft_recovery_migration_really_backfills_and_preserves_secure_downgrade(pg_container) -> None:
    from app.models.chat_session import ChatSession
    from app.models.hr_creation import HrCreationDraft
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade, insert_agent_at_schema_revision

    database_name = f"hrdraft_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    # Empty databases use metadata bootstrap and are stamped at the current
    # head regardless of the requested target. Rewind one revision so the
    # release upgrade below executes this migration instead of no-oping.
    _alembic_upgrade(database_url, "head")
    _alembic(database_url, "downgrade", "tenant_null_semantics_0712")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    hr_agent_id = uuid.uuid4()
    session_ids = [uuid.uuid4(), uuid.uuid4()]
    draft_ids = [uuid.uuid4(), uuid.uuid4()]
    created_at = datetime.now(timezone.utc) - timedelta(days=10)
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(
                Tenant(
                    id=tenant_id,
                    name="HR draft migration",
                    slug=f"hr-draft-{tenant_id.hex[:8]}",
                )
            )
            db.add(
                User(
                    id=user_id,
                    username=f"hr-draft-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@hr-draft.test",
                    password_hash="x",
                    display_name="Owner",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            await insert_agent_at_schema_revision(
                db,
                agent_id=hr_agent_id,
                tenant_id=tenant_id,
                name="__system_hr__",
                creator_id=user_id,
                sponsor_user_id=user_id,
                owner_user_id=user_id,
                agent_class="internal_system",
                status="running",
            )
            for session_id in session_ids:
                db.add(
                    ChatSession(
                        id=session_id,
                        agent_id=hr_agent_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        source_channel="web",
                    )
                )
            await db.flush()
            for draft_id, session_id, status in zip(
                draft_ids,
                session_ids,
                ("awaiting_confirmation", "confirmed"),
                strict=True,
            ):
                db.add(
                    HrCreationDraft(
                        id=draft_id,
                        tenant_id=tenant_id,
                        hr_agent_id=hr_agent_id,
                        session_id=session_id,
                        requested_by_user_id=user_id,
                        status=status,
                        blueprint_version=1,
                        blueprint_hash="sha256:migration",
                        blueprint_json={"name": "Researcher"},
                        preview_json={},
                        created_at=created_at,
                    )
                )
            await db.commit()
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, status, created_at, expires_at FROM hr_creation_drafts WHERE id = ANY(:ids) ORDER BY id"
                    ),
                    {"ids": draft_ids},
                )
            ).all()
        expires_by_id = {row.id: row.expires_at for row in rows}
        assert expires_by_id[draft_ids[0]] == created_at + timedelta(days=7), rows
        assert expires_by_id[draft_ids[1]] is None
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "tenant_null_semantics_0712")
    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            preserved = await connection.scalar(
                text("SELECT expires_at FROM hr_creation_drafts WHERE id = :id"),
                {"id": draft_ids[0]},
            )
        assert preserved == created_at + timedelta(days=7)
    finally:
        await engine.dispose()
