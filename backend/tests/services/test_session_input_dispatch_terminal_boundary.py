import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.runtime_task_worker import _discover_terminal_boundary_tenants
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService, enqueue_terminal_boundary
from app.services.session_input_dispatch import recover_admitted_session_inputs_once
from app.services.session_live_input import submit_live_human_input
from tests.services.test_session_input_control_v2 import _seed_session


pytestmark = pytest.mark.asyncio


async def test_discovery_holds_only_the_named_unchanged_undelivered_boundary(owner_sessionmaker):
    from app.services.session_input_dispatch import discover_admitted_session_input_locators
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    boundary_id = uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(db, authority=authority, session_id=session_id)
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == receipt.input_id))
        admission.state, admission.dispatch_state = "admitted", "pending"
        boundary = RuntimeTerminalBoundaryOutbox(
            id=boundary_id, tenant_id=tenant_id, runtime_task_id=run_id, task_type="web_chat_turn",
            agent_id=agent_id, session_id=str(session_id), event_kind="turn_stop", terminal_status="completed",
            authority_ref="runtime_task", authority_id=str(run_id), binding_json={}, binding_sha256="a" * 64,
            idempotency_key="b" * 64, status="pending",
        )
        db.add(boundary)
        await db.commit()
        for status, receipt_status, named_id, code, boundary_session, dispatch_state, held in [
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", True),
            ("processing", "processing", boundary_id.hex, "terminal_boundary_ack_pending", session_id.hex, "pending", True),
            # R8a B1: even a hold recorded by the old worker at dead_letter
            # must wake up to reject; it is no longer a quiescent queue.
            ("dead_letter", "dead_letter", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("delivered", "delivered", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("processing", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(uuid4()), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "missing", None, "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", "invalid-id", "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id).upper(), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id), "other", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(uuid4()), "pending", False),
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "dispatching", False),
        ]:
            boundary.status, boundary.session_id = status, boundary_session
            admission.dispatch_state = dispatch_state
            admission.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            admission.dispatch_receipt_json = {
                "code": code, "terminal_boundary_id": named_id, "terminal_boundary_status": receipt_status,
            }
            await db.commit()
            found = await discover_admitted_session_input_locators(db, tenant_id=tenant_id)
            assert found == ([] if held else [(tenant_id, admission.id)])

        # A boundary in another tenant cannot hold this input even with an exact ID.
        other_tenant, _, _, _, _ = await _seed_session(owner_sessionmaker)
        boundary.tenant_id, boundary.session_id = other_tenant, str(session_id)
        admission.dispatch_state = "pending"
        await db.commit()
        assert await discover_admitted_session_input_locators(db, tenant_id=tenant_id) == [(tenant_id, admission.id)]


@pytest.mark.parametrize("missing_boundary", [False, True], ids=["old-pending", "old-missing"])
async def test_old_web_boundary_delivery_releases_next_session_input(owner_sessionmaker, missing_boundary):
    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    old = datetime.now(UTC) - timedelta(hours=25)
    next_input_id = uuid4()

    async def build(db, task):
        row = await enqueue_terminal_boundary(db, task=task, event_kind="turn_stop", agent_id=agent_id,
            session_id=session_id, terminal_status="completed", authority_ref="runtime_task", authority_id=task.id,
            binding={"terminal_event_id": uuid4(), "terminal_sequence": 1, "authority_sha256": "a" * 64})
        return (row,)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        task.status = "completed"
        task.created_at = task.completed_at = old
        if not missing_boundary:
            row, = await build(db, task)
            row.available_at = row.created_at = old
        await db.commit()
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, agent_id),
            user=await db.get(User, user_id), session=await db.get(ChatSession, session_id),
            content="Continue after the previous result", source="old-web-boundary-test",
            input_id=next_input_id, idempotency_key=f"old-web:{next_input_id}")
        assert receipt["dispatch"]["kind"] == "deferred"
        assert receipt["dispatch"]["status"] == (
            "waiting_for_terminal_boundary_reconciliation" if missing_boundary else "waiting_for_terminal_boundary_ack")

    assert tenant_id in await _discover_terminal_boundary_tenants(worker_id="old-web", task_types=("web_chat_turn",),
        session_factory=owner_sessionmaker, limit=1000)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    if missing_boundary:
        assert await service.reconcile_terminal_tasks_once(tenant_id=tenant_id, builder=build,
            task_types=("web_chat_turn",)) == {"claimed": 1, "enqueued": 1, "held": 0}
    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="old-web", task_types=("web_chat_turn",))
    assert [item.runtime_task_id for item in claimed] == [run_id]

    async def canonical(_db, item):
        return dict(item.binding)

    async def deliver(item):
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    assert await service.process_terminal_boundary(item=claimed[0], worker_id="old-web",
        canonical_validator=canonical, process_callback=deliver)
    async with owner_sessionmaker() as db:
        boundary = await db.get(RuntimeTerminalBoundaryOutbox, claimed[0].id)
        assert boundary.status == "delivered"
        recovery = await recover_admitted_session_inputs_once(db, worker_id="old-web-input", tenant_id=tenant_id)
        assert recovery == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == next_input_id))
        assert admission.dispatch_state == "dispatched"
        next_input = await db.get(SessionTurnInput, next_input_id)
        assert next_input.target_run_id == uuid5(next_input_id, "session-v2-runtime-run")
        assert await db.get(RuntimeTask, next_input.target_run_id) is not None
        assert (await recover_admitted_session_inputs_once(db, worker_id="old-web-input", tenant_id=tenant_id))["claimed"] == 0
        assert list((await db.scalars(select(RuntimeTask.id).where(
            RuntimeTask.tenant_id == tenant_id, RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.id != run_id))).all()) == [next_input.target_run_id]


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _summary_test_material(seeded, *, runtime_task_id, terminal_sequence: int, final_response: str):
    from app.services.web_terminal_boundary_processor import _WebTerminalMaterial

    return _WebTerminalMaterial(
        tenant_id=seeded.tenant_id,
        runtime_task_id=runtime_task_id,
        agent_id=seeded.agent_id,
        session_id=seeded.session_id,
        turn_id=f"turn-{runtime_task_id.hex}",
        event_kind="turn_stop",
        terminal_status="completed",
        terminal_event_id=uuid4(),
        terminal_sequence=terminal_sequence,
        agent_name="R15i Agent",
        user_id=seeded.user_id,
        response_messages=({"role": "user", "content": "previous turn"},),
        summary_messages=(
            {"role": "user", "content": "previous turn"},
            {"role": "assistant", "content": final_response},
        ),
        response_commit={
            "schema": "hive.response_commit.v1",
            "committed": True,
            "commit_kind": "session_v2_terminal_outcome",
            "idempotency_key": f"session-run-outcome:{runtime_task_id}",
            "runtime_task_id": str(runtime_task_id),
            "terminal_outcome_id": str(uuid4()),
            "terminal_result_id": str(uuid4()),
            "terminal_event_id": str(uuid4()),
            "source_refs": [f"session-run-outcome://{uuid4()}"],
        },
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(),
    )


