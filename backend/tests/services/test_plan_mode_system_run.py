"""Behavior contracts for the durable explicit System Plan launcher."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from tests.services.test_plan_mode_system_run_recovery import _seed_plan


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _stub_plan_file(monkeypatch, module) -> None:
    # Test Double rationale: filesystem slot allocation is outside the launcher
    # behavior under test; RuntimeTask and Plan authority remain real PostgreSQL.
    monkeypatch.setattr(module, "provision_agent_plan_file_slot", lambda *_args, **_kwargs: None)


@pytest.mark.asyncio
async def test_launch_arms_plan_mode_with_draft_id_then_resets(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services.plan_mode_runtime_context import (
        interactive_plan_mode_active,
        interactive_plan_mode_metadata,
    )

    seeded = await _seed_plan(owner_sessionmaker)
    _stub_plan_file(monkeypatch, system_run)
    captured: dict[str, object] = {}

    # Test Double rationale: isolate the external LLM provider while exercising
    # real Plan and durable RuntimeTask authority.
    async def invoke_without_network(request):
        captured.update(
            {
                "armed_active": interactive_plan_mode_active(),
                "armed_plan_id": interactive_plan_mode_metadata().get("plan_id"),
                "source": request.session_context.source,
                "plan_mode_active": request.session_context.plan_mode.active,
                "state_plan_id": request.session_context.plan_mode.plan_id,
                "state_plan_file_path": request.session_context.plan_mode.plan_file_path,
                "mirror_plan_file_path": request.session_context.metadata["plan_mode"].get("plan_file_path"),
                "max_tool_rounds": request.max_tool_rounds,
                "agent_id": request.agent_id,
            }
        )
        return SimpleNamespace(content="planned", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", invoke_without_network)

    returned = await system_run.launch_system_plan_run(
        seeded.plan,
        session_factory=owner_sessionmaker,
    )

    assert returned is seeded.plan
    assert captured == {
        "armed_active": True,
        "armed_plan_id": str(seeded.plan_id),
        "source": system_run.SYSTEM_PLAN_RUN_SOURCE,
        "plan_mode_active": True,
        "state_plan_id": str(seeded.plan_id),
        "state_plan_file_path": f"workspace/plans/{seeded.plan_id}.plan.md",
        "mirror_plan_file_path": f"workspace/plans/{seeded.plan_id}.plan.md",
        "max_tool_rounds": system_run.SYSTEM_PLAN_RUN_MAX_ROUNDS,
        "agent_id": seeded.agent_id,
    }
    assert system_run.SYSTEM_PLAN_RUN_MAX_ROUNDS == 200
    assert interactive_plan_mode_active() is False


@pytest.mark.asyncio
async def test_launch_passes_seed_context_and_trusted_scopes(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(owner_sessionmaker)
    _stub_plan_file(monkeypatch, system_run)
    scopes = [
        {
            "action_kind": "create_enabled_trigger",
            "target_ref": "trigger:new",
            "arguments": {"type": "cron", "config": {"expr": "0 9 * * *"}},
        }
    ]
    captured: dict[str, object] = {}

    async def invoke_without_network(request):
        captured["prompt"] = request.messages[0]["content"]
        captured["typed_scopes"] = request.session_context.plan_mode.authorization_scopes
        captured["mirrored_scopes"] = request.session_context.metadata["plan_mode"]["authorization_scopes"]
        return SimpleNamespace(content="planned", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", invoke_without_network)

    await system_run.launch_system_plan_run(
        seeded.plan,
        seed_context={
            "tool_name": "set_trigger",
            "action_kind": "create_enabled_trigger",
            "authorization_scopes": scopes,
        },
        session_factory=owner_sessionmaker,
    )

    prompt = str(captured["prompt"])
    assert "Plan Mode" in prompt
    assert "exit_plan_mode" in prompt
    assert seeded.plan.original_request in prompt
    assert "set_trigger" in prompt
    assert captured["typed_scopes"] == scopes
    assert captured["mirrored_scopes"] == scopes


@pytest.mark.asyncio
async def test_launch_uses_restart_stable_session_and_distinct_runtime_identity(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    from app.services import plan_mode_system_run as system_run

    seeded = await _seed_plan(
        owner_sessionmaker,
        with_root_runtime_task=False,
        with_session=False,
    )
    _stub_plan_file(monkeypatch, system_run)
    captured: list[tuple[str | None, str | None]] = []
    restored_pending_items: list[str] = []

    async def invoke_with_manifest(request):
        from app.runtime.recovery_manifest import load_recovery_manifest, persist_recovery_manifest

        captured.append(
            (
                request.session_context.session_id,
                request.session_context.metadata.get("runtime_task_id"),
            )
        )
        if len(captured) == 1:
            request.session_context.track_pending_item("resume plan authoring")
            assert persist_recovery_manifest(
                seeded.agent_id,
                request.session_context,
                data_root=tmp_path,
            )
        else:
            restored = load_recovery_manifest(
                seeded.agent_id,
                session_context=request.session_context,
                data_root=tmp_path,
            )
            assert restored is not None
            restored_pending_items.extend(restored.pending_items)
        return SimpleNamespace(content="planned", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", invoke_with_manifest)

    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)
    await system_run.launch_system_plan_run(seeded.plan, session_factory=owner_sessionmaker)

    expected_session_id = f"plan-{seeded.plan_id.hex}"
    expected_task_id = system_run.system_plan_runtime_task_id(seeded.plan_id)
    assert captured == [
        (expected_session_id, expected_task_id.hex),
        (expected_session_id, expected_task_id.hex),
    ]
    assert expected_task_id != seeded.plan_id
    assert restored_pending_items == ["resume plan authoring"]


@pytest.mark.asyncio
async def test_launch_is_fail_closed_when_invoke_raises(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run
    from app.services.plan_mode_runtime_context import interactive_plan_mode_active

    seeded = await _seed_plan(owner_sessionmaker)
    _stub_plan_file(monkeypatch, system_run)

    async def disconnected_provider(_request):
        raise RuntimeError("LLM exploded mid-plan")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", disconnected_provider)

    returned = await system_run.launch_system_plan_run(
        seeded.plan,
        session_factory=owner_sessionmaker,
    )

    assert returned is seeded.plan
    assert interactive_plan_mode_active() is False
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeTask, system_run.system_plan_runtime_task_id(seeded.plan_id))
        assert row is not None
        assert row.status == "resumable"


@pytest.mark.asyncio
async def test_launch_noops_without_agent_or_model(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services import plan_mode_system_run as system_run

    missing_agent_plan = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        requested_by_user_id=uuid4(),
        session_id="missing-agent-session",
        runtime_task_id=None,
        intent_type="in_session_execution",
        original_request="plan only",
        status="draft",
    )
    missing_model = await _seed_plan(owner_sessionmaker, with_model=False)
    invoked = 0

    async def must_not_invoke(_request):
        nonlocal invoked
        invoked += 1
        return SimpleNamespace(content="unexpected", tokens_used=0)

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", must_not_invoke)

    assert (
        await system_run.launch_system_plan_run(
            missing_agent_plan,
            session_factory=owner_sessionmaker,
        )
        is missing_agent_plan
    )
    assert (
        await system_run.launch_system_plan_run(
            missing_model.plan,
            session_factory=owner_sessionmaker,
        )
        is missing_model.plan
    )
    assert invoked == 0
