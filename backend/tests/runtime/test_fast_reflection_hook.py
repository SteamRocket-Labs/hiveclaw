from __future__ import annotations

import uuid
from pathlib import Path

import pytest


def test_runtime_terminal_response_receipt_resolves_exact_boundary_ref() -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _committed_response_base_ref

    boundary_id = uuid.uuid4()
    task_id = uuid.uuid4()
    idempotency_key = "a" * 64
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        metadata={
            "runtime_task_id": str(task_id),
            "terminal_boundary_id": str(boundary_id),
            "terminal_boundary_idempotency_key": idempotency_key,
            "response_commit": {
                "commit_kind": "runtime_terminal_boundary",
                "runtime_task_id": str(task_id),
                "terminal_boundary_id": str(boundary_id),
                "idempotency_key": idempotency_key,
                "source_refs": [f"runtime-terminal-boundary://{boundary_id}"],
            },
        },
    )

    assert _committed_response_base_ref(ctx) == f"runtime-terminal-boundary://{boundary_id}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_task_id", str(uuid.uuid4())),
        ("terminal_boundary_id", str(uuid.uuid4())),
        ("idempotency_key", "b" * 64),
        ("source_refs", []),
    ],
)
def test_runtime_terminal_response_receipt_rejects_binding_drift(field, value) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _committed_response_base_ref

    boundary_id = uuid.uuid4()
    task_id = uuid.uuid4()
    idempotency_key = "a" * 64
    receipt = {
        "commit_kind": "runtime_terminal_boundary",
        "runtime_task_id": str(task_id),
        "terminal_boundary_id": str(boundary_id),
        "idempotency_key": idempotency_key,
        "source_refs": [f"runtime-terminal-boundary://{boundary_id}"],
    }
    receipt[field] = value
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        metadata={
            "runtime_task_id": str(task_id),
            "terminal_boundary_id": str(boundary_id),
            "terminal_boundary_idempotency_key": idempotency_key,
            "response_commit": receipt,
        },
    )

    assert _committed_response_base_ref(ctx) == ""


async def test_response_complete_fast_reflection_hook_awaits_durable_candidate(monkeypatch, tmp_path: Path) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response

    scheduled: list[dict[str, object]] = []

    async def fake_create(**kwargs):
        scheduled.append(kwargs)
        return {"status": "candidate_created"}

    async def fake_learning_brain(**kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["messages"] == [{"role": "user", "content": "错了，下次用 npm。"}]
        return {
            "method": "learning_brain_agent",
            "signal_type": "user_preference_correction",
            "lesson": "Use npm for this repository.",
            "confidence": 0.87,
            "learning_brain_decision": {
                "schema": "fast_reflection_learning_brain_decision.v1",
                "signal_type": "user_preference_correction",
                "lesson": "Use npm for this repository.",
                "confidence": 0.87,
                "container": "session_learning",
                "promotion_intent": "project_only",
                "rationale": "User corrected the project package manager.",
                "evidence_refs": ["message:0"],
                "boundary_checks": {"not_direct_memory_write": True},
            },
        }

    monkeypatch.setattr("app.runtime.hooks_setup._create_fast_reflection_candidate", fake_create)
    monkeypatch.setattr("app.runtime.hooks_setup._run_fast_reflection_learning_brain", fake_learning_brain)
    monkeypatch.setattr("app.runtime.hooks_setup._agent_data_root", lambda: tmp_path)

    agent_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    receipt = await _fast_reflection_on_response(
        HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=str(agent_id),
            session_id="session-1",
            source="web",
            messages=[{"role": "user", "content": "错了，下次用 npm。"}],
            metadata={
                "tenant_id": str(uuid.uuid4()),
                "response_commit": {
                    "schema": "hive.response_commit.v1",
                    "committed": True,
                    "commit_kind": "session_v2_terminal_outcome",
                    "idempotency_key": f"session-run-outcome:{outcome_id}",
                    "source_refs": [f"session-run-outcome://{outcome_id}"],
                },
            },
        )
    )

    assert len(scheduled) == 1
    assert scheduled[0]["data_root"] == tmp_path
    assert scheduled[0]["agent_id"] == agent_id
    assert scheduled[0]["session_id"] == "session-1"
    assert scheduled[0]["metadata"]["fast_reflection_classification"]["method"] == "learning_brain_agent"
    assert scheduled[0]["metadata"]["fast_reflection_classification"]["lesson"] == "Use npm for this repository."
    assert (
        scheduled[0]["metadata"]["fast_reflection_classification"]["learning_brain_decision"]["container"]
        == "session_learning"
    )
    assert receipt == {
        "status": "candidate_created",
        "source_ref": f"session-run-outcome://{outcome_id}",
    }


