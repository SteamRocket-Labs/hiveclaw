from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.memory.t0.ledger import append_t0_session_event
from app.runtime.hooks import HookContext, HookEvent
from app.runtime.hooks_setup import _t0_delegation_end, _t0_dream_end, _t0_heartbeat_tick_end, _t0_session_close, _t0_trigger_end


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
        return SimpleNamespace(status="committed", package_dir=tmp_path / "pkg", job_id=kwargs["job_id"], issues=())

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


@pytest.mark.asyncio
async def test_trigger_end_seals_runtime_t0_segment_then_starts_canonical_t2_package(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "trigger_run-daily-research"
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="trigger fired",
        source="trigger",
        data_root=tmp_path,
    )
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="完成了一次用户配置的定时调研。",
        source="trigger",
        data_root=tmp_path,
    )
    calls: list[dict] = []

    async def fake_build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="committed", package_dir=tmp_path / "pkg", job_id=kwargs["job_id"], issues=())

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fake_build)

    await _t0_trigger_end(
        HookContext(
            event=HookEvent.TRIGGER_END,
            agent_id=str(agent_id),
            session_id=None,
            source="trigger",
            messages=[
                {"role": "system", "content": "trigger fired"},
                {"role": "assistant", "content": "完成了一次用户配置的定时调研。"},
            ],
            metadata={"tenant_id": str(tenant_id), "trigger_id": "daily-research", "trigger_name": "每日调研"},
        )
    )

    assert len(calls) == 1
    assert calls[0]["agent_id"] == agent_id
    assert calls[0]["tenant_id"] == tenant_id
    assert calls[0]["session_id"] == session_id
    assert calls[0]["t0_segment_id"] == first.segment_id
    assert calls[0]["data_root"] == tmp_path


@pytest.mark.asyncio
async def test_delegation_end_seals_runtime_t0_segment_then_starts_canonical_t2_package(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "delegation_run-worker-42"
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="子代理收到用户要求的竞品资料整理任务。",
        source="agent",
        data_root=tmp_path,
    )
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="assistant_message",
        role="assistant",
        content="子代理完成了用户要求的竞品资料整理。",
        source="agent",
        data_root=tmp_path,
    )
    calls: list[dict] = []

    async def fake_build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="committed", package_dir=tmp_path / "pkg", job_id=kwargs["job_id"], issues=())

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fake_build)

    await _t0_delegation_end(
        HookContext(
            event=HookEvent.DELEGATION_END,
            agent_id=str(agent_id),
            session_id=None,
            source="delegation",
            messages=[
                {"role": "assistant", "content": "子代理完成了用户要求的竞品资料整理。"},
            ],
            metadata={"tenant_id": str(tenant_id), "delegation_id": "worker-42"},
        )
    )

    assert len(calls) == 1
    assert calls[0]["agent_id"] == agent_id
    assert calls[0]["tenant_id"] == tenant_id
    assert calls[0]["session_id"] == session_id
    assert calls[0]["t0_segment_id"] == first.segment_id
    assert calls[0]["data_root"] == tmp_path


@pytest.mark.asyncio
async def test_trigger_end_does_not_fabricate_t0_when_runtime_transcript_missing(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()

    async def fail_build(**_kwargs):
        raise AssertionError("T2 must not build from a fabricated trigger summary")

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fail_build)

    await _t0_trigger_end(
        HookContext(
            event=HookEvent.TRIGGER_END,
            agent_id=str(agent_id),
            session_id="empty-trigger-session",
            source="trigger",
            messages=[{"role": "assistant", "content": "summary-only hook payload"}],
            metadata={"tenant_id": str(uuid4()), "trigger_id": "empty-trigger"},
        )
    )

    source_files = list(
        (tmp_path / str(agent_id) / "memory" / "t0" / "sessions" / "empty-trigger-session").glob(
            "segments/*/source.md"
        )
    )
    assert source_files == []


