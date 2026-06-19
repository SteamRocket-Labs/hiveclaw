from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.memory.t0.ledger import append_t0_session_event
from app.runtime.hooks import HookContext, HookEvent
from app.runtime.hooks_setup import _t0_session_close


def _patch_t0_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake = lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))  # noqa: E731
    monkeypatch.setattr("app.config.get_settings", fake)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", fake)
    monkeypatch.setattr("app.runtime.hooks_setup.get_settings", fake, raising=False)
    monkeypatch.setattr("app.services.t0_logger.get_settings", fake)


@pytest.mark.asyncio
async def test_session_close_seals_t0_segment_then_starts_canonical_t2_package(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "chat-session-1"
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="这一段关闭后要进入 T2 Segment Package。",
        data_root=tmp_path,
    )
    calls: list[dict] = []

    async def fake_build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="committed", package_dir=tmp_path / "pkg", job_id="job-test")

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fake_build)

    await _t0_session_close(
        HookContext(
            event=HookEvent.SESSION_CLOSE,
            agent_id=str(agent_id),
            session_id=session_id,
            source="web",
            messages=[{"role": "user", "content": "done"}],
            metadata={"reason": "user_left", "tenant_id": str(tenant_id), "agent_name": "Agent"},
        )
    )

    assert len(calls) == 1
    assert calls[0]["agent_id"] == agent_id
    assert calls[0]["tenant_id"] == tenant_id
    assert calls[0]["session_id"] == session_id
    assert calls[0]["t0_segment_id"] == first.segment_id
    assert calls[0]["data_root"] == tmp_path


def test_t0_to_t2_hook_plan_uses_projection_not_legacy_extract() -> None:
    from app.runtime.hooks_setup import export_memory_hook_plan

    plan = export_memory_hook_plan()
    response = [
        item
        for item in plan
        if item["event"] == HookEvent.RESPONSE_COMPLETE.value
        and item["key"] != "memory.response_complete.fast_reflection"
    ]
    pre_compaction = [item for item in plan if item["event"] == HookEvent.PRE_COMPACTION.value]

    assert response == [
        {
            "event": HookEvent.RESPONSE_COMPLETE.value,
            "handler_name": "project_on_response",
            "key": "memory.response_complete.session_projection",
            "profile_name": None,
            "has_matcher": False,
            "matcher_spec": None,
        }
    ]
    assert pre_compaction == [
        {
            "event": HookEvent.PRE_COMPACTION.value,
            "handler_name": "project_on_pre_compaction",
            "key": "memory.pre_compaction.session_projection",
            "profile_name": None,
            "has_matcher": False,
            "matcher_spec": None,
        }
    ]


@pytest.mark.asyncio
async def test_response_and_pre_compaction_do_not_call_legacy_extractor(monkeypatch, tmp_path) -> None:
    from app.runtime import hooks_setup

    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    updates: list[tuple] = []

    def fake_update_session_memory(update_agent_id, payload):
        updates.append((update_agent_id, payload))

    def fail_schedule(*_args, **_kwargs):
        raise AssertionError("legacy extract_agent.schedule_extract must not be used by T0->T2 runtime hooks")

    def fail_extract(*_args, **_kwargs):
        raise AssertionError("legacy extract_agent.extract must not be used by T0->T2 runtime hooks")

    monkeypatch.setattr("app.runtime.hooks_setup.update_session_memory", fake_update_session_memory)
    monkeypatch.setattr("app.services.extract_agent.extract_agent.schedule_extract", fail_schedule)
    monkeypatch.setattr("app.services.extract_agent.extract_agent.extract", fail_extract)

    ctx = HookContext(
        event=HookEvent.RESPONSE_COMPLETE,
        agent_id=str(agent_id),
        session_id="chat-session-1",
        source="web",
        messages=[{"role": "user", "content": "只更新临时 projection，不写旧 learnings。"}],
        metadata={"turn_count": 1},
    )
    await hooks_setup._project_on_response(ctx)
    await hooks_setup._project_on_pre_compaction(
        HookContext(
            event=HookEvent.PRE_COMPACTION,
            agent_id=str(agent_id),
            session_id="chat-session-1",
            source="web",
            messages=ctx.messages,
            metadata={"trigger": "budget"},
        )
    )

    assert [item[0] for item in updates] == [agent_id, agent_id]
    assert not (tmp_path / str(agent_id) / "memory" / "learnings").exists()


def test_startup_does_not_auto_replay_legacy_extraction_queue() -> None:
    main_py = Path(__file__).resolve().parents[2] / "app" / "main.py"
    source = main_py.read_text(encoding="utf-8")

    assert "replay_pending_extractions" not in source
    assert "record_extract_replay_outcome" not in source
