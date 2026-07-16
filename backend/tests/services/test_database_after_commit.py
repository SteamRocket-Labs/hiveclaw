from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_after_commit_callback_waits_for_outer_transaction(owner_sessionmaker) -> None:
    from app.database import schedule_after_commit

    called = asyncio.Event()

    async def callback() -> None:
        called.set()

    async with owner_sessionmaker() as db:
        await db.begin()
        schedule_after_commit(db, callback, description="test outer commit")
        async with db.begin_nested():
            pass
        await asyncio.sleep(0)
        assert called.is_set() is False
        await db.commit()

    await asyncio.wait_for(called.wait(), timeout=1.0)


async def test_after_commit_callback_is_discarded_on_outer_rollback(owner_sessionmaker) -> None:
    from app.database import schedule_after_commit

    called = asyncio.Event()

    async def callback() -> None:
        called.set()

    async with owner_sessionmaker() as db:
        await db.begin()
        schedule_after_commit(db, callback, description="test outer rollback")
        await db.rollback()

    await asyncio.sleep(0)
    assert called.is_set() is False


async def test_after_commit_callback_registered_inside_rolled_back_savepoint_is_discarded(
    owner_sessionmaker,
) -> None:
    from app.database import schedule_after_commit

    called = asyncio.Event()

    async def callback() -> None:
        called.set()

    async with owner_sessionmaker() as db:
        await db.begin()
        savepoint = await db.begin_nested()
        assert schedule_after_commit(db, callback, description="rolled back savepoint") is True
        await savepoint.rollback()
        await db.commit()

    await asyncio.sleep(0)
    assert called.is_set() is False


def test_after_commit_registration_degrades_to_durable_sweeper_for_non_async_session() -> None:
    from app.database import schedule_after_commit

    async def callback() -> None:
        raise AssertionError("unsupported session must not dispatch an uncommitted callback")

    assert schedule_after_commit(object(), callback, description="unsupported session") is False