def _deferred_summary_processor(owner_sessionmaker, material, *, summary_generator, bridge=True):
    """A real WebTerminalBoundaryProcessor whose externals are injected doubles.

    Only the boundary-load and T0 frontier verification are stubbed onto the
    instance; ``__call__``, the response projection and the whole summary
    projection state machine run for real against Postgres.
    """

    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    async def noop_hook(_ctx):
        return None

    async def noop_advisory(*_args, **_kwargs):
        return None

    async def response_projector(_ctx):
        return {"source_refs": [f"session-run-outcome://{uuid4()}"], "receipt_sha256": "d" * 64}

    def seal(**_kwargs):
        return SimpleNamespace(boundary_id=None, event_id=f"evt_{'1' * 32}", sequence=33)

    async def maybe_bridge(**_kwargs):
        return bridge

    processor = WebTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker,
        bridge_to_t0=maybe_bridge,
        turn_boundary_projector=noop_hook,
        emit_advisory_hook=noop_advisory,
        response_projector=response_projector,
        summary_generator=summary_generator,
        seal_t0=seal,
    )
    processor._load = lambda _item: _async_value(material)
    processor._verify_t0_frontier = lambda _material: _async_value(None)
    return processor


async def _async_value(value):
    return value


async def _complete_turn_and_enqueue_boundary(db, *, task: RuntimeTask, terminal_sequence: int):
    task.status = "completed"
    task.completed_at = datetime.now(UTC)
    return await enqueue_terminal_boundary(
        db,
        task=task,
        event_kind="turn_stop",
        agent_id=task.parent_agent_id,
        session_id=task.parent_session_id,
        terminal_status="completed",
        authority_ref="runtime_task",
        authority_id=task.id,
        binding={"terminal_event_id": uuid4(), "terminal_sequence": terminal_sequence, "authority_sha256": "a" * 64},
    )


