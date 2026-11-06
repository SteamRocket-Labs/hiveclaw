"""A4m — an allow_session grant must not overwrite the list-removal marker.

G1 review round 2: the grant merge read transcript_metadata_json without the
session's transcript lock and wrote the whole field back, so a concurrent
list removal (advisory lock, then FOR UPDATE, per chat_sessions.delete_session)
could commit its removed_from_list_by marker between the grant's read and its
UPDATE — the grant's stale copy then resurrected the conversation in the
member's list. Both orders are pinned here on two real connections.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.services.chat_transcript import lock_transcript_session
from tests.services.test_session_permission_runtime import (
    _authority,
    _install_successful_executor,
    _seed_waiting_batch,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_BLOCKED_LOCK_SQL = """
SELECT EXISTS (
    SELECT 1 FROM pg_locks AS l
    WHERE NOT l.granted
      AND (
            l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
            OR l.pid IN (SELECT pid FROM pg_stat_activity WHERE datname = current_database())
      )
)
"""


async def _wait_until_someone_waits_on_a_lock(migrated_pg_url: str, *, stop: asyncio.Event | None = None,
                                              timeout: float = 30.0) -> bool:
    """True once a backend of this test database waits on an ungranted lock.

    The pre-fix grant queues its UPDATE on the removal's row lock; the fixed
    grant waits on the removal's transcript advisory. pg_locks shows both, so
    this is the version-agnostic signal that the concurrent writer has truly
    reached the contested row or advisory. Returns False if ``stop`` fires
    first (used when the pre-fix side holds no lock at all).
    """
    url = make_url(migrated_pg_url)
    conn = await asyncpg.connect(user=url.username or "rocky243", database=url.database,
                                 host=(url.query or {}).get("host") or "/tmp")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            if stop is not None and stop.is_set():
                return False
            if await conn.fetchval(_BLOCKED_LOCK_SQL):
                return True
            if loop.time() > deadline:
                raise AssertionError("no backend of this test database was seen waiting on a lock")
            await asyncio.sleep(0.05)
    finally:
        await conn.close()


async def _remove_from_own_list(tenant_id: uuid.UUID, session_id: uuid.UUID, member_id: uuid.UUID,
                                sessionmaker, *, on_locked=None) -> None:
    """The canonical G1 list-removal writer (chat_sessions.delete_session):
    transcript advisory lock, FOR UPDATE re-select, merge removed_from_list_by."""
    async with tenant_scoped_session(tenant_id, session_factory=sessionmaker) as db:
        await lock_transcript_session(db, session_id=session_id)
        row = (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if on_locked is not None:
            await on_locked()
        metadata = dict(row.transcript_metadata_json or {})
        removed = dict(metadata.get("removed_from_list_by") or {})
        removed[str(member_id)] = datetime.now(timezone.utc).isoformat()
        metadata["removed_from_list_by"] = removed
        row.transcript_metadata_json = metadata
        await db.commit()


async def _approve_allow_session(seed, sessionmaker, *, on_before_metadata_read=None, commit_gate=None):
    """Resolve a pending permission as allow_session on its own connection.

    The resolver's post-commit ``db.refresh(invocation)`` is the last await
    before it reads the metadata it merges the grant into; wrapping that call
    (and the first commit after it, which persists the grant) gives the test
    the two version-agnostic gates the race needs.
    """
    from app.services.session_permission_runtime import resolve_session_tool_permission

    async with tenant_scoped_session(seed.tenant_id, session_factory=sessionmaker) as db:
        real_refresh, real_commit = db.refresh, db.commit
        armed = {"armed": False}

        async def gated_refresh(obj, *args, **kwargs):
            if getattr(obj, "id", None) == seed.invocation_ids[0]:
                if on_before_metadata_read is not None:
                    await on_before_metadata_read()
                if commit_gate is not None:
                    armed["armed"] = True
            return await real_refresh(obj, *args, **kwargs)

        async def gated_commit():
            if armed["armed"]:
                armed["armed"] = False
                commit_gate[0].set()
                await asyncio.wait_for(commit_gate[1].wait(), 30)
            return await real_commit()

        db.refresh = gated_refresh
        if commit_gate is not None:
            db.commit = gated_commit
        _user, authority = await _authority(db, seed)
        return await resolve_session_tool_permission(
            db, authority=authority, permission_request_id=seed.permission_ids[0], decision="allow_session",
        )


async def _assert_both_writes_survived(seed, sessionmaker) -> None:
    async with tenant_scoped_session(seed.tenant_id, session_factory=sessionmaker) as db:
        row = await db.get(ChatSession, seed.session_id)
        metadata = dict(row.transcript_metadata_json or {})
    removed = metadata.get("removed_from_list_by") or {}
    grants = [item for item in (metadata.get("session_permission_grants") or []) if isinstance(item, dict)]
    assert str(seed.user_id) in removed, f"the list-removal marker was overwritten: {metadata}"
    assert any(grant.get("tool_name") == "read_file" and grant.get("scope") == "session"
               and grant.get("status") == "active" for grant in grants), f"the session grant was lost: {metadata}"


async def test_removal_holding_the_row_lock_survives_concurrent_allow_session_grant(
    owner_sessionmaker, app_user_sessionmaker, migrated_pg_url, monkeypatch,
):
    """The removal holds the session row lock while an allow_session resolves.

    The grant resolver is parked just before it reads the metadata it merges
    into; the removal then takes the transcript advisory plus the row lock and
    reads the not-yet-marked metadata. When the grant proceeds, the pre-fix
    code reads outside the lock and its UPDATE queues behind the removal —
    landing last with a marker-free copy. With the fix the grant waits on the
    advisory, re-reads the locked row and merges into the marked metadata.
    """
    seed = await _seed_waiting_batch(owner_sessionmaker)
    _install_successful_executor(monkeypatch)

    read_entered = asyncio.Event()
    removal_locked = asyncio.Event()
    grant_pending = asyncio.Event()

    async def before_metadata_read():
        read_entered.set()
        await asyncio.wait_for(removal_locked.wait(), 30)

    async def on_locked():
        removal_locked.set()
        await asyncio.wait_for(grant_pending.wait(), 30)

    grant_task = asyncio.create_task(_approve_allow_session(
        seed, app_user_sessionmaker, on_before_metadata_read=before_metadata_read))
    await asyncio.wait_for(read_entered.wait(), 30)
    removal_task = asyncio.create_task(_remove_from_own_list(
        seed.tenant_id, seed.session_id, seed.user_id, app_user_sessionmaker, on_locked=on_locked))
    await asyncio.wait_for(removal_locked.wait(), 30)
    assert await _wait_until_someone_waits_on_a_lock(migrated_pg_url), "the grant never contested the removal"
    grant_pending.set()

    await asyncio.wait_for(removal_task, 30)
    receipt = await asyncio.wait_for(grant_task, 60)
    assert receipt.status == "resolved", receipt
    await _assert_both_writes_survived(seed, app_user_sessionmaker)


async def test_allow_session_holding_its_merge_survives_concurrent_list_removal(
    owner_sessionmaker, app_user_sessionmaker, migrated_pg_url, monkeypatch,
):
    """The reverse order: the grant's merge is pending while the removal runs.

    The resolver is paused right before the commit that persists the grant —
    holding the transcript advisory and the row lock with the fix, holding
    nothing without it. The removal then resolves: pre-fix it commits its
    marker over the pending grant's marker-free base; with the fix it waits on
    the advisory and merges its marker into the committed grant.
    """
    seed = await _seed_waiting_batch(owner_sessionmaker)
    _install_successful_executor(monkeypatch)

    grant_merged = asyncio.Event()
    release_grant_commit = asyncio.Event()
    removal_done = asyncio.Event()

    async def removal():
        await _remove_from_own_list(seed.tenant_id, seed.session_id, seed.user_id, app_user_sessionmaker)
        removal_done.set()

    grant_task = asyncio.create_task(_approve_allow_session(
        seed, app_user_sessionmaker, commit_gate=(grant_merged, release_grant_commit)))
    await asyncio.wait_for(grant_merged.wait(), 30)
    removal_task = asyncio.create_task(removal())
    # Pre-fix the grant holds no lock, so the removal simply completes;
    # with the fix it parks on the transcript advisory the grant holds.
    await _wait_until_someone_waits_on_a_lock(migrated_pg_url, stop=removal_done)
    release_grant_commit.set()

    await asyncio.wait_for(removal_task, 30)
    receipt = await asyncio.wait_for(grant_task, 60)
    assert receipt.status == "resolved", receipt
    await _assert_both_writes_survived(seed, app_user_sessionmaker)
