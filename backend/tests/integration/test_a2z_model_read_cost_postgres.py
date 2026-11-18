"""Tool preparation reads scope facts; source validation precedes the run lock."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.services import session_model_round
from app.services.session_tool_runtime import prepare_tool_invocation
from tests.services.test_session_permission_runtime import _seed_waiting_batch
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@contextmanager
def model_read_bytes():
    """Sample an identical SELECT through public results without draining its cursor."""
    observations = []
    sampling = False

    def after(_connection, cursor, sql, _params, _context, _many):
        nonlocal sampling
        if not sampling and sql.lstrip().startswith("SELECT") and "FROM session_model_results" in sql:
            sampling = True
            try:
                rows = _connection.exec_driver_sql(sql, _params).all()
                observations.append(sum(len(str(value).encode()) for row in rows for value in row if value is not None))
            finally:
                sampling = False

    event.listen(Engine, "after_cursor_execute", after)
    try:
        yield observations
    finally:
        event.remove(Engine, "after_cursor_execute", after)


async def test_prepare_tool_invocation_does_not_transfer_model_snapshot(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    original = session_model_round.prepare_model_request

    async def large_request(db, **kwargs):
        kwargs["wire_request"] = {**kwargs["wire_request"], "padding": "x" * 280_000}
        return await original(db, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_model_round, "prepare_model_request", large_request)
        seed = await _seed_waiting_batch(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        with model_read_bytes() as received:
            invocation = await prepare_tool_invocation(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                session_id=seed.session_id, run_id=seed.run_id, provider_request_id=seed.request_id,
                provider_tool_use_id=f"permission-tool-0-{seed.run_id.hex}", tool_name="read_file",
                arguments={"path": "workspace/file-0.md"})
        assert invocation.id == seed.invocation_ids[0]
        assert len(received) == 1
        assert sum(received) < 4096, received


async def test_prepare_takes_no_explicit_run_lock_before_source_reads(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    """A span-free fixture isolates prepare's explicit lock; FK locks are tested separately."""
    from app.services import session_model_inputs

    seed = await _seed_waiting_batch(owner_sessionmaker)
    # owner 11-13 / 11-18 A: binding still reads sources in a private round.
    original = session_model_inputs.build_model_input_bindings
    lock_available = []

    async def inspect_lock(db, **kwargs):
        # No context spans are captured here, so NOWAIT probes the explicit
        # plan lock. Real source-span FK locks have a separate renewal test.
        async with app_user_sessionmaker() as other:
            await pin_rls_tenant_context(other, seed.tenant_id)
            try:
                row = await other.scalar(select(RuntimeTask.id).where(RuntimeTask.id == seed.run_id,
                    RuntimeTask.tenant_id == seed.tenant_id).with_for_update(nowait=True))
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "55P03"
                lock_available.append(False)
            else:
                assert row == seed.run_id
                lock_available.append(True)
        return await original(db, **kwargs)

    monkeypatch.setattr(session_model_inputs, "build_model_input_bindings", inspect_lock)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        await session_model_round.prepare_model_request(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            session_id=seed.session_id, run_id=seed.run_id, turn_id=seed.turn_id, round_index=2,
            messages=[{"role": "user", "content": "Continue the authorized work."}], tools=[],
            provider="synthetic", model="local-reference")
        await db.commit()
    assert lock_available == [True]


async def test_saved_file_database_error_rolls_back_input_binding(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.database import tenant_scoped_session
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services import artifact_continuation
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.web_chat_run_orchestrator import _bind_session_round_inputs
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority, _seed_session

    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)

    async def allow(**_kwargs):
        return HookResult()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        authority = await _authority(db, user_id=user, agent_id=agent, session_id=session)
        receipt = await _accepted_input(db, authority=authority, session_id=session, run_id=run,
            kind="steer_current_turn", content="Continue with the saved report.")
        await run_user_prompt_admission(db, authority=authority, input_id=receipt.input_id,
            worker_id="a2z-atomic-bind", hook_executor=allow)
        await queue_admitted_human_input(db, authority=authority, input_id=receipt.input_id)
        await db.commit()

    async def database_error(db, **_kwargs):
        await db.execute(text("SELECT a2z_missing_saved_file_column"))

    monkeypatch.setattr(artifact_continuation, "saved_file_context", database_error)
    state = SimpleNamespace(agent=SimpleNamespace(id=agent, tenant_id=tenant),
        actor_user_id=user, run_uuid=run, session_id=str(session), metadata={"turn_id": f"turn-{run.hex}"},
        ports=SimpleNamespace(runtime=SimpleNamespace(tenant_scoped_session=lambda tenant:
            tenant_scoped_session(tenant, session_factory=app_user_sessionmaker))))
    with pytest.raises(DBAPIError):
        await _bind_session_round_inputs(state, 1)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        row = await db.get(SessionTurnInput, receipt.input_id)
        assert row.status == "queued"
        assert row.bound_round_id is None
        assert row.model_request_snapshot_ref is None
        assert await db.scalar(select(SessionModelResult.id).where(
            SessionModelResult.tenant_id == tenant, SessionModelResult.run_id == run)) is None