async def _submit_follow_up(db, seeded, *, content="Follow-up after the reply"):
    next_input_id = uuid4()
    receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, seeded.agent_id),
        user=await db.get(User, seeded.user_id), session=await db.get(ChatSession, seeded.session_id),
        content=content, source="r15i-summary-dispatch-test",
        input_id=next_input_id, idempotency_key=f"r15i:{next_input_id}")
    return next_input_id, receipt


async def _boundary_status(owner_sessionmaker, boundary_id):
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, boundary_id)
        return row.status if row is not None else "missing"


async def _summary_projection(owner_sessionmaker, session_id):
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, session_id)
    projection = (session.transcript_metadata_json or {}).get("terminal_summary_projection") or {}
    return session.summary, projection


async def test_follow_up_dispatches_while_summary_generation_is_blocked(owner_sessionmaker):
    tenant_id, _user_id, _agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=_user_id, agent_id=_agent_id, session_id=session_id,
        run_id=run_id)
    material = _summary_test_material(seeded, runtime_task_id=run_id, terminal_sequence=21,
        final_response="turn one answer")
    release = asyncio.Event()

    async def generate(_messages, **_kwargs):
        await release.wait()
        return "summary written after release"

    processor = _deferred_summary_processor(owner_sessionmaker, material, summary_generator=generate)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        boundary = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run_id),
            terminal_sequence=21)
        next_input_id, receipt = await _submit_follow_up(db, seeded)
        assert receipt["dispatch"]["kind"] == "deferred"
        assert receipt["dispatch"]["status"] == "waiting_for_terminal_boundary_ack"

    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i-a", task_types=("web_chat_turn",))
    assert [item.runtime_task_id for item in claimed] == [run_id]

    async def canonical(_db, item):
        return dict(item.binding)

    delivery = asyncio.create_task(service.process_terminal_boundary(item=claimed[0], worker_id="r15i-a",
        canonical_validator=canonical, process_callback=processor))
    assert await _wait_until(lambda: _boundary_delivered(owner_sessionmaker, boundary.id)), \
        "the boundary must be delivered while the summary generator is still blocked"
    assert await asyncio.wait_for(delivery, timeout=5) is True

    # The held follow-up dispatches and its run exists without waiting for the summary.
    async with owner_sessionmaker() as db:
        recovery = await recover_admitted_session_inputs_once(db, worker_id="r15i-a-input", tenant_id=tenant_id)
    assert recovery == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
    async with owner_sessionmaker() as db:
        next_input = await db.get(SessionTurnInput, next_input_id)
        assert next_input.target_run_id is not None
        assert await db.get(RuntimeTask, next_input.target_run_id) is not None

    summary, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert summary is None and projection.get("state") == "in_flight"

    release.set()

    async def summary_sealed():
        summary, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "sealed" and summary == "summary written after release"

    assert await _wait_until(summary_sealed)
    _, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert projection["terminal_sequence"] == 21