@pytest.mark.asyncio
async def test_system_distiller_runtime_events_do_not_enter_t2(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()

    async def fail_build(**_kwargs):
        raise AssertionError("heartbeat/dream runtime ledgers are system audit sources and must not enter T2")

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fail_build)

    await _t0_heartbeat_tick_end(
        HookContext(
            event=HookEvent.HEARTBEAT_TICK_END,
            agent_id=str(agent_id),
            source="heartbeat",
            messages=[{"role": "system", "content": "heartbeat checked memory state"}],
            metadata={"tick_id": "hb-1"},
        )
    )
    await _t0_dream_end(
        HookContext(
            event=HookEvent.DREAM_END,
            agent_id=str(agent_id),
            source="dream",
            messages=[{"role": "system", "content": "dream consolidation finished"}],
            metadata={"dream_id": "dream-1"},
        )
    )


@pytest.mark.asyncio
async def test_runtime_t0_can_explicitly_opt_out_of_t2_packaging(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()

    async def fail_build(**_kwargs):
        raise AssertionError("non-semantic runtime ledger must not enter T2")

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fail_build)

    await _t0_trigger_end(
        HookContext(
            event=HookEvent.TRIGGER_END,
            agent_id=str(agent_id),
            session_id=None,
            source="trigger",
            messages=[{"role": "system", "content": "health check completed"}],
            metadata={
                "tenant_id": str(uuid4()),
                "trigger_id": "health-check",
                "semantic_memory_eligible": False,
            },
        )
    )


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
            "key": "memory.pre_compaction.t0_checkpoint",
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


@pytest.mark.asyncio
async def test_pre_compaction_seals_t0_checkpoint_and_enqueues_t2_job(monkeypatch, tmp_path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events
    from app.runtime import hooks_setup

    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "long-running-chat"
    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        event_type="user_message",
        role="user",
        content="这是一段很长会话，压缩前必须形成可恢复 checkpoint。",
        data_root=tmp_path,
    )
    calls: list[dict] = []

    async def fake_build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="committed", package_dir=tmp_path / "pkg", job_id=kwargs["job_id"], issues=())

    monkeypatch.setattr("app.memory.t2.segment_package.build_t2_segment_package_with_llm", fake_build)

    await hooks_setup._project_on_pre_compaction(
        HookContext(
            event=HookEvent.PRE_COMPACTION,
            agent_id=str(agent_id),
            session_id=session_id,
            source="web",
            messages=[{"role": "user", "content": "压缩前的可恢复上下文。"}],
            metadata={"trigger": "budget", "tenant_id": str(tenant_id)},
        )
    )

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [event.event_type for event in events] == ["user_message", "segment_boundary"]
    assert events[-1].content == "pre_compaction:budget"
    assert len(calls) == 1
    assert calls[0]["agent_id"] == agent_id
    assert calls[0]["tenant_id"] == tenant_id
    assert calls[0]["session_id"] == session_id
    assert calls[0]["t0_segment_id"] == first.segment_id
    assert (
        tmp_path
        / str(agent_id)
        / "memory"
        / ".staging"
        / "t2_jobs"
        / calls[0]["job_id"]
        / "job_manifest.json"
    ).exists()


@pytest.mark.asyncio
async def test_session_close_with_empty_messages_only_seals_t0(monkeypatch, tmp_path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events
    from app.runtime import hooks_setup

    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    session_id = uuid4()
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="already persisted before close",
        data_root=tmp_path,
    )
    updates: list[tuple] = []

    def fake_update_session_memory(update_agent_id, payload):
        updates.append((update_agent_id, payload))

    monkeypatch.setattr("app.runtime.hooks_setup.update_session_memory", fake_update_session_memory)

    await hooks_setup._t0_session_close(
        HookContext(
            event=HookEvent.SESSION_CLOSE,
            agent_id=str(agent_id),
            session_id=str(session_id),
            source="web",
            messages=[],
            metadata={"reason": "invoke_complete"},
        )
    )

    assert updates == []
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [
        ("user_message", "user"),
        ("segment_boundary", "system"),
    ]


def test_startup_does_not_auto_replay_legacy_extraction_queue() -> None:
    main_py = Path(__file__).resolve().parents[2] / "app" / "main.py"
    source = main_py.read_text(encoding="utf-8")

    assert "replay_pending_extractions" not in source
    assert "record_extract_replay_outcome" not in source