@pytest.mark.asyncio
async def test_response_complete_fast_reflection_replay_short_circuits_learning_brain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    learning_brain_calls = 0

    async def learning_brain(**_kwargs):
        nonlocal learning_brain_calls
        learning_brain_calls += 1
        if learning_brain_calls > 1:
            raise AssertionError("durably committed disposition must bypass the provider")
        return {
            "method": "learning_brain_agent",
            "signal_type": "user_preference_correction",
            "lesson": "Use pnpm for this repository.",
            "confidence": 0.99,
        }

    monkeypatch.setattr("app.runtime.hooks_setup._run_fast_reflection_learning_brain", learning_brain)
    monkeypatch.setattr("app.runtime.hooks_setup._agent_data_root", lambda: tmp_path)
    metadata = {
        "tenant_id": str(tenant_id),
        "skill_candidate_loop_enabled": False,
        "response_commit": {
            "schema": "hive.response_commit.v1",
            "committed": True,
            "commit_kind": "session_v2_terminal_outcome",
            "idempotency_key": f"session-run-outcome:{outcome_id}",
            "source_refs": [f"session-run-outcome://{outcome_id}"],
        },
    }

    def context() -> HookContext:
        return HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=str(agent_id),
            session_id="session-replay",
            source="web",
            messages=[{"role": "user", "content": "下次这个项目统一使用 pnpm。"}],
            metadata=dict(metadata),
        )

    first = await _fast_reflection_on_response(context())
    replay = await _fast_reflection_on_response(context())

    assert learning_brain_calls == 1
    assert first is not None and replay is not None
    assert replay["idempotent_replay"] is True
    assert {
        key: first.get(key) for key in ("status", "candidate_id", "signal_type", "classification_method", "source_ref")
    } == {
        key: replay.get(key) for key in ("status", "candidate_id", "signal_type", "classification_method", "source_ref")
    }


@pytest.mark.asyncio
async def test_response_complete_low_signal_replay_is_durable_before_learning_brain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response

    agent_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    learning_brain_calls = 0

    async def learning_brain(**_kwargs):
        nonlocal learning_brain_calls
        learning_brain_calls += 1
        if learning_brain_calls > 1:
            raise AssertionError("durably skipped disposition must bypass the provider")
        return None

    monkeypatch.setattr("app.runtime.hooks_setup._run_fast_reflection_learning_brain", learning_brain)
    monkeypatch.setattr("app.runtime.hooks_setup._agent_data_root", lambda: tmp_path)

    def context() -> HookContext:
        return HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=str(agent_id),
            session_id="session-low-signal",
            source="web",
            messages=[{"role": "assistant", "content": "Task completed."}],
            metadata={
                "tenant_id": str(uuid.uuid4()),
                "response_commit": {
                    "schema": "hive.response_commit.v1",
                    "committed": True,
                    "commit_kind": "session_v2_terminal_outcome",
                    "idempotency_key": f"session-run-outcome:{outcome_id}",
                    "source_refs": [f"session-run-outcome://{outcome_id}"],
                },
            },
        )

    first = await _fast_reflection_on_response(context())
    replay = await _fast_reflection_on_response(context())

    assert learning_brain_calls == 1
    assert first is not None and replay is not None
    assert first["status"] == replay["status"] == "skipped"
    assert first["reason"] == replay["reason"] == "low_signal"
    assert replay["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_response_complete_fast_reflection_hook_holds_without_commit_receipt(monkeypatch) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response

    scheduled: list[dict[str, object]] = []

    async def unexpected_create(**kwargs):
        scheduled.append(kwargs)
        return {"status": "unexpected"}

    monkeypatch.setattr("app.runtime.hooks_setup._create_fast_reflection_candidate", unexpected_create)

    await _fast_reflection_on_response(
        HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=str(uuid.uuid4()),
            session_id="uncommitted-session",
            source="web",
            messages=[{"role": "user", "content": "Use npm next time."}],
            metadata={},
        )
    )

    assert scheduled == []