async def _boundary_delivered(owner_sessionmaker, boundary_id):
    return await _boundary_status(owner_sessionmaker, boundary_id) == "delivered"


async def test_follow_up_dispatches_when_summary_generation_raises(owner_sessionmaker):
    tenant_id, _user_id, _agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=_user_id, agent_id=_agent_id, session_id=session_id,
        run_id=run_id)
    material = _summary_test_material(seeded, runtime_task_id=run_id, terminal_sequence=21,
        final_response="turn one answer")

    async def generate(_messages, **_kwargs):
        raise TimeoutError("provider outcome is ambiguous")

    processor = _deferred_summary_processor(owner_sessionmaker, material, summary_generator=generate)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        boundary = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run_id),
            terminal_sequence=21)
        next_input_id, receipt = await _submit_follow_up(db, seeded)
        assert receipt["dispatch"]["kind"] == "deferred"

    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i-b", task_types=("web_chat_turn",))

    async def canonical(_db, item):
        return dict(item.binding)

    assert await service.process_terminal_boundary(item=claimed[0], worker_id="r15i-b",
        canonical_validator=canonical, process_callback=processor) is True

    async with owner_sessionmaker() as db:
        recovery = await recover_admitted_session_inputs_once(db, worker_id="r15i-b-input", tenant_id=tenant_id)
    assert recovery == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}

    async def failure_recorded():
        summary, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "needs_reconciliation" and summary is None

    assert await _wait_until(failure_recorded)
    _, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert projection["error_code"] == "TimeoutError"


async def test_two_quick_turns_keep_the_newer_summary(owner_sessionmaker):
    tenant_id, _user_id, _agent_id, session_id, run1_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=_user_id, agent_id=_agent_id, session_id=session_id,
        run_id=run1_id)
    run2_id = uuid4()
    material1 = _summary_test_material(seeded, runtime_task_id=run1_id, terminal_sequence=21,
        final_response="turn one answer")
    material2 = _summary_test_material(seeded, runtime_task_id=run2_id, terminal_sequence=22,
        final_response="turn two answer")
    release_turn_one = asyncio.Event()

    async def generate(messages, **_kwargs):
        if messages[-1]["content"] == "turn one answer":
            await release_turn_one.wait()
            return "turn one summary"
        return "turn two summary"

    processor = _deferred_summary_processor(owner_sessionmaker, material1, summary_generator=generate)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        boundary1 = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run1_id),
            terminal_sequence=21)
        db.add(RuntimeTask(
            id=run2_id, task_type="web_chat_turn", status="running",
            parent_agent_id=_agent_id, child_agent_id=_agent_id, tenant_id=tenant_id,
            parent_session_id=str(session_id), child_session_id=str(session_id),
            root_user_id=_user_id, root_session_id=str(session_id), root_runtime_task_id=run2_id,
            prompt="second turn", metadata_json={"turn_id": f"turn-{run2_id.hex}"},
            terminal_boundary_generation=1,
        ))
        await db.flush()
        boundary2 = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run2_id),
            terminal_sequence=22)
        next_input_id, receipt = await _submit_follow_up(db, seeded)
        assert receipt["dispatch"]["kind"] == "deferred"

    async def canonical(_db, item):
        return dict(item.binding)

    claimed1 = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i-c", task_types=("web_chat_turn",),
        limit=1)
    assert [item.runtime_task_id for item in claimed1] == [run1_id]
    delivery1 = asyncio.create_task(service.process_terminal_boundary(item=claimed1[0], worker_id="r15i-c",
        canonical_validator=canonical, process_callback=processor))
    assert await _wait_until(lambda: _boundary_delivered(owner_sessionmaker, boundary1.id)), \
        "turn one must deliver while its summary generator is still blocked"
    assert await asyncio.wait_for(delivery1, timeout=5) is True

    async def turn_one_summary_in_flight():
        _, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "in_flight" and projection.get("terminal_sequence") == 21

    assert await _wait_until(turn_one_summary_in_flight)

    processor2 = _deferred_summary_processor(owner_sessionmaker, material2, summary_generator=generate)
    claimed2 = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i-c2", task_types=("web_chat_turn",),
        limit=1)
    assert [item.runtime_task_id for item in claimed2] == [run2_id]
    assert await service.process_terminal_boundary(item=claimed2[0], worker_id="r15i-c2",
        canonical_validator=canonical, process_callback=processor2) is True

    async def turn_two_summary_sealed():
        summary, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "sealed" and summary == "turn two summary"

    assert await _wait_until(turn_two_summary_sealed), \
        "turn two must seal its own summary while turn one's generator is still blocked"

    # Releasing the older turn's generator must not overwrite the newer summary.
    release_turn_one.set()
    from app.services.web_terminal_boundary_processor import _SUMMARY_PROJECTION_TASKS
    assert await _wait_until(lambda: _async_value(not _SUMMARY_PROJECTION_TASKS))
    summary, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert summary == "turn two summary"
    assert projection["terminal_sequence"] == 22
    assert projection["runtime_task_id"] == str(run2_id)

    async with owner_sessionmaker() as db:
        recovery = await recover_admitted_session_inputs_once(db, worker_id="r15i-c-input", tenant_id=tenant_id)
    assert recovery == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}


