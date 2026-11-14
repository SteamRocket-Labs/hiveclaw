"""Bound only auxiliary model work; join all existing commits before delivery."""

import asyncio
import copy
import json
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.memory import metrics
from app.runtime import hooks_setup
from app.runtime.hooks import HookContext, HookEvent
from app.services import session_learning, session_memory
from app.services.fast_reflection_service import load_fast_reflection_disposition


@pytest.fixture
def projections(monkeypatch, tmp_path):
    agent_id, session_id, outcome_id = uuid4(), str(uuid4()), uuid4()
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        agent_id=str(agent_id),
        session_id=session_id,
        source="web",
        messages=[
            {"role": "user", "content": "Keep the report concise."},
            {"role": "assistant", "content": "The report is complete."},
            {"role": "assistant", "content": "Next I will verify the report."},
        ],
        metadata={
            "tenant_id": str(uuid4()),
            "user_id": str(uuid4()),
            "response_commit": {
                "schema": "hive.response_commit.v1",
                "committed": True,
                "commit_kind": "session_v2_terminal_outcome",
                "idempotency_key": str(outcome_id),
                "source_refs": [f"session-run-outcome://{outcome_id}"],
            },
        },
    )
    state = SimpleNamespace(
        ctx=ctx,
        root=tmp_path / "data",
        clients=[],
        close_errors=set(),
        low_signal=False,
        delays={"session_memory_writer": 0, "fast_reflection_learning_brain": 0},
    )
    now = datetime(2026, 11, 13, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(session_memory, "datetime", FixedDatetime)
    monkeypatch.setattr(session_learning, "_now", lambda: now)
    monkeypatch.setattr(hooks_setup, "_agent_data_root", lambda: state.root)
    monkeypatch.setattr(metrics, "_hook_failure_total", defaultdict(int))

    async def model_config(*_args):
        return {"provider": "openai", "model": "scripted", "max_output_tokens": 1000}

    class Client:
        def __init__(self, config):
            self.phase = config["_usage_source"]
            self.closed = False
            self.cancelled = False
            self.request = None
            state.clients.append(self)

        async def stream(self, **kwargs):
            self.task = asyncio.current_task()
            # Compare provider-visible inputs, not LLMMessage.created_at.
            self.request = {**kwargs, "messages": [message.to_openai_format() for message in kwargs["messages"]]}
            try:
                await asyncio.sleep(state.delays[self.phase])
                payload = {"current_state": "Model-authored state.", "pending_work": ["Model-authored next step."]}
                if self.phase == "fast_reflection_learning_brain":
                    payload = {
                        "signal_type": "user_preference_correction",
                        "lesson": "Keep reports concise.",
                        "confidence": 0.9,
                        "container": "session_learning",
                        "promotion_intent": "project_only",
                        "evidence_refs": ["message:0"],
                        "boundary_checks": {
                            "not_one_off": True,
                            "no_credentials": True,
                            "not_direct_memory_write": True,
                        },
                    }
                    if state.low_signal:
                        payload.update(signal_type="low_signal", lesson="")
                return SimpleNamespace(content=json.dumps(payload))
            except asyncio.CancelledError:
                self.cancelled = True
                self.cancelled_at = asyncio.get_running_loop().time()
                raise

        async def complete(self, **kwargs):
            return await self.stream(**kwargs)

        async def close(self):
            # Cancellation must finish provider cleanup before a fallback commits.
            await asyncio.sleep(0)
            self.closed = True
            if self.phase in state.close_errors:
                raise ConnectionError("PRIVATE_CLOSE_ERROR")

    monkeypatch.setattr(session_memory, "_get_session_memory_model_config", model_config)
    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", model_config)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", Client)
    monkeypatch.setattr("app.services.fast_reflection_learning_brain.create_llm_client_from_config", Client)
    return state


def _memory_path(state):
    return session_memory.get_session_memory_path(
        UUID(state.ctx.agent_id), session_id=state.ctx.session_id, data_root=state.root
    )


def _learning_path(state):
    return state.root / state.ctx.agent_id / "evolution" / "session_learning_projections.jsonl"


async def _bounded(state, monkeypatch, *, seconds=0.05):
    # Shorten only the production constant for focused failure-path tests.
    monkeypatch.setattr(hooks_setup, "AUX_PROJECTION_TIMEOUT_SECONDS", seconds, raising=False)
    async with asyncio.timeout(2):  # Hang guard also makes the unbounded baseline red.
        return await hooks_setup.project_committed_response_complete(copy.deepcopy(state.ctx))


def test_auxiliary_projection_wall_time_bound_is_three_seconds():
    assert hooks_setup.AUX_PROJECTION_TIMEOUT_SECONDS == 3.0