async def test_next_round_reads_previous_identity_without_snapshot(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    original = session_model_round.prepare_model_request

    async def large_request(db, **kwargs):
        kwargs["wire_request"] = {**kwargs["wire_request"], "padding": "x" * 280_000}
        return await original(db, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_model_round, "prepare_model_request", large_request)
        seed = await _seed_waiting_batch(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        with model_read_bytes() as received:
            await session_model_round.prepare_model_request(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                session_id=seed.session_id, run_id=seed.run_id, turn_id=seed.turn_id, round_index=2,
                messages=[{"role": "user", "content": "Continue the authorized work."}], tools=[],
                provider="synthetic", model="local-reference")
        await db.commit()
    assert received
    assert sum(received) < 4096, received


async def test_semantic_history_reads_seals_without_request_snapshots(
    native_workspace, app_user_sessionmaker, monkeypatch,
):
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_model_inputs import message_snapshot
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    original = session_model_round.prepare_model_request

    async def large_request(db, **kwargs):
        kwargs["wire_request"] = {"messages": [message_snapshot(message) for message in kwargs["messages"]],
            "tools": kwargs["tools"], "padding": "x" * 280_000}
        return await original(db, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_model_round, "prepare_model_request", large_request)
        await memory_call(n, app_user_sessionmaker, "get_current_time", {})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        with model_read_bytes() as received:
            history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session)
    assert history.messages
    assert received
    assert sum(received) < 4096, received


async def test_history_read_does_not_leave_partial_models_for_same_session_get(
    native_workspace, app_user_sessionmaker,
):
    from app.models.session_v2 import SessionModelResult
    from app.services.session_semantic_history import load_session_semantic_history
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    _, invocation = await memory_call(n, app_user_sessionmaker, "get_current_time", {})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        identity, expected_hash = (await db.execute(select(SessionModelResult.id,
            SessionModelResult.model_request_hash).where(SessionModelResult.tenant_id == n.tenant,
            SessionModelResult.provider_request_id == invocation.provider_request_id))).one()
        retained = []

        def retain_model(_session, instance):
            if isinstance(instance, SessionModelResult):
                retained.append(instance)

        # Other consumers can retain loaded entities. Keep any history loads
        # alive so the weak identity map cannot hide this same-session case.
        event.listen(db.sync_session, "loaded_as_persistent", retain_model)
        try:
            history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session)
        finally:
            event.remove(db.sync_session, "loaded_as_persistent", retain_model)
        assert history.messages
        result = await db.get(SessionModelResult, identity)
        assert result is not None
        assert result.model_request_hash == expected_hash


async def test_lease_renewal_is_compatible_with_source_span_foreign_key_lock(
    owner_sessionmaker, app_user_sessionmaker,
):
    from datetime import datetime, timedelta, timezone
    import uuid
    from app.models.invocation_span import InvocationSpan
    from app.services.runtime_task_fence import (
        renew_current_runtime_task_lease, reset_runtime_task_fence, set_runtime_task_fence,
    )

    seed = await _seed_waiting_batch(owner_sessionmaker)
    old_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.status, task.claimed_by, task.claim_version = "running", "a2z-worker", 1
        task.claim_expires_at = old_expiry
        await db.commit()

    # Capturing a kernel-context source inserts a span with this FK. PostgreSQL
    # retains KEY SHARE until that source-validation transaction finishes.
    async with app_user_sessionmaker() as source_db:
        await pin_rls_tenant_context(source_db, seed.tenant_id)
        identity = uuid.uuid4()
        source_db.add(InvocationSpan(id=identity, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            runtime_task_id=seed.run_id, trace_id=str(identity), span_id=str(identity), span_type="kernel_context",
            name="source-validation", status="ok", started_at=datetime.now(timezone.utc)))
        await source_db.flush()

        def no_wait(_connection, _cursor, sql, params, _context, _many):
            # Fail at the conflicting database lock instead of waiting for the
            # held source transaction; the production UPDATE and CAS stay real.
            if sql.lstrip().startswith("SELECT runtime_tasks.id") and "FOR " in sql:
                return sql + " NOWAIT", params
            return sql, params

        token = set_runtime_task_fence(task_id=seed.run_id, claim_version=1, worker_id="a2z-worker")
        event.listen(Engine, "before_cursor_execute", no_wait, retval=True)
        try:
            renewed = await renew_current_runtime_task_lease(lease_seconds=600)
        finally:
            event.remove(Engine, "before_cursor_execute", no_wait)
            reset_runtime_task_fence(token)
        assert renewed > old_expiry
        async with app_user_sessionmaker() as other:
            await pin_rls_tenant_context(other, seed.tenant_id)
            task = await other.get(RuntimeTask, seed.run_id)
            assert task.claim_expires_at == renewed
            assert (task.status, task.claimed_by, task.claim_version) == ("running", "a2z-worker", 1)