async def test_unprojected_required_parts_still_hold_the_next_input(owner_sessionmaker):
    tenant_id, _user_id, _agent_id, _session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=_user_id, agent_id=_agent_id, session_id=_session_id,
        run_id=run_id)
    material = _summary_test_material(seeded, runtime_task_id=run_id, terminal_sequence=21,
        final_response="turn one answer")

    async def generate(_messages, **_kwargs):
        return "summary must not run while required parts are pending"

    # The canonical transcript never reaches T0, so the boundary cannot deliver.
    processor = _deferred_summary_processor(owner_sessionmaker, material, summary_generator=generate, bridge=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        boundary = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run_id),
            terminal_sequence=21)
        next_input_id, _receipt = await _submit_follow_up(db, seeded)

    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i-d", task_types=("web_chat_turn",))

    async def canonical(_db, item):
        return dict(item.binding)

    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryPending

    with pytest.raises(WebTerminalBoundaryPending, match="T0 projection is pending"):
        await service.process_terminal_boundary(item=claimed[0], worker_id="r15i-d",
            canonical_validator=canonical, process_callback=processor)
    assert await service.fail_terminal_boundary(item=claimed[0], worker_id="r15i-d",
        error=WebTerminalBoundaryPending("terminal transcript T0 projection is pending")) == "retry"
    assert await _boundary_status(owner_sessionmaker, boundary.id) == "pending"

    # The undelivered boundary keeps the follow-up out of the dispatch
    # candidates entirely: it stays durably held, not claimed-and-deferred.
    async with owner_sessionmaker() as db:
        recovery = await recover_admitted_session_inputs_once(db, worker_id="r15i-d-input", tenant_id=tenant_id)
        assert recovery == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        from app.services.session_input_dispatch import discover_admitted_session_input_locators

        assert await discover_admitted_session_input_locators(db, tenant_id=tenant_id) == []
        admission = await db.scalar(select(SessionInputAdmission).where(
            SessionInputAdmission.input_id == next_input_id))
        assert admission.dispatch_state == "pending"