async def test_under_one_second_keeps_current_session_and_learning_bytes(projections):
    state = projections
    state.delays = dict.fromkeys(state.delays, 0.1)
    # Existing handlers, with no deadline, are the pre-R24a output oracle.
    await hooks_setup._project_on_response(copy.deepcopy(state.ctx))
    await hooks_setup._fast_reflection_on_response(copy.deepcopy(state.ctx))
    expected_memory, expected_learning = _memory_path(state).read_bytes(), _learning_path(state).read_bytes()
    original_requests = [client.request for client in state.clients]
    assert len(original_requests) == 2

    state.root = state.root.parent / "bounded"
    receipt = await hooks_setup.project_committed_response_complete(copy.deepcopy(state.ctx))
    assert _memory_path(state).read_bytes() == expected_memory
    assert _learning_path(state).read_bytes() == expected_learning
    assert [client.request for client in state.clients[2:]] == original_requests
    assert all(client.closed and not client.cancelled for client in state.clients)
    assert receipt["source_refs"] == state.ctx.metadata["response_commit"]["source_refs"]


async def test_timeout_stores_exact_existing_deterministic_session_fallback(projections, monkeypatch, caplog):
    state = projections
    state.delays["session_memory_writer"] = 30
    await _bounded(state, monkeypatch)
    metadata = hooks_setup._session_memory_metadata(state.ctx)
    expected = session_memory._mark_deterministic_fallback(
        session_memory.build_session_memory_payload_from_messages(state.ctx.messages, metadata=metadata),
        reason="model_failure:TimeoutError",
    )
    path = session_memory.update_session_memory(
        UUID(state.ctx.agent_id), expected, data_root=state.root.parent / "expected"
    )
    assert _memory_path(state).read_bytes() == path.read_bytes()
    assert _learning_path(state).exists(), "The unrelated, fast reflection result must still be committed"
    assert "aux_projection_timeout" in caplog.text and "projection=session_memory" in caplog.text


async def test_timeout_skips_reflection_without_candidate_or_learning_write(projections, monkeypatch, caplog):
    state = projections
    state.delays["fast_reflection_learning_brain"] = 30
    await _bounded(state, monkeypatch)
    root = state.root / state.ctx.agent_id
    assert not _learning_path(state).exists()
    assert not (root / "evolution" / "evolution_ledger.jsonl").exists()
    # The existing skip path retains its replay receipt, but writes no lesson.
    disposition = load_fast_reflection_disposition(
        data_root=state.root,
        agent_id=UUID(state.ctx.agent_id),
        session_id=state.ctx.session_id,
        response_idempotency_key=state.ctx.metadata["response_commit"]["idempotency_key"],
    )
    # R24a round 2: a timed-out pass is not a model verdict of low signal.
    assert disposition == {"status": "skipped", "reason": "aux_projection_timeout", "idempotent_replay": True}
    assert (
        session_memory.load_session_memory(
            UUID(state.ctx.agent_id), session_id=state.ctx.session_id, data_root=state.root
        ).current_state
        == "Model-authored state."
    )
    assert "aux_projection_timeout" in caplog.text and "projection=fast_reflection" in caplog.text


async def test_timeout_leaves_no_pending_auxiliary_task(projections, monkeypatch):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    before = asyncio.all_tasks()
    await _bounded(state, monkeypatch)
    assert len(state.clients) == 2
    assert all(client.cancelled and client.closed and client.task.done() for client in state.clients)
    assert not (asyncio.all_tasks() - before)