async def test_terminal_boundary_lookup_does_not_scan_delivered_history(
    owner_sessionmaker, app_user_sessionmaker,
):
    import json
    from types import SimpleNamespace
    import uuid
    from sqlalchemy import text
    from app.services.session_input_dispatch import _terminal_boundary_dispatch_hold

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await db.execute(text("""INSERT INTO runtime_tasks(id,tenant_id,task_type,status,parent_agent_id,
            child_agent_id,depth,root_idempotency_key,config_snapshot_hash,policy_snapshot_hash,completed_at,
            terminal_boundary_enqueued_at)
            SELECT md5('a2z-history-'||g)::uuid,:tenant,'heartbeat','completed',:agent,:agent,1,
                'a2z-history-'||g,repeat('0',64),repeat('0',64),now(),now() FROM generate_series(1,3000) g"""),
            {"tenant": seed.tenant_id, "agent": seed.agent_id})
        await db.execute(text("""INSERT INTO runtime_terminal_boundary_outbox(id,tenant_id,runtime_task_id,
            agent_id,session_id,event_kind,terminal_status,authority_ref,authority_id,binding_json,
            binding_sha256,idempotency_key,status,delivered_at)
            SELECT md5('a2z-boundary-'||g)::uuid,:tenant,md5('a2z-history-'||g)::uuid,:agent,
                'a2z-background-'||g,'turn_stop','completed','runtime_task',md5('a2z-history-'||g),
                '{}',repeat('0',64),md5(g::text)||md5(g::text),'delivered',now()
                FROM generate_series(1,3000) g"""), {"tenant": seed.tenant_id, "agent": seed.agent_id})
        await db.commit()
        await db.execute(text("ANALYZE runtime_terminal_boundary_outbox"))
    plans = []

    def explain(connection, _cursor, sql, params, _context, _many):
        if sql.lstrip().startswith("SELECT") and "FROM runtime_terminal_boundary_outbox" in sql:
            plan = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).scalar()
            plans.append(json.loads(plan) if isinstance(plan, str) else plan)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        event.listen(Engine, "after_cursor_execute", explain)
        try:
            result = await _terminal_boundary_dispatch_hold(db,
                admission=SimpleNamespace(tenant_id=seed.tenant_id), row=None, command=None, authority=None,
                agent=SimpleNamespace(id=seed.agent_id), session=SimpleNamespace(id=uuid.uuid4()))
        finally:
            event.remove(Engine, "after_cursor_execute", explain)
    assert result is None
    assert len(plans) == 1

    def scanned_rows(node):
        scanned = 0
        if node.get("Relation Name") == "runtime_terminal_boundary_outbox":
            scanned = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                + node.get("Rows Removed by Index Recheck", 0)) * node.get("Actual Loops", 1)
        return scanned + sum(scanned_rows(child) for child in node.get("Plans", []))

    assert scanned_rows(plans[0][0]["Plan"]) < 50, plans


async def test_model_prepare_rechecks_claim_after_unlocked_source_binding(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from app.services import session_model_inputs
    from app.services.runtime_task_fence import (
        StaleRuntimeTaskFenceError, reset_runtime_task_fence, set_runtime_task_fence,
    )

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.status, task.claimed_by, task.claim_version = "running", "a2z-original", 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        await db.commit()
    # owner 11-13 / 11-18 A: supersede the claim during the private binding read.
    original = session_model_inputs.build_model_input_bindings
    changed = []

    async def supersede_claim(*args, **kwargs):
        async with app_user_sessionmaker() as other:
            await pin_rls_tenant_context(other, seed.tenant_id)
            await other.execute(select(RuntimeTask.id).where(RuntimeTask.id == seed.run_id,
                RuntimeTask.tenant_id == seed.tenant_id).with_for_update(key_share=True, nowait=True))
            await other.execute(update(RuntimeTask).where(RuntimeTask.id == seed.run_id,
                RuntimeTask.tenant_id == seed.tenant_id).values(claimed_by="a2z-successor", claim_version=2))
            await other.commit()
            changed.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(session_model_inputs, "build_model_input_bindings", supersede_claim)
    token = set_runtime_task_fence(task_id=seed.run_id, claim_version=1, worker_id="a2z-original")
    try:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            # Native/machine callers retain this ORM row while preparing their
            # next request. A locked read must not trust its old identity map.
            held_task = await db.get(RuntimeTask, seed.run_id)
            with pytest.raises(StaleRuntimeTaskFenceError):
                await session_model_round.prepare_model_request(db, tenant_id=seed.tenant_id,
                    agent_id=seed.agent_id, session_id=seed.session_id, run_id=seed.run_id,
                    turn_id=seed.turn_id, round_index=2,
                    messages=[{"role": "user", "content": "Continue the authorized work."}], tools=[],
                    provider="synthetic", model="local-reference")
            assert held_task.id == seed.run_id
    finally:
        reset_runtime_task_fence(token)
    assert changed == [True]
