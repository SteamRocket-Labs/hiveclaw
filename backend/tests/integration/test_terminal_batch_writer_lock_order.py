"""Terminal batch writers must be advisory-first against live opponents.

The shared terminal settlement acquires each task's session advisory, so a
batch caller that takes the RuntimeTask row locks FIRST sits on the
row → advisory side of the global order and deadlocks against any
advisory-first writer of the same session (an in-flight transcript append,
or an advisory-first ordinary UPDATE).  These regressions drive the REAL
batch callers — ``soft_delete_agent`` and root-user offboarding's
``revoke_user_authority`` — interleaved with both opponent kinds at the
caller's genuine first advisory acquisition, and require committed intended
outcomes on both sides (never merely the absence of a deadlock string).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _gate_first_advisory(monkeypatch, caller_task_box: dict, gate: asyncio.Event, release: asyncio.Event):
    """Gate ONLY the caller task's first ``lock_transcript_session`` call.

    Opponents reach the advisory through module-bound imports that resolved
    before this patch (``session_v2_persistence``) or through the saved
    original below, so they are never gated.
    """

    import app.services.chat_transcript as chat_transcript

    original = chat_transcript.lock_transcript_session

    async def gated(db, *, session_id):
        if asyncio.current_task() is caller_task_box.get("task") and not caller_task_box.get("used"):
            caller_task_box["used"] = True
            gate.set()
            # Hold the caller BEFORE its first advisory/row lock and let the
            # opponent run to completion against the unlocked state.
            await asyncio.wait_for(release.wait(), timeout=30.0)
        return await original(db, session_id=session_id)

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", gated)
    return original


async def _append_opponent(seed: dict, owner_sessionmaker, original_lock) -> str:
    """Ordinary in-flight transcript append: advisory → FK key-share on the row."""

    import app.services.session_v2_persistence as persistence

    provider_request_id = f"hive:{seed['run_id']}:round:1:attempt:1"
    async with owner_sessionmaker() as db:
        await original_lock(db, session_id=uuid.UUID(str(seed["session_id"])))
        await persistence.append_session_events(
            db,
            tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"],
            session_id=uuid.UUID(str(seed["session_id"])),
            drafts=[
                persistence.SessionEventDraft(
                    item_id=uuid.uuid5(seed["run_id"], f"lock-order-opponent:{provider_request_id}"),
                    item_kind="hook",
                    lifecycle="started",
                    scope={
                        "level": "run",
                        "session_id": str(seed["session_id"]),
                        "thread_id": str(seed["session_id"]),
                        "turn_id": seed["turn_id"],
                        "run_id": str(seed["run_id"]),
                    },
                    actor={"type": "hook"},
                    payload={"boundary": "Stop", "provider_request_id": provider_request_id},
                )
            ],
        )
        await db.commit()
    return "ok:append"


async def _plain_update_opponent(seed: dict, owner_sessionmaker, original_lock) -> str:
    """Advisory-first ordinary (non-key) UPDATE writer on the same row."""

    async with owner_sessionmaker() as db:
        await original_lock(db, session_id=uuid.UUID(str(seed["session_id"])))
        await db.execute(
            text("update runtime_tasks set result_summary = :s where id = :i").bindparams(
                s="batch-lock-order-opponent", i=seed["run_id"]
            )
        )
        await db.commit()
    return "ok:plain_update"


async def _run_soft_delete_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, *, opponent) -> dict:
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams, _seed_run

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    gate = asyncio.Event()
    release = asyncio.Event()
    caller_task_box: dict[str, Any] = {"task": None, "used": False}
    original_lock = _gate_first_advisory(monkeypatch, caller_task_box, gate, release)

    log: dict[str, Any] = {}

    async def caller() -> None:
        from app.models.agent import Agent
        from app.services.agent_identity_lifecycle import soft_delete_agent

        caller_task_box["task"] = asyncio.current_task()
        try:
            async with owner_sessionmaker() as db:
                agent = await db.get(Agent, seed["agent_id"])
                await soft_delete_agent(db, agent, actor_id=seed["user_id"])
                await db.commit()
            log["caller"] = "committed"
        except Exception as exc:  # noqa: BLE001 - the point of the regression
            log["caller"] = f"{type(exc).__name__}: {str(exc)[:260]}"

    async def opponent_task() -> None:
        await asyncio.wait_for(gate.wait(), timeout=30.0)
        try:
            if opponent == "append":
                log["opponent"] = await _append_opponent(seed, owner_sessionmaker, original_lock)
            else:
                log["opponent"] = await _plain_update_opponent(seed, owner_sessionmaker, original_lock)
        except Exception as exc:  # noqa: BLE001
            log["opponent"] = f"{type(exc).__name__}: {str(exc)[:260]}"
        finally:
            release.set()

    await asyncio.wait_for(asyncio.gather(caller(), opponent_task()), timeout=120)
    return {**log, "seed": seed}


async def _run_revoke_authority_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, *, opponent) -> dict:
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams, _seed_run

    from app.models.user import User

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    actor_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            User(
                id=actor_id,
                username=f"admin-{actor_id.hex[:8]}",
                email=f"{actor_id.hex[:8]}@offboard.test",
                password_hash="x",
                display_name="Offboarding Actor",
                tenant_id=seed["tenant_id"],
            )
        )
        await db.commit()

    gate = asyncio.Event()
    release = asyncio.Event()
    caller_task_box: dict[str, Any] = {"task": None, "used": False}
    original_lock = _gate_first_advisory(monkeypatch, caller_task_box, gate, release)

    log: dict[str, Any] = {}

    async def caller() -> None:
        from app.services.user_offboarding_service import revoke_user_authority

        caller_task_box["task"] = asyncio.current_task()
        try:
            async with owner_sessionmaker() as db:
                target_user = await db.get(User, seed["user_id"])
                actor_user = await db.get(User, actor_id)
                from datetime import UTC, datetime

                await revoke_user_authority(
                    db,
                    target_user=target_user,
                    actor_user=actor_user,
                    now=datetime.now(UTC),
                )
                await db.commit()
            log["caller"] = "committed"
        except Exception as exc:  # noqa: BLE001
            log["caller"] = f"{type(exc).__name__}: {str(exc)[:260]}"

    async def opponent_task() -> None:
        await asyncio.wait_for(gate.wait(), timeout=30.0)
        try:
            if opponent == "append":
                log["opponent"] = await _append_opponent(seed, owner_sessionmaker, original_lock)
            else:
                log["opponent"] = await _plain_update_opponent(seed, owner_sessionmaker, original_lock)
        except Exception as exc:  # noqa: BLE001
            log["opponent"] = f"{type(exc).__name__}: {str(exc)[:260]}"
        finally:
            release.set()

    await asyncio.wait_for(asyncio.gather(caller(), opponent_task()), timeout=120)
    return {**log, "seed": seed}


async def _assert_async(log: dict, owner_sessionmaker, expected_status: str, *, opponent: str) -> None:
    from app.models.runtime_task import RuntimeTask

    assert log.get("caller") == "committed", log
    assert str(log.get("opponent")) == f"ok:{opponent}", log
    seed = log["seed"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        metadata = dict(task.metadata_json or {})
    assert str(task.status) == expected_status, task.status
    assert task.claimed_by is None and task.claim_expires_at is None
    assert int(task.claim_version or 0) >= 2
    assert str(metadata.get("terminal_committed_status") or "") == expected_status, metadata
    assert str(metadata.get("terminal_execution_fence_ref") or "").startswith("runtime-task-terminal:")


async def test_soft_delete_agent_vs_transcript_append(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    log = await _run_soft_delete_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, opponent="append")
    print(f"BATCH-LOCK soft-delete/append {log['caller']} | {log['opponent']}")
    await _assert_async(log, owner_sessionmaker, "killed", opponent="append")


async def test_soft_delete_agent_vs_advisory_first_plain_update(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    log = await _run_soft_delete_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, opponent="plain_update")
    print(f"BATCH-LOCK soft-delete/plain-update {log['caller']} | {log['opponent']}")
    await _assert_async(log, owner_sessionmaker, "killed", opponent="plain_update")


async def test_revoke_user_authority_vs_transcript_append(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    log = await _run_revoke_authority_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, opponent="append")
    print(f"BATCH-LOCK offboarding/append {log['caller']} | {log['opponent']}")
    await _assert_async(log, owner_sessionmaker, "needs_reconciliation", opponent="append")


async def test_revoke_user_authority_vs_advisory_first_plain_update(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    log = await _run_revoke_authority_vs_opponent(owner_sessionmaker, monkeypatch, tmp_path, opponent="plain_update")
    print(f"BATCH-LOCK offboarding/plain-update {log['caller']} | {log['opponent']}")
    await _assert_async(log, owner_sessionmaker, "needs_reconciliation", opponent="plain_update")


async def test_generic_terminal_update_vs_transcript_append(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    """The generic terminal writer must also be advisory-first.

    ``runtime_task_service.update_runtime_task_record`` is the 45-call-site
    surface used by the runtime worker, A2A orchestrator, subagent runs,
    long tasks, the heartbeat sweep and the trigger daemon; CC reproduced a
    real ``DeadlockDetectedError`` whose victim was an in-flight transcript
    append of the same session. Both sides must commit intended outcomes.
    """

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams, _seed_run

    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_service import update_runtime_task_record

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    gate = asyncio.Event()
    release = asyncio.Event()
    caller_task_box: dict[str, Any] = {"task": None, "used": False}
    original_lock = _gate_first_advisory(monkeypatch, caller_task_box, gate, release)

    log: dict[str, Any] = {}

    async def caller() -> None:
        caller_task_box["task"] = asyncio.current_task()
        try:
            log["caller"] = (
                "ok:True"
                if await update_runtime_task_record(
                    str(seed["run_id"]), status="completed", result_summary="generic terminal update"
                )
                else "ok:False"
            )
        except Exception as exc:  # noqa: BLE001
            log["caller"] = f"{type(exc).__name__}: {str(exc)[:260]}"

    async def opponent_task() -> None:
        await asyncio.wait_for(gate.wait(), timeout=30.0)
        try:
            log["opponent"] = await _append_opponent(seed, owner_sessionmaker, original_lock)
        except Exception as exc:  # noqa: BLE001
            log["opponent"] = f"{type(exc).__name__}: {str(exc)[:260]}"
        finally:
            release.set()

    await asyncio.wait_for(asyncio.gather(caller(), opponent_task()), timeout=120)
    print(f"BATCH-LOCK generic-update/append {log['caller']} | {log['opponent']}")
    assert log.get("caller") == "ok:True", log
    assert log.get("opponent") == "ok:append", log
    assert "Deadlock" not in str(log), log
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        metadata = dict(task.metadata_json or {})
    assert str(task.status) == "completed", task.status
    assert str(metadata.get("terminal_committed_status") or "") == "completed", metadata


async def test_task_admitted_between_prescan_and_batch_is_settled_under_its_advisory(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """Late admission must not create a row → advisory edge.

    A task committed between the batch helper's unlocked prescan and its
    ``FOR UPDATE`` batch carries a session whose advisory is not held yet.
    The previous trailing ``late_session_ids`` acquisition took that
    advisory while rows were already locked and deadlocked an advisory-first
    writer of the late session (the CC5 late-admission reproduction). The
    helper must instead roll back to its savepoint, rescan (the newcomer is
    then in the prescan), and settle EVERYTHING — including the late task —
    with all advisories held first; the opponent must commit too.
    """

    from datetime import UTC, datetime, timedelta

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams, _seed_run

    import app.services.chat_transcript as chat_transcript
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.services.agent_identity_lifecycle import soft_delete_agent

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)

    late_session_id = uuid.uuid4()
    late_run_id = uuid.uuid4()
    log: dict[str, Any] = {"late_rounds": 0}
    opponent_done = asyncio.Event()
    may_update = asyncio.Event()
    original_plural = chat_transcript.lock_transcript_sessions
    original_singular = chat_transcript.lock_transcript_session
    state = {"prescans": 0}

    async def opponent() -> None:
        """Advisory-first ordinary writer on the late-admitted task's row."""
        try:
            async with owner_sessionmaker() as db:
                await original_singular(db, session_id=late_session_id)
                await may_update.wait()
                await db.execute(
                    text("update runtime_tasks set result_summary = :s where id = :i").bindparams(
                        s="late-admission-opponent", i=late_run_id
                    )
                )
                await db.commit()
            log["opponent"] = "ok:plain_update"
        except Exception as exc:  # noqa: BLE001
            log["opponent"] = f"{type(exc).__name__}: {str(exc)[:260]}"
        finally:
            opponent_done.set()

    async def prescan_hook(db, *, session_ids):
        state["prescans"] += 1
        if state["prescans"] == 1:
            # Commit a brand-new active task of this agent in a session the
            # prescan has not seen, exactly in the prescan→batch window.
            async with owner_sessionmaker() as fresh:
                fresh.add(
                    RuntimeTask(
                        id=late_run_id,
                        task_type="business_task",
                        status="running",
                        parent_agent_id=seed["agent_id"],
                        child_agent_id=seed["agent_id"],
                        tenant_id=seed["tenant_id"],
                        parent_session_id=str(late_session_id),
                        child_session_id=str(late_session_id),
                        root_user_id=seed["user_id"],
                        root_session_id=str(late_session_id),
                        prompt="late admitted task",
                        claimed_by="kernel-worker-2",
                        claim_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                        claim_version=1,
                        metadata_json={},
                    )
                )
                await fresh.commit()
            asyncio.get_running_loop().create_task(opponent())
        elif session_ids and any(str(value) == str(late_session_id) for value in session_ids):
            # A later round that now includes the late session: release the
            # opponent's row write while the caller holds NO row locks.
            log["late_rounds"] += 1
            may_update.set()
        return await original_plural(db, session_ids=session_ids)

    monkeypatch.setattr(chat_transcript, "lock_transcript_sessions", prescan_hook)

    async def caller() -> None:
        try:
            async with owner_sessionmaker() as db:
                agent = await db.get(Agent, seed["agent_id"])
                await soft_delete_agent(db, agent, actor_id=seed["user_id"])
                await db.commit()
            log["caller"] = "committed"
        except Exception as exc:  # noqa: BLE001
            log["caller"] = f"{type(exc).__name__}: {str(exc)[:260]}"

    await asyncio.wait_for(caller(), timeout=60)
    await asyncio.wait_for(opponent_done.wait(), timeout=30)
    print(f"BATCH-LOCK late-admission {log}")
    assert log.get("caller") == "committed", log
    assert log.get("opponent") == "ok:plain_update", log
    assert "Deadlock" not in str(log), log
    assert log["late_rounds"] >= 1, "the late session was never prescanned in a restart round"
    async with owner_sessionmaker() as db:
        late = await db.get(RuntimeTask, late_run_id)
        seed_task = await db.get(RuntimeTask, seed["run_id"])
    assert late is not None and str(late.status) == "killed", late and late.status
    assert late.claimed_by is None and int(late.claim_version or 0) >= 2
    assert str(seed_task.status) == "killed", seed_task.status
