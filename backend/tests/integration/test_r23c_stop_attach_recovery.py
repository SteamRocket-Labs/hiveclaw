"""Stopped delegation attach preserves open requests and loader commits."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration import test_r23c_stop_ancestor_admission as admission
from tests.integration import test_r23c_stopped_parent_delegation_recovery as crash
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, SimulatedWorkerCrash

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _crash_child(family, monkeypatch, *, stage="prepare", round_index=1):
    from app.services import session_model_round as rounds

    ready = asyncio.Event()
    bind, prepare, commit = rounds.bind_round_inputs, rounds.prepare_model_request, AsyncSession.commit
    boundary_name = {"sealed": "seal_model_response", "round_committed": "commit_sealed_model_round"}.get(stage)
    boundary = getattr(rounds, boundary_name) if boundary_name else None
    prepared = {}

    async def bind_round(db, **kwargs):
        if kwargs["run_id"] == family.child.id:
            kwargs["round_index"] = round_index
        return await bind(db, **kwargs)

    async def prepare_round(db, **kwargs):
        if kwargs["run_id"] == family.child.id:
            kwargs["round_index"] = round_index
            if stage == "prepare":
                kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
            prepared.update(kwargs)
        result = await prepare(db, **kwargs)
        if kwargs["run_id"] == family.child.id and stage == "prepare":
            db.info["r23c3_crash"] = True
        return result

    async def after_boundary(db, **kwargs):
        result = await boundary(db, **kwargs)
        if kwargs["run_id"] == family.child.id:
            db.info["r23c3_crash"] = True
        return result

    async def die_after_commit(db):
        await commit(db)
        if db.info.pop("r23c3_crash", False):
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise SimulatedWorkerCrash()

    with monkeypatch.context() as patch:
        patch.setattr(rounds, "bind_round_inputs", bind_round)
        patch.setattr(rounds, "prepare_model_request", prepare_round)
        patch.setattr(AsyncSession, "commit", die_after_commit)
        if boundary_name:
            patch.setattr(rounds, boundary_name, after_boundary)
        worker = await admission._claim(family, attempt=1)
        await asyncio.wait_for(ready.wait(), timeout=20)
        worker.cancel()
        await admission._finish(worker)
    async with family.owner() as db:
        task = await db.get(RuntimeTask, family.child.id)
        assert task.status == "running", "Process death must bypass cancellation settlement"
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    return prepared


async def _assert_ambiguous_request(family, *, round_index=1):
    async with family.owner() as db:
        task = await db.get(RuntimeTask, family.child.id)
        row = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.run_id == task.id,
                SessionModelResult.round_id == f"{task.id}:round:{round_index}",
            )
        )
        assert task.status == "needs_reconciliation", "A possibly sent request must never be settled as killed"
        assert row.state == "needs_reconciliation"
        assert row.seal_json == {"delivery_state": "unknown", "error_class": "cancelled", "retry_safe": False}


async def test_suspended_live_stop_scan_preserves_child_reclaim_fence(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from app.services.session_control_input import recover_stale_cancel_control_inputs_once

    async with admission._family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        await _crash_child(family, monkeypatch)
        async with owner_sessionmaker() as db:
            parent = await db.get(RuntimeTask, family.seed["run_id"])
            parent.status = "suspended"
            parent.claimed_by = parent.claim_expires_at = None
            await db.commit()
        # The real live ingress commits begin; the signal stub models death
        # before the database child cancel, with the child still prepared.
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")

        async def must_not_signal(**_kwargs):
            pytest.fail("The existing unclaimed scan branch settles without signalling")

        async with owner_sessionmaker() as db:
            counts = await recover_stale_cancel_control_inputs_once(
                db,
                worker_id="r23c3-suspended",
                signal_callback=must_not_signal,
                stale_after=timedelta(0),
                tenant_id=family.seed["tenant_id"],
            )
            assert counts["settled"] == 1 and counts["signalled"] == 0
        async with owner_sessionmaker() as db:
            parent = await db.get(RuntimeTask, family.seed["run_id"])
            assert parent.status == "killed" and parent.metadata_json["cancel_state"] == "cancelled"
            assert parent.metadata_json["terminal_commit_source"] == "unclaimed_session_cancel"
            child = await db.get(RuntimeTask, family.child.id)
            assert child.status == "running" and "cancel_state" not in child.metadata_json
        await admission._finish(await admission._claim(family, attempt=2))
        assert family.provider.requests == [], "Scan settlement must not reopen an accepted parent stop"
        await _assert_ambiguous_request(family)


async def test_stop_commits_after_first_ancestor_read_without_killing_open_request(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from app.services import web_chat_runtime as runtime

    async with admission._family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        await _crash_child(family, monkeypatch)
        read = runtime._stopped_by_self_or_delegation_ancestor
        observed = []

        async def stop_between_reads(db, task):
            result = await read(db, task)
            if task.id == family.child.id:
                observed.append(result)
                if len(observed) == 1:
                    assert result is False
                    # On 49062c14 this is the loader's read, before attach's
                    # second check. With the fix it is attach's pre-read;
                    # the loader must then see the monotonic committed stop.
                    await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
            return result

        monkeypatch.setattr(runtime, "_stopped_by_self_or_delegation_ancestor", stop_between_reads)
        await admission._finish(await admission._claim(family, attempt=2))
        assert observed == [False, True]
        assert family.provider.requests == []
        await _assert_ambiguous_request(family)


@pytest.mark.parametrize("frontier", ["missing_first", "pending_tool"])
async def test_open_request_beyond_loader_frontier_is_sealed_before_stop_settlement(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, frontier
):
    from app.services import session_model_round as rounds

    provider = ScriptedLLMClient([admission.colleagues._tool("get_current_time", {})])
    async with admission._family(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, provider=provider
    ) as family:
        if frontier == "missing_first":
            # The real bind/prepare boundary accepts a round index without
            # an earlier root; recovery cannot assume contiguous storage.
            await _crash_child(family, monkeypatch, round_index=2)
        else:
            prepared = await _crash_child(family, monkeypatch, stage="round_committed")
            scope = {key: prepared[key] for key in ("tenant_id", "agent_id", "session_id", "run_id", "turn_id")}
            async with owner_sessionmaker() as db:
                assert not list(
                    await db.scalars(
                        select(SessionToolInvocation).where(SessionToolInvocation.run_id == family.child.id)
                    )
                )
                await rounds.bind_round_inputs(db, **scope, round_index=2)
                await rounds.prepare_model_request(
                    db,
                    **scope,
                    round_index=2,
                    messages=[],
                    tools=[],
                    provider=prepared["provider"],
                    model=prepared["model"],
                    wire_request={"messages": [], "mcp_servers": [{"name": "possibly-sent"}]},
                )
                await db.commit()
        sent = len(provider.requests)
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
        await admission._finish(await admission._claim(family, attempt=2))
        assert len(provider.requests) == sent
        await _assert_ambiguous_request(family, round_index=2)


async def test_cancelled_attach_commits_loader_sealed_round_finalization(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    async with admission._family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        await _crash_child(family, monkeypatch, stage="sealed")
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == family.child.id))
            assert row.state == "sealed" and row.round_committed_event_id is None
            result_id, seal = row.id, row.seal_json
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
        await admission._finish(await admission._claim(family, attempt=2))
        assert len(family.provider.requests) == 1
        await admission._assert_cancelled(family)
        async with owner_sessionmaker() as db:
            row = await db.get(SessionModelResult, result_id)
            assert row.state == "round_committed", "CancelledError must not roll back the loader's real registry commit"
            assert row.round_committed_event_id is not None and row.seal_json == seal
