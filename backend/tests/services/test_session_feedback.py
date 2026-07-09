from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True


def test_session_feedback_default_writer_is_explicit_overlay_not_t3_adapter() -> None:
    from app.services.session_feedback import record_session_feedback

    writer = record_session_feedback.__kwdefaults__["append_memory"]

    assert writer.__name__ == "write_session_feedback_overlay"
    assert writer.__module__ == "app.services.session_feedback"


@pytest.mark.asyncio
async def test_record_useful_session_feedback_persists_event_audit_and_t3(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.models.audit import AuditLog
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(
            status="overlay",
            category="feedback",
            entry_id="explicit-feedback-1",
            path="memory/explicit/entries/explicit-feedback-1.md",
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="The answer gave the exact deployment checklist.",
        message_id=uuid.uuid4(),
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    assert db.flushed is True
    assert any(isinstance(row, SessionFeedbackEvent) for row in db.added)
    assert any(isinstance(row, AuditLog) and row.action == "session_feedback.recorded" for row in db.added)
    assert calls[0][1]["evidence"] == "user_stated"
    assert calls[0][1]["proposed_by"] == "owner_feedback"
    assert calls[0][1]["category"] == "feedback"
    assert calls[0][1]["data_root"] == tmp_path
    assert "deployment checklist" in calls[0][1]["content"]
    assert result["label"] == "useful"
    assert result["calibration_result"]["t3_status"] == "overlay"
    assert result["calibration_result"]["entry_id"] == "explicit-feedback-1"


@pytest.mark.asyncio
async def test_record_session_feedback_writes_activation_credit_sidecar(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def fake_append_memory(*_args, **_kwargs):
        return T3AppendResult(
            status="overlay",
            category="feedback",
            entry_id="explicit-feedback-heat",
            path="memory/explicit/entries/explicit-feedback-heat.md",
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="misleading",
        reason="The recalled deployment fact was stale.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    calibration = result["calibration_result"]
    sidecar = calibration["heat_decay_sidecar"]
    activation_event = calibration["activation_event"]
    sidecar_path = tmp_path / str(agent_id) / sidecar["path"]

    assert activation_event["schema"] == "hive.ccplus.activation_event.v1"
    assert activation_event["event_type"] == "owner_feedback_misleading"
    assert activation_event["candidate_ref"]["candidate_id"] == "feedback_overlay:explicit-feedback-heat"
    assert activation_event["feedback"]["signal"] == "owner_feedback"
    assert activation_event["feedback"]["credit"] < 0
    assert sidecar["schema"] == "hive.ccplus.activation_feedback_sidecar.v1"
    assert sidecar["heat_delta"] < 0
    assert "decay_signal" not in sidecar
    assert sidecar_path.exists()
    sidecar_line = sidecar_path.read_text(encoding="utf-8")
    assert "owner_feedback_misleading" in sidecar_line
    assert "decay_signal" not in sidecar_line
    assert not (tmp_path / str(agent_id) / "memory" / "t0").exists()
    assert not (tmp_path / str(agent_id) / "memory" / "t2").exists()
    assert not (tmp_path / str(agent_id) / "memory" / "t3").exists()


def test_activation_feedback_sidecar_read_model_filters_session_and_skips_bad_lines(tmp_path) -> None:
    from app.services.session_feedback import read_activation_feedback_sidecar

    agent_id = uuid.uuid4()
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()
    sidecar_path = tmp_path / str(agent_id) / "memory" / "control" / "activation_feedback.jsonl"
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_text(
        "\n".join(
            [
                (
                    '{"schema":"hive.ccplus.activation_feedback_sidecar.v1",'
                    f'"agent_id":"{agent_id}","session_id":"{session_a}","label":"useful","created_at":"2026-07-09T01:00:00Z"}}'
                ),
                "{not-json",
                (
                    '{"schema":"hive.ccplus.activation_feedback_sidecar.v1",'
                    f'"agent_id":"{agent_id}","session_id":"{session_b}","label":"misleading","created_at":"2026-07-09T02:00:00Z"}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = read_activation_feedback_sidecar(tmp_path, agent_id, session_id=session_a, limit=10)

    assert result["schema"] == "hive.ccplus.activation_feedback_read_model.v1"
    assert result["path"] == "memory/control/activation_feedback.jsonl"
    assert result["total_lines"] == 3
    assert result["skipped_lines"] == 1
    assert result["matched_entries"] == 1
    assert result["truncated"] is False
    assert [entry["session_id"] for entry in result["entries"]] == [str(session_a)]


@pytest.mark.asyncio
async def test_activation_feedback_sidecar_writer_prunes_to_retention_limit(tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace as T3AppendResult

    import app.services.session_feedback as session_feedback

    monkeypatch.setattr(session_feedback, "_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES", 2)

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def fake_append_memory(*_args, **_kwargs):
        return T3AppendResult(status="overlay", category="feedback", entry_id=f"feedback-{uuid.uuid4().hex[:8]}")

    for label in ("useful", "misleading", "useful"):
        await session_feedback.record_session_feedback(
            db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(
                id=uuid.uuid4(),
                agent_id=agent_id,
                tenant_id=tenant_id,
                source_channel="web",
                created_at=datetime(2026, 7, 9, tzinfo=UTC),
            ),
            current_user=SimpleNamespace(id=user_id),
            label=label,
            data_root=tmp_path,
            append_memory=fake_append_memory,
        )

    result = session_feedback.read_activation_feedback_sidecar(tmp_path, agent_id, newest_first=False, limit=10)

    assert result["total_lines"] == 2
    assert result["retention"]["max_entries"] == 2
    assert [entry["label"] for entry in result["entries"]] == ["misleading", "useful"]


@pytest.mark.asyncio
async def test_record_session_feedback_links_to_verified_decision_trace(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.decision_trace import DecisionTraceStore
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = DecisionTraceStore()
    decision = decision_store.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        user_id=str(user_id),
        session_id=str(session_id),
        tool_name="send_feishu_message",
    )
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(status="overlay", category="feedback", entry_id="explicit-feedback-1")

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="Asking first was correct.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
        decision_id=decision.id,
        decision_trace_store=decision_store,
    )

    feedback_row = next(row for row in db.added if isinstance(row, SessionFeedbackEvent))
    assert feedback_row.decision_trace_id == decision.id
    assert f"decision/{decision.id}" in calls[0][1]["source_refs"]
    assert result["attribution"]["decision_ref"] == f"decision/{decision.id}"
    assert decision_store.feedback_for_decision(decision.id)[0].reaction == "useful"


@pytest.mark.asyncio
async def test_record_session_feedback_rejects_cross_session_decision_trace(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.services.decision_trace import DecisionTraceStore
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = DecisionTraceStore()
    decision = decision_store.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["external_visible"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        user_id=str(user_id),
        session_id=str(uuid.uuid4()),
        tool_name="send_feishu_message",
    )

    async def fake_append_memory(*args, **kwargs):
        return T3AppendResult(status="accepted", category="feedback", entry_id="t3-feedback-1")

    with pytest.raises(ValueError, match="decision_id does not belong to this session"):
        await record_session_feedback(
            db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=uuid.uuid4(), agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            current_user=SimpleNamespace(id=user_id),
            label="useful",
            data_root=tmp_path,
            append_memory=fake_append_memory,
            decision_id=decision.id,
            decision_trace_store=decision_store,
        )


@pytest.mark.asyncio
async def test_record_misleading_session_feedback_marks_harmful_calibration(tmp_path) -> None:
    from types import SimpleNamespace as T3AppendResult
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    calls = []

    async def fake_append_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return T3AppendResult(
            status="duplicate",
            category="feedback",
            entry_id="t3-feedback-1",
            similar={"counter_delta": {"harmful_count": "1"}},
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=uuid.uuid4(), agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=uuid.uuid4()),
        label="misleading",
        reason="The answer hid a failed migration.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    assert calls[0][1]["evidence"] == "misleading"
    assert "calibration warning" in calls[0][1]["content"]
    assert result["label"] == "misleading"
    assert result["calibration_result"]["t3_status"] == "duplicate"
    assert result["calibration_result"]["counter_delta"]["harmful_count"] == "1"


@pytest.mark.asyncio
async def test_feedback_credit_reaches_recently_activated_sidecar_entries(tmp_path) -> None:
    """M3 FeedbackCredit: owner feedback must move the lifecycle-sidecar credit
    of memories activated during this session — and only those — closing the
    heat_delta write-only loop. Sidecar-only: no MD prose touched."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace as T3AppendResult

    from app.memory.lifecycle_store import (
        MemoryLifecycleStore,
        bump_access_telemetry,
        lifecycle_path,
    )
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_started = datetime.now(UTC) - timedelta(minutes=30)

    path = lifecycle_path(tmp_path, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = MemoryLifecycleStore(path)
    store.create_active("recalled during session", entry_id="usr-in-session")
    store.create_active("recalled long ago", entry_id="usr-stale")
    bump_access_telemetry(tmp_path, agent_id, entry_id="usr-in-session", now=session_started + timedelta(minutes=5))
    bump_access_telemetry(tmp_path, agent_id, entry_id="usr-stale", now=session_started - timedelta(days=3))

    async def fake_append_memory(*_args, **_kwargs):
        return T3AppendResult(
            status="overlay",
            category="feedback",
            entry_id="explicit-feedback-credit",
            path="memory/explicit/entries/explicit-feedback-credit.md",
        )

    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(
            id=session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            source_channel="web",
            created_at=session_started,
        ),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="The deployment memory was exactly right.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )

    sidecar = result["calibration_result"]["heat_decay_sidecar"]
    assert sidecar["credited_entry_ids"] == ["usr-in-session"]

    reloaded = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    assert reloaded.get("usr-in-session").credit > 0
    assert reloaded.get("usr-stale").credit == 0.0

    negative = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(
            id=session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            source_channel="web",
            created_at=session_started,
        ),
        current_user=SimpleNamespace(id=user_id),
        label="misleading",
        reason="Actually that was stale.",
        data_root=tmp_path,
        append_memory=fake_append_memory,
    )
    assert negative["calibration_result"]["heat_decay_sidecar"]["credited_entry_ids"] == ["usr-in-session"]
    assert MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("usr-in-session").credit == pytest.approx(0.0)
