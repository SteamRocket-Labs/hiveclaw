"""Ancestor stops fence delegation admission without mutating descendant rows."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration import test_r11c_colleague_lifecycle_postgres as colleagues
from tests.integration import test_r23a_stop_ends_blocked_model_call as stopped
from tests.integration import test_r23c_stopped_parent_delegation_recovery as crash
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, SimulatedWorkerCrash
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@asynccontextmanager
async def _family(owner, app_user, monkeypatch, tmp_path, *, late=False, provider=None):
    import app.runtime.invoker as invoker
    from app.agents import orchestrator

    seed, parent, _ = await colleagues._setup(
        owner, app_user, monkeypatch, tmp_path, tool="delegate_to_agent", paused=True
    )
    parent.script[0] = colleagues._tool(
        "delegate_to_agent",
        {
            "agent_name": colleagues.COLLEAGUE,
            "message": "Review the supplied facts.",
            "tool_profile": "review_readonly",
        },
    )
    provider = provider or ScriptedLLMClient([{"content": "Delegated result"}])
    monkeypatch.setattr(
        invoker, "create_llm_client", lambda **kwargs: provider if kwargs["model"] == "fake-colleague" else parent
    )
    entering, admit = asyncio.Event(), asyncio.Event()
    create = orchestrator.create_runtime_task_record

    async def late_admission(**kwargs):
        if kwargs["task_type"] == "delegation":
            entering.set()
            await admit.wait()
        return await create(**kwargs)

    if late:
        monkeypatch.setattr(orchestrator, "create_runtime_task_record", late_admission)
    workers = [await stopped._dispatch(owner, seed["run_id"])]
    try:
        if late:
            await asyncio.wait_for(entering.wait(), timeout=20)
            assert await colleagues._child(owner, seed) is None
            await crash._accept_stop(owner, seed, monkeypatch, "live")
            admit.set()
        await asyncio.wait_for(parent.started.wait(), timeout=20)
        child = await colleagues._child(owner, seed)
        assert child is not None and child.metadata_json["tool_profile"] == "review_readonly"
        yield SimpleNamespace(seed=seed, child=child, parent=parent, provider=provider, workers=workers, owner=owner)
    finally:
        admit.set()
        parent.release.set()
        if hasattr(provider, "release"):
            provider.release.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def _claim(family, *, attempt):
    worker = await crash._claim_and_dispatch_child(family.owner, family.child.id, attempt=attempt)
    family.workers.append(worker)
    return worker


async def _finish(worker):
    return await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=20)


async def _assert_cancelled(family, *, boundary_kind="turn_abort"):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

    async with family.owner() as db:
        child = await db.get(RuntimeTask, family.child.id)
        assert child.status == "killed" and child.metadata_json["cancelled"] is True
        assert child.completed_at is not None and child.metadata_json["terminal_execution_fence_ref"]
        events = list(
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.run_id == child.id,
                    ChatTranscriptEvent.item_kind == "run",
                    ChatTranscriptEvent.lifecycle == "cancelled",
                )
            )
        )
        assert len(events) == 1
        boundary = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == child.id,
                # Without any non-Run execution frontier the existing builder
                # uses runtime_terminal; an interrupted execution uses turn_abort.
                RuntimeTerminalBoundaryOutbox.event_kind == boundary_kind,
            )
        )
        assert boundary is not None and boundary.terminal_status == "killed"
        assert "cancel_state" not in child.metadata_json, "An ancestor stop must never become the child's own stop"


async def test_child_admitted_after_stop_is_reclaimed_without_provider_request(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, late=True) as family:
        async with owner_sessionmaker() as db:
            claimed = await RuntimeTaskClaimService(
                db=db, worker_id="r23c-late-before-crash", task_types=("delegation",), lease_seconds=60
            ).claim_available(batch_size=25)
        child = next(task for task in claimed if task.id == family.child.id)
        assert child.attempt_count == 1 and child.status == "running"
        # Process death after claim, before attach: no cancellation callback runs.
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, child.id)).claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        await _finish(await _claim(family, attempt=2))
        assert family.provider.requests == []
        await _assert_cancelled(family, boundary_kind="runtime_terminal")


async def test_pending_child_first_claim_after_stop_uses_existing_cancellation_settlement(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.agents import orchestrator
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    budget = orchestrator._settle_delegation_budget
    settled = []

    async def observe_budget(**kwargs):
        settled.append((kwargs["task_id"], kwargs["status"]))
        return await budget(**kwargs)

    monkeypatch.setattr(orchestrator, "_settle_delegation_budget", observe_budget)
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        assert family.child.status == "pending" and family.child.attempt_count == 0
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
        await _finish(await _claim(family, attempt=1))
        assert family.provider.requests == []
        await _assert_cancelled(family, boundary_kind="runtime_terminal")
        assert settled == [(family.child.id.hex, "killed")]
        processor = DirectInvocationTerminalBoundaryProcessor(
            session_factory=app_user_sessionmaker, data_root=family.seed["data_root"]
        )
        drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=family.seed["tenant_id"],
            worker_id="r23c-stop-notice",
            canonical_validator=processor.validate,
            process_callback=processor,
            task_types=("delegation",),
        )
        assert drained["delivered"] == 1, drained
        async with owner_sessionmaker() as db:
            notice = await db.scalar(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(family.child.id))
            )
            assert notice is not None and notice.terminal_status == "killed"
            assert notice.parent_session_id == family.seed["session_id"]


async def test_between_round_child_reclaim_never_enters_kernel_after_parent_stop(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.services import session_model_round as rounds

    provider = ScriptedLLMClient([colleagues._tool("get_current_time", {}), {"content": "Must not request this round"}])
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, provider=provider) as family:
        ready = asyncio.Event()
        commit_round, commit = rounds.commit_sealed_model_round, AsyncSession.commit

        async def after_round(db, **kwargs):
            result = await commit_round(db, **kwargs)
            if kwargs["run_id"] == family.child.id:
                db.info["r23c_between_rounds"] = True
            return result

        async def die_between_rounds(db):
            await commit(db)
            if db.info.pop("r23c_between_rounds", False):
                ready.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise SimulatedWorkerCrash()

        monkeypatch.setattr(rounds, "commit_sealed_model_round", after_round)
        monkeypatch.setattr(AsyncSession, "commit", die_between_rounds)
        worker = await _claim(family, attempt=1)
        await asyncio.wait_for(ready.wait(), timeout=20)
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
        worker.cancel()
        await _finish(worker)
        monkeypatch.setattr(rounds, "commit_sealed_model_round", commit_round)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        async with owner_sessionmaker() as db:
            rows = list(
                await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == family.child.id))
            )
            assert len(rows) == 1 and rows[0].state == "round_committed"
            (await db.get(RuntimeTask, family.child.id)).claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        assert len(provider.requests) == 1
        await _finish(await _claim(family, attempt=2))
        assert len(provider.requests) == 1, "The committed first round must never lead to another provider request"
        await _assert_cancelled(family)


async def _second_stopping_principal(owner, seed):
    from app.models.user import User

    # A scoped administrator is a real second principal allowed to mutate
    # this private session. Shared groups require the original requester.
    async with owner() as db:
        other = User(
            id=uuid4(),
            tenant_id=seed["tenant_id"],
            username=f"r23c-{uuid4().hex}",
            email=f"{uuid4().hex}@fixture.test",
            password_hash="x",
            display_name="Second stopping principal",
            role="org_admin",
        )
        db.add(other)
        await db.commit()
        return other.id


async def _stop_as(owner, seed, user_id):
    from app.api.chat_sessions import cancel_session_run
    from app.models.user import User

    async with owner() as db:
        return await cancel_session_run(
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            idempotency_key=f"r23c-stop:{seed['run_id']}:{user_id}",
            current_user=await db.get(User, user_id),
            db=db,
        )


async def test_second_principal_stop_accept_never_locks_or_writes_descendants(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.agents import orchestrator
    from app.services import web_chat_runtime as runtime

    seed, child_id, _, _, _, _ = await colleagues._stop_fixture(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
    )
    other = await _second_stopping_principal(owner_sessionmaker, seed)
    async with owner_sessionmaker() as db:
        before = (await db.get(RuntimeTask, child_id)).metadata_json
    held, release = asyncio.Event(), asyncio.Event()
    cancel, signal = orchestrator.cancel_async_delegation, runtime.signal_web_chat_cancel
    calls = []

    async def hold_child(*args, **kwargs):
        async with owner_sessionmaker() as db:
            await db.scalar(select(RuntimeTask).where(RuntimeTask.id == child_id).with_for_update())
            held.set()
            await release.wait()
        return await cancel(*args, **kwargs)

    async def signal_after_accept(**kwargs):
        calls.append(kwargs["user_id"])
        if len(calls) == 1:
            return await signal(**kwargs)
        return runtime.CancelSignalDeliveryReceipt(
            run_id=seed["run_id"].hex,
            delivery_state="delivered",
            local_delivered=False,
            cross_process_delivered=True,
            retryable=False,
        )

    monkeypatch.setattr(orchestrator, "cancel_async_delegation", hold_child)
    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal_after_accept)
    first = asyncio.create_task(_stop_as(owner_sessionmaker, seed, seed["user_id"]))
    engine = owner_sessionmaker.kw["bind"].sync_engine
    statements = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, str(parameters)))

    try:
        await asyncio.wait_for(held.wait(), timeout=20)
        event.listen(engine, "before_cursor_execute", record)
        # The first cancellation owns the child row. A second accept must
        # finish while that lock remains held; otherwise S_parent -> child is back.
        second = await asyncio.wait_for(_stop_as(owner_sessionmaker, seed, other), timeout=2)
        assert second["accepted"] is True and len(calls) == 2
        descendant_locks = [
            (sql, params)
            for sql, params in statements
            if "FOR UPDATE" in sql.upper() and (str(child_id) in params or "cancel_delegation_children" in sql)
        ]
        assert descendant_locks == []
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, child_id)).metadata_json == before
    finally:
        if event.contains(engine, "before_cursor_execute", record):
            event.remove(engine, "before_cursor_execute", record)
        release.set()
        await _finish(first)


async def test_direct_child_stop_ids_survive_parent_stop(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        await crash._accept_stop(owner_sessionmaker, {**family.seed, "run_id": family.child.id}, monkeypatch, "live")
        async with owner_sessionmaker() as db:
            before = dict((await db.get(RuntimeTask, family.child.id)).metadata_json)
            assert before["cancel_control_id"] and before["cancel_command_id"]
        await crash._accept_stop(owner_sessionmaker, family.seed, monkeypatch, "live")
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, family.child.id)).metadata_json == before


async def test_redis_publish_failure_still_cancels_children_in_database(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.services import runtime_control_bus

    seed, child_id, _, delivered, _, _ = await colleagues._stop_fixture(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
    )

    async def unavailable(**_kwargs):
        raise ConnectionError("Synthetic local Redis failure")

    monkeypatch.setattr(runtime_control_bus, "publish_web_chat_cancel", unavailable)
    assert (await _stop_as(owner_sessionmaker, seed, seed["user_id"]))["accepted"] is True
    assert delivered[0].delivery_state == "unavailable" and delivered[0].retryable is True
    async with owner_sessionmaker() as db:
        child = await db.get(RuntimeTask, child_id)
        assert child.status == "killed" and child.metadata_json["cancelled"] is True
        assert "cancel_state" not in child.metadata_json


async def test_live_parent_stop_never_marks_child_cancelling(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.services import runtime_control_bus, web_chat_runtime as runtime

    provider = colleagues.PausedClient([{"content": "Must be abandoned"}], pause_at=0)
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, provider=provider) as family:
        worker = await _claim(family, attempt=1)
        await asyncio.wait_for(provider.started.wait(), timeout=20)
        signal = runtime.signal_web_chat_cancel
        observed = []

        async def publish(**_kwargs):
            pass

        async def observe(**kwargs):
            async with owner_sessionmaker() as db:
                observed.append((await db.get(RuntimeTask, family.child.id)).metadata_json.get("cancel_state"))
            return await signal(**kwargs)

        monkeypatch.setattr(runtime_control_bus, "publish_web_chat_cancel", publish)
        monkeypatch.setattr(runtime, "signal_web_chat_cancel", observe)
        assert (await stopped._stop(owner_sessionmaker, family.seed))["accepted"] is True
        await _finish(worker)
        assert observed == [None] and provider.cancelled.is_set()
        await _assert_cancelled(family)


@pytest.mark.parametrize("state", ["cancelling", "cancelled"])
async def test_grandparent_stop_is_detected_in_one_scalar_cte(owner_sessionmaker, state):
    from app.services.web_chat_runtime import _stopped_by_self_or_delegation_ancestor

    tenant, _, agent, session, root_id = await _seed_session(owner_sessionmaker, active_run=True)
    child_id, grandchild_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        root = await db.get(RuntimeTask, root_id)
        root.metadata_json = {**root.metadata_json, "cancel_state": state}
        for task_id, parent in ((child_id, root_id), (grandchild_id, child_id)):
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant,
                    task_type="delegation",
                    status="pending",
                    parent_agent_id=agent,
                    parent_session_id=str(session),
                    root_runtime_task_id=parent,
                    metadata_json={"unrelated_body": "Must not load a full row" * 100},
                )
            )
        await db.commit()
        grandchild = await db.get(RuntimeTask, grandchild_id)
        # A read-only one-statement helper must not flush its caller's work.
        grandchild.prompt = "Caller-owned unflushed update"
        engine = owner_sessionmaker.kw["bind"].sync_engine
        statements = []

        def record(_conn, _cursor, sql, _parameters, _context, _executemany):
            statements.append(sql)

        event.listen(engine, "before_cursor_execute", record)
        try:
            assert await _stopped_by_self_or_delegation_ancestor(db, grandchild) is True
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert len(statements) == 1 and "WITH RECURSIVE" in statements[0]
        assert db.is_modified(grandchild), "The helper must leave caller-owned changes unflushed"
        assert "FOR UPDATE" not in statements[0].upper() and "runtime_tasks.prompt" not in statements[0]
        assert "metadata_json ->>" in statements[0], "Project scalar metadata fields, never the full metadata document"


async def test_later_stopped_turn_does_not_stop_child_of_normally_completed_turn(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    async with _family(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path) as family:
        family.parent.release.set()
        await _finish(family.workers[0])
        later_id = uuid4()
        async with owner_sessionmaker() as db:
            original = await db.get(RuntimeTask, family.seed["run_id"])
            assert original.status == "completed" and "cancel_state" not in original.metadata_json
            db.add(
                RuntimeTask(
                    id=later_id,
                    tenant_id=family.seed["tenant_id"],
                    task_type="web_chat_turn",
                    status="running",
                    parent_agent_id=family.seed["agent_id"],
                    child_agent_id=family.seed["agent_id"],
                    parent_session_id=str(family.seed["session_id"]),
                    child_session_id=str(family.seed["session_id"]),
                    root_user_id=family.seed["user_id"],
                    root_runtime_task_id=later_id,
                )
            )
            await db.commit()
        await crash._accept_stop(owner_sessionmaker, {**family.seed, "run_id": later_id}, monkeypatch, "live")
        await _finish(await _claim(family, attempt=1))
        assert len(family.provider.requests) == 1
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, family.child.id)
            assert child.status == "completed" and "cancel_state" not in child.metadata_json


async def test_unclaimed_cancel_settlement_does_not_inherit_stop_to_descendants(owner_sessionmaker):
    from tests.services.test_session_input_control_v2 import _authority
    from app.services.session_control_input import accept_cancel_control_input, recover_stale_cancel_control_inputs_once
    from app.services.web_chat_runtime import _stopped_by_self_or_delegation_ancestor

    tenant, user, agent, session, root_id = await _seed_session(owner_sessionmaker, active_run=True)
    child_id = uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, root_id)).status = "suspended"
        db.add(
            RuntimeTask(
                id=child_id,
                tenant_id=tenant,
                task_type="delegation",
                status="pending",
                parent_agent_id=agent,
                parent_session_id=str(session),
                root_runtime_task_id=root_id,
            )
        )
        await db.commit()
        authority = await _authority(db, user_id=user, agent_id=agent, session_id=session)
        await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=uuid4(),
            expected_run_id=root_id,
            idempotency_key=f"r23c-unclaimed:{root_id}",
        )
        await db.commit()

        async def must_not_signal(**_kwargs):
            pytest.fail("Unclaimed suspended runs are settled without cancelling descendants")

        counts = await recover_stale_cancel_control_inputs_once(
            db, worker_id="r23c-unclaimed", signal_callback=must_not_signal, stale_after=timedelta(0), tenant_id=tenant
        )
        assert counts["settled"] == 1 and counts["signalled"] == 0
    async with owner_sessionmaker() as db:
        root, child = await db.get(RuntimeTask, root_id), await db.get(RuntimeTask, child_id)
        assert root.metadata_json["cancel_state"] == "cancelled"
        assert root.metadata_json["terminal_commit_source"] == "unclaimed_session_cancel"
        assert await _stopped_by_self_or_delegation_ancestor(db, child) is False
        assert await _stopped_by_self_or_delegation_ancestor(db, root) is True


@pytest.mark.parametrize("excluded", ["remote_type", "remote_session", "non_delegation_link", "other_tenant"])
async def test_ancestor_stop_respects_live_scope_and_delegation_links(owner_sessionmaker, excluded):
    from app.models.tenant import Tenant
    from app.services.web_chat_runtime import _stopped_by_self_or_delegation_ancestor

    tenant, _, agent, session, root_id = await _seed_session(owner_sessionmaker, active_run=True)
    child_id = uuid4()
    async with owner_sessionmaker() as db:
        root = await db.get(RuntimeTask, root_id)
        root.metadata_json = {**root.metadata_json, "cancel_state": "cancelling"}
        if excluded == "remote_type":
            root.task_type = "remote_a2a"
        elif excluded == "remote_session":
            root.metadata_json = {
                **root.metadata_json,
                "schema": "hive.remote_a2a_task.v1",
                "remote_a2a": {"dispatch_kind": "session"},
            }
        child_tenant = tenant
        if excluded == "other_tenant":
            child_tenant = uuid4()
            db.add(Tenant(id=child_tenant, name="Other ancestor scope", slug=f"r23c-{child_tenant.hex}"))
            await db.flush()
        db.add(
            RuntimeTask(
                id=child_id,
                tenant_id=child_tenant,
                task_type="subagent" if excluded == "non_delegation_link" else "delegation",
                status="pending",
                parent_agent_id=agent,
                parent_session_id=str(session),
                root_runtime_task_id=root_id,
            )
        )
        await db.commit()
        assert await _stopped_by_self_or_delegation_ancestor(db, await db.get(RuntimeTask, child_id)) is False