async def _dead_letter_turn_two_boundary(owner_sessionmaker, seeded, *, terminal_sequence: int):
    from uuid import uuid4 as _uuid4

    run2_id = _uuid4()
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(
            id=run2_id, task_type="web_chat_turn", status="running",
            parent_agent_id=seeded.agent_id, child_agent_id=seeded.agent_id,
            tenant_id=seeded.tenant_id,
            parent_session_id=str(seeded.session_id), child_session_id=str(seeded.session_id),
            root_user_id=seeded.user_id, root_session_id=str(seeded.session_id), root_runtime_task_id=run2_id,
            prompt="second turn", metadata_json={"turn_id": f"turn-{run2_id.hex}"},
            terminal_boundary_generation=1,
        ))
        await db.flush()
        boundary2 = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run2_id),
            terminal_sequence=terminal_sequence)
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, boundary2.id)
        # An unrelated dead-letter cause: the T0 bridge never projected.
        row.status = "dead_letter"
        row.last_error = "WebTerminalBoundaryPending: terminal transcript T0 projection is pending"
        await db.commit()
    return run2_id, boundary2


@pytest.mark.parametrize("disposition", [None, "retry"])
@pytest.mark.parametrize("older_state", ["needs_reconciliation", "in_flight"])
async def test_older_unresolved_summary_does_not_block_newer_dead_letter_redrive(
    owner_sessionmaker, older_state, disposition
):
    """R15i round-2 B1: a held projection of turn N must not gate turn N+1's
    dead letter. Before the fix, ``prepare_web_summary_retry`` counted ANY
    in_flight/needs_reconciliation projection as this dead letter's own, so
    both redrive variants raised and the session stayed stuck."""
    from app.services.web_terminal_boundary_processor import _SUMMARY_PROJECTION_TASKS

    tenant_id, user_id, _agent_id, session_id, run1_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=user_id, agent_id=_agent_id, session_id=session_id,
        run_id=run1_id)
    material1 = _summary_test_material(seeded, runtime_task_id=run1_id, terminal_sequence=21,
        final_response="turn one answer")
    release = asyncio.Event()
    provider_calls = 0

    async def generate(_messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if older_state == "in_flight":
            await release.wait()
            return "turn one summary"
        raise TimeoutError("provider timeout")

    processor1 = _deferred_summary_processor(owner_sessionmaker, material1, summary_generator=generate)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run1_id),
            terminal_sequence=21)
        await db.commit()
    claimed1 = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i2-a", task_types=("web_chat_turn",),
        limit=1)

    async def canonical(_db, item):
        return dict(item.binding)

    assert await service.process_terminal_boundary(item=claimed1[0], worker_id="r15i2-a",
        canonical_validator=canonical, process_callback=processor1) is True

    async def older_unresolved():
        _, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == older_state and (older_state == "needs_reconciliation"
            or provider_calls == 1)

    assert await _wait_until(older_unresolved)
    if older_state == "in_flight":
        # Simulate a redeploy: the event loop cancels the detached task
        # mid-provider-call and the projection stays in_flight with no writer.
        for task in list(_SUMMARY_PROJECTION_TASKS):
            task.cancel()
        await asyncio.gather(*list(_SUMMARY_PROJECTION_TASKS), return_exceptions=True)
        _, projection = await _summary_projection(owner_sessionmaker, session_id)
        assert projection.get("state") == "in_flight"

    run2_id, boundary2 = await _dead_letter_turn_two_boundary(owner_sessionmaker, seeded, terminal_sequence=22)

    # While the dead letter stands, every follow-up is refused outright.
    async with owner_sessionmaker() as db:
        _input_id, receipt = await _submit_follow_up(db, seeded)
    assert receipt["dispatch"]["kind"] == "rejected"

    # The older turn's unresolved summary must not gate this dead letter's
    # redrive, with or without an operator summary disposition.
    redriven = await service.redrive_dead_letter(tenant_id=tenant_id, outbox_id=boundary2.id,
        actor_user_id=user_id, reason="r15i2 older summary must not block this boundary",
        summary_disposition=disposition)
    assert redriven.status == "pending"

    material2 = _summary_test_material(seeded, runtime_task_id=run2_id, terminal_sequence=22,
        final_response="turn two answer")

    async def generate_two(_messages, **_kwargs):
        return "turn two summary"

    processor2 = _deferred_summary_processor(owner_sessionmaker, material2, summary_generator=generate_two)
    claimed2 = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i2-b", task_types=("web_chat_turn",),
        limit=1)
    assert [item.runtime_task_id for item in claimed2] == [run2_id]
    assert await service.process_terminal_boundary(item=claimed2[0], worker_id="r15i2-b",
        canonical_validator=canonical, process_callback=processor2) is True
    assert await _wait_until(lambda: _boundary_delivered(owner_sessionmaker, boundary2.id))

    async def newer_summary_sealed():
        summary, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "sealed" and summary == "turn two summary"

    assert await _wait_until(newer_summary_sealed), \
        "the redriven turn's own summary must supersede the older unresolved projection"
    _, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert projection["terminal_sequence"] == 22
    assert projection["runtime_task_id"] == str(run2_id)


