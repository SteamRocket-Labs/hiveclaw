"""Context recovery must leave the running worker's lease renewable."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("stage", ["history", "reclaimed_history", "restart", "superseded", "concurrent_control"])
async def test_context_loading_does_not_block_claim_renewal(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, stage,
):
    import app.database as database
    import app.services.session_semantic_history as semantic
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError, renew_current_runtime_task_lease, reset_runtime_task_fence, set_runtime_task_fence,
    )

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        task.claim_version = 1
        task.claimed_by = "context-lease-worker"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        task.attempt_count = 1 if stage == "history" else 2
        if stage != "history":
            task.metadata_json = {**task.metadata_json, "reclaimed_expired_claim": True}
        await db.commit()
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime, "_async_session", app_user_sessionmaker)
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause(loader, db, *args, **kwargs):
        # A normal read autoflushes pending state before a slow recovery read.
        await db.scalar(select(RuntimeTask.id).where(RuntimeTask.id == seed["run_id"]))
        reached.set()
        await release.wait()
        return await loader(db, *args, **kwargs)

    if stage in {"history", "reclaimed_history"}:
        original = semantic.load_session_semantic_history

        async def held_history(db, **kwargs):
            return await pause(original, db, **kwargs)

        monkeypatch.setattr(semantic, "load_session_semantic_history", held_history)
    else:
        original = runtime._session_restart_resume_history

        async def held_restart(db, task):
            return await pause(original, db, task)

        monkeypatch.setattr(runtime, "_session_restart_resume_history", held_restart)

    token = set_runtime_task_fence(task_id=seed["run_id"], claim_version=1, worker_id="context-lease-worker")
    loading = asyncio.create_task(runtime._load_runtime_context(seed["run_id"]))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        renewed = await asyncio.wait_for(renew_current_runtime_task_lease(lease_seconds=60), timeout=1)
        assert renewed > datetime.now(timezone.utc)
        if stage == "superseded":
            async with owner_sessionmaker() as db:
                await db.execute(update(RuntimeTask).where(RuntimeTask.id == seed["run_id"]).values(
                    claim_version=2, claimed_by="replacement-worker", metadata_json={"replacement_marker": True},
                ))
                await db.commit()
            release.set()
            with pytest.raises(StaleRuntimeTaskFenceError):
                await loading
            async with owner_sessionmaker() as db:
                assert await db.scalar(select(RuntimeTask.metadata_json).where(
                    RuntimeTask.id == seed["run_id"],
                )) == {"replacement_marker": True}
            return
        if stage == "concurrent_control":
            async with owner_sessionmaker() as db:
                current = await db.get(RuntimeTask, seed["run_id"])
                current.metadata_json = {**current.metadata_json, "concurrent_control_marker": True}
                await db.commit()
        release.set()
        task, agent, user, model, _fallback, _history, session = await loading
        assert (task.id, agent.id, user.id, session.id) == (
            seed["run_id"], seed["agent_id"], seed["user_id"], seed["session_id"],
        )
        assert model.model == "fake-4.1"
        assert task.metadata_json["session_semantic_history"]["schema"]
        if stage == "concurrent_control":
            assert task.metadata_json["concurrent_control_marker"] is True
    finally:
        release.set()
        if not loading.done():
            loading.cancel()
        await asyncio.gather(loading, return_exceptions=True)
        reset_runtime_task_fence(token)