async def test_no_asset_write_after_boundary_delivery(projections, monkeypatch):
    state = projections
    state.delays = dict.fromkeys(state.delays, 0.2)
    await _bounded(state, monkeypatch)

    def snapshot():
        return {
            str(path.relative_to(state.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in state.root.rglob("*")
            if path.is_file()
        }

    delivered = snapshot()
    assert delivered and all(client.closed for client in state.clients)
    await asyncio.sleep(0.3)  # Beyond both scripted calls' original completion time.
    assert snapshot() == delivered
    assert all(client.cancelled for client in state.clients)


async def test_both_calls_share_deadline_despite_reflection_preflight(projections, monkeypatch):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    native = hooks_setup._load_fast_reflection_disposition

    async def delayed_preflight(**kwargs):
        await asyncio.sleep(0.25)
        return await native(**kwargs)

    monkeypatch.setattr(hooks_setup, "_load_fast_reflection_disposition", delayed_preflight)
    start = asyncio.get_running_loop().time()
    await _bounded(state, monkeypatch, seconds=0.4)
    assert all(client.cancelled and client.closed for client in state.clients)
    assert max(client.cancelled_at for client in state.clients) - start < 0.6
    assert abs(state.clients[0].cancelled_at - state.clients[1].cancelled_at) < 0.1


async def test_timeout_replay_reuses_fallback_and_skip_without_model_calls(projections, monkeypatch):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    receipt = await _bounded(state, monkeypatch)
    memory = _memory_path(state).read_bytes()
    state.delays = dict.fromkeys(state.delays, 0)
    replay = await _bounded(state, monkeypatch)
    assert len(state.clients) == 2, "Replay must not retry timed-out learning or replace the committed fallback"
    assert replay == receipt
    assert _memory_path(state).read_bytes() == memory
    assert not _learning_path(state).exists()


async def test_auxiliary_deadline_never_detaches_a_durable_write_thread(projections, monkeypatch):
    import threading
    from app.services import fast_reflection_service

    state = projections
    entered, release = threading.Event(), threading.Event()
    native = fast_reflection_service.create_fast_reflection_candidate

    def delayed_commit(**kwargs):
        entered.set()
        assert release.wait(1)
        return native(**kwargs)

    monkeypatch.setattr(fast_reflection_service, "create_fast_reflection_candidate", delayed_commit)
    boundary = asyncio.create_task(_bounded(state, monkeypatch))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.1)  # The shared model deadline has already passed.
        assert not boundary.done(), "The join must await a started commit, never cancel its to_thread await"
        release.set()
        await boundary
        assert _learning_path(state).exists()
        assert all(client.closed and not client.cancelled for client in state.clients)
    finally:
        release.set()
        await asyncio.gather(boundary, return_exceptions=True)


def _disposition(state):
    return load_fast_reflection_disposition(
        data_root=state.root,
        agent_id=UUID(state.ctx.agent_id),
        session_id=state.ctx.session_id,
        response_idempotency_key=state.ctx.metadata["response_commit"]["idempotency_key"],
    )


def _assert_timeout_observation(state, caplog):
    assert dict(metrics._hook_failure_total) == {
        ("response_complete", "session_memory", "aux_projection_timeout"): 1,
        ("response_complete", "fast_reflection", "aux_projection_timeout"): 1,
    }
    warnings = [
        record.getMessage() for record in caplog.records if "aux_projection_timeout projection=" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert all(
        f"agent={state.ctx.agent_id}" in message and f"session={state.ctx.session_id}" in message
        for message in warnings
    )


async def test_timeout_counts_each_projection_once_including_replay(projections, monkeypatch, caplog):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    receipt = await _bounded(state, monkeypatch)
    _assert_timeout_observation(state, caplog)
    assert await _bounded(state, monkeypatch) == receipt
    assert len(state.clients) == 2
    _assert_timeout_observation(state, caplog)


async def test_real_low_signal_reason_and_committed_replay_are_unchanged(projections, monkeypatch):
    state = projections
    state.low_signal = True
    receipt = await _bounded(state, monkeypatch)
    expected = {"status": "skipped", "reason": "low_signal", "idempotent_replay": True}
    assert _disposition(state) == expected
    assert not _learning_path(state).exists()
    # Even a later timeout budget cannot reinterpret an already committed verdict.
    state.delays = dict.fromkeys(state.delays, 30)
    state.low_signal = False
    assert await _bounded(state, monkeypatch) == receipt
    assert _disposition(state) == expected
    assert len(state.clients) == 2
    assert not metrics._hook_failure_total


async def test_close_errors_preserve_timeout_marker_metrics_and_cancellation(projections, monkeypatch, caplog):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    state.close_errors = set(state.delays)
    before = asyncio.all_tasks()
    await _bounded(state, monkeypatch)
    payload = session_memory.load_session_memory(
        UUID(state.ctx.agent_id), session_id=state.ctx.session_id, data_root=state.root
    )
    assert payload.source == "deterministic_fallback:model_failure:TimeoutError;source=web"
    assert _disposition(state)["reason"] == "aux_projection_timeout"
    _assert_timeout_observation(state, caplog)
    assert "ConnectionError" in caplog.text and "PRIVATE_CLOSE_ERROR" not in caplog.text
    assert len(state.clients) == 2
    assert all(client.closed and client.cancelled and client.task.done() for client in state.clients)
    assert not (asyncio.all_tasks() - before)


async def test_close_errors_without_cancellation_keep_existing_failure_behavior(projections, monkeypatch, caplog):
    state = projections
    state.close_errors = set(state.delays)
    await _bounded(state, monkeypatch)
    payload = session_memory.load_session_memory(
        UUID(state.ctx.agent_id), session_id=state.ctx.session_id, data_root=state.root
    )
    assert payload.source == "deterministic_fallback:model_failure:ConnectionError;source=web"
    assert _disposition(state)["reason"] == "low_signal"
    assert not _learning_path(state).exists()
    assert all(client.closed and not client.cancelled for client in state.clients)
    assert not metrics._hook_failure_total
    assert "aux_projection_timeout" not in caplog.text


async def test_outer_cancellation_survives_close_errors_without_fallback_write(projections):
    state = projections
    state.delays = dict.fromkeys(state.delays, 30)
    state.close_errors = set(state.delays)
    before = asyncio.all_tasks()
    boundary = asyncio.create_task(hooks_setup.project_committed_response_complete(copy.deepcopy(state.ctx)))
    try:
        async with asyncio.timeout(1):
            while len(state.clients) < 2 or not all(client.request for client in state.clients):
                await asyncio.sleep(0)
        boundary.cancel()
        with pytest.raises(asyncio.CancelledError):
            await boundary
        assert all(client.closed and client.cancelled and client.task.done() for client in state.clients)
        assert not _memory_path(state).exists()
        assert _disposition(state) is None
        assert not metrics._hook_failure_total
        assert not (asyncio.all_tasks() - before)
    finally:
        boundary.cancel()
        await asyncio.gather(boundary, return_exceptions=True)