async def test_ack_gap_replay_after_call_return_invokes_provider_once_and_seals_once(owner_sessionmaker):
    """R15i round-2 N2: the ack can fail after ``__call__`` has returned, so
    the outbox reprocesses the boundary and a second detached task is spawned
    for the same turn while the first is still inside the provider call."""
    from app.services.web_terminal_boundary_processor import _SUMMARY_PROJECTION_TASKS

    tenant_id, _user_id, _agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    seeded = SimpleNamespace(tenant_id=tenant_id, user_id=_user_id, agent_id=_agent_id, session_id=session_id,
        run_id=run_id)
    material = _summary_test_material(seeded, runtime_task_id=run_id, terminal_sequence=21,
        final_response="turn one answer")
    release = asyncio.Event()
    provider_calls = 0

    async def generate(_messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        await release.wait()
        return "the only summary"

    processor = _deferred_summary_processor(owner_sessionmaker, material, summary_generator=generate)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    async with owner_sessionmaker() as db:
        boundary = await _complete_turn_and_enqueue_boundary(db, task=await db.get(RuntimeTask, run_id),
            terminal_sequence=21)
        await db.commit()

    async def canonical(_db, item):
        return dict(item.binding)

    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="r15i2-c", task_types=("web_chat_turn",),
        limit=1)
    assert await service.process_terminal_boundary(item=claimed[0], worker_id="r15i2-c",
        canonical_validator=canonical, process_callback=processor) is True
    assert await _wait_until(lambda: _boundary_delivered(owner_sessionmaker, boundary.id))

    async def first_attempt_inside_provider():
        _, projection = await _summary_projection(owner_sessionmaker, session_id)
        return projection.get("state") == "in_flight" and provider_calls == 1

    assert await _wait_until(first_attempt_inside_provider)
    # The detached task carries the runtime snapshot prefix.
    assert [task.get_name() for task in _SUMMARY_PROJECTION_TASKS] == [
        f"runtime-terminal-boundary-summary:{run_id}"
    ]

    # Stands in for the outbox replay after a failed ack (the ack above
    # succeeded): run the callback, and its spawn, a second time after
    # ``__call__`` has returned.
    await processor(claimed[0])

    release.set()
    await asyncio.gather(*list(_SUMMARY_PROJECTION_TASKS), return_exceptions=True)

    summary, projection = await _summary_projection(owner_sessionmaker, session_id)
    assert provider_calls == 1, "the replayed turn must not invoke the provider a second time"
    assert summary == "the only summary"
    assert projection["state"] == "sealed"
    assert projection["terminal_sequence"] == 21
    assert projection["attempt_count"] == 1, "the slot was claimed and sealed exactly once"