@pytest.mark.asyncio
async def test_committed_response_projection_propagates_durable_candidate_failure(monkeypatch) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import project_committed_response_complete

    async def no_session_projection(_ctx):
        return None

    async def fail_candidate(_ctx):
        raise RuntimeError("candidate transaction failed")

    monkeypatch.setattr("app.runtime.hooks_setup._project_on_response", no_session_projection)
    monkeypatch.setattr("app.runtime.hooks_setup._fast_reflection_on_response", fail_candidate)
    outcome_id = uuid.uuid4()
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        agent_id=str(uuid.uuid4()),
        session_id="committed-session",
        source="web",
        messages=[{"role": "assistant", "content": "Committed response"}],
        metadata={
            "response_commit": {
                "schema": "hive.response_commit.v1",
                "committed": True,
                "commit_kind": "session_v2_terminal_outcome",
                "idempotency_key": f"session-run-outcome:{outcome_id}",
                "source_refs": [f"session-run-outcome://{outcome_id}"],
            }
        },
    )

    with pytest.raises(RuntimeError, match="candidate transaction failed"):
        await project_committed_response_complete(ctx)


@pytest.mark.asyncio
async def test_committed_response_projection_marks_required_consumers_for_advisory_replay(monkeypatch) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import project_committed_response_complete

    outcome_id = uuid.uuid4()
    base_ref = f"session-run-outcome://{outcome_id}"

    async def session_projection(_ctx):
        return {"source_ref": base_ref, "transaction_id": str(uuid.uuid4()), "revision": 1}

    async def fast_reflection(_ctx):
        return {"source_ref": base_ref, "status": "skipped", "reason": "low_signal"}

    monkeypatch.setattr("app.runtime.hooks_setup._project_on_response", session_projection)
    monkeypatch.setattr("app.runtime.hooks_setup._fast_reflection_on_response", fast_reflection)
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        agent_id=str(uuid.uuid4()),
        session_id="committed-session",
        source="web",
        messages=[{"role": "assistant", "content": "Committed response"}],
        metadata={
            "response_commit": {
                "schema": "hive.response_commit.v1",
                "committed": True,
                "commit_kind": "session_v2_terminal_outcome",
                "idempotency_key": f"session-run-outcome:{outcome_id}",
                "source_refs": [base_ref],
            }
        },
    )

    await project_committed_response_complete(ctx)

    assert ctx.metadata["required_response_complete_projected"] is True


@pytest.mark.asyncio
async def test_required_response_marker_skips_builtin_advisory_handlers(monkeypatch) -> None:
    from app.runtime.hooks import HookContext, HookEvent
    from app.runtime.hooks_setup import _fast_reflection_on_response, _project_on_response

    monkeypatch.setattr(
        "app.runtime.hooks_setup._project_committed_response_session_memory",
        lambda *_args, **_kwargs: pytest.fail("session projection must not replay through generic advisory hook"),
    )
    monkeypatch.setattr(
        "app.runtime.hooks_setup._create_fast_reflection_candidate",
        lambda **_kwargs: pytest.fail("fast reflection must not replay through generic advisory hook"),
    )
    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        agent_id=str(uuid.uuid4()),
        session_id="committed-session",
        source="web",
        messages=[{"role": "assistant", "content": "Committed response"}],
        metadata={"required_response_complete_projected": True},
    )

    assert await _project_on_response(ctx) is None
    assert await _fast_reflection_on_response(ctx) is None


def test_memory_hook_plan_registers_fast_reflection_handler() -> None:
    from app.runtime.hooks import HookEvent
    from app.runtime.hooks_setup import _MEMORY_HOOK_REGISTRATIONS, export_memory_hook_plan

    plan = export_memory_hook_plan()

    assert len(_MEMORY_HOOK_REGISTRATIONS) == 18
    assert any(
        item["event"] == HookEvent.RESPONSE_COMPLETE.value
        and item["key"] == "memory.response_complete.fast_reflection"
        and item["handler_name"] == "fast_reflection_on_response"
        for item in plan
    )
    assert any(item["key"] == "evolution.heartbeat_tick_end.maintenance" for item in plan)


def test_memory_hook_plan_registers_permission_denied_audit_consumer() -> None:
    """B-5 regression guard: PERMISSION_DENIED is live-emitted, so a real
    observe-only audit consumer must be registered (not merely declared in the
    catalog). Reverting the registration drops this binding."""
    from app.runtime.hooks import HookEvent
    from app.runtime.hooks_setup import export_memory_hook_plan

    plan = export_memory_hook_plan()

    assert any(
        item["event"] == HookEvent.PERMISSION_DENIED.value
        and item["key"] == "governance.permission_denied.audit"
        and item["handler_name"] == "audit_permission_denied"
        for item in plan
    )
