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


@pytest.mark.asyncio
async def test_decision_linked_feedback_defaults_to_request_scoped_sql_store(tmp_path, monkeypatch) -> None:

    import app.services.session_feedback as session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_id = f"decision-{uuid.uuid4().hex}"
    calls: list[tuple[str, object]] = []

    class FakeSqlDecisionTraceStore:
        def __init__(self, session) -> None:
            calls.append(("session", session))

        async def get_decision(self, requested_id: str):
            calls.append(("get", requested_id))
            return SimpleNamespace(
                id=decision_id,
                tenant_id=str(tenant_id),
                agent_id=str(agent_id),
                session_id=str(session_id),
            )

        async def record_feedback(self, **kwargs):
            calls.append(("feedback", kwargs))
            return SimpleNamespace(id=str(uuid.uuid4()))


    monkeypatch.setattr(session_feedback, "SqlDecisionTraceStore", FakeSqlDecisionTraceStore, raising=False)

    result = await session_feedback.record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="The approval boundary was correct.",
        decision_id=decision_id,
        data_root=tmp_path,
    )

    assert calls[0] == ("session", db)
    assert ("get", decision_id) in calls
    feedback_call = next(payload for kind, payload in calls if kind == "feedback")
    assert feedback_call["decision_id"] == decision_id
    assert feedback_call["reaction"] == "useful"
    assert result["decision_ref"] == f"decision/{decision_id}"


@pytest.mark.asyncio
async def test_decision_linked_feedback_requires_exact_tenant_agent_and_session_scope(tmp_path) -> None:
    from app.services.session_feedback import record_session_feedback
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    store = InMemoryDecisionTraceStore()
    decision = store.record_decision(
        action="send_feishu_message",
        chosen="ask",
        reasoning="External-visible action.",
        alternatives_considered=["send", "ask"],
        situational_factors=["charter_confirm_first"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        session_id=None,
    )

    with pytest.raises(ValueError, match="does not belong to this session"):
        await record_session_feedback(
            db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            label="useful",
            decision_id=decision.id,
            data_root=tmp_path,
            decision_trace_store=store,
        )

    assert store.feedback_for_decision(decision.id) == []


@pytest.mark.asyncio
async def test_record_useful_session_feedback_persists_evaluation_and_audit(tmp_path) -> None:
    from app.models.audit import AuditLog
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent, tenant, session, user = (uuid.uuid4() for _ in range(4))
    message = uuid.uuid4()
    reason = "The answer gave the exact deployment checklist."
    result = await record_session_feedback(
        db, agent=SimpleNamespace(id=agent, tenant_id=tenant),
        session=SimpleNamespace(id=session, source_channel="web"),
        current_user=SimpleNamespace(id=user), label="useful", reason=reason,
        message_id=message, data_root=tmp_path,
    )
    assert db.flushed
    row = next(row for row in db.added if isinstance(row, SessionFeedbackEvent))
    assert (row.user_id, row.reason, row.label) == (user, reason, "useful")
    assert row.attribution_json["source_refs"] == [f"session:{session}", f"message:{message}"]
    assert any(isinstance(row, AuditLog) and row.action == "session_feedback.recorded" for row in db.added)
    assert result["calibration_result"]["memory_status"] == "not_requested"
    assert result["calibration_result"]["t3_status"] == "not_requested"
    assert not result["calibration_result"]["entry_id"]
    assert not (tmp_path / str(agent) / "memory/explicit").exists()


@pytest.mark.asyncio
async def test_record_session_feedback_writes_activation_credit_sidecar(tmp_path) -> None:
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()


    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="misleading",
        reason="The recalled deployment fact was stale.",
        data_root=tmp_path,
    )

    calibration = result["calibration_result"]
    sidecar = calibration["heat_decay_sidecar"]
    activation_event = calibration["activation_event"]
    sidecar_path = tmp_path / str(agent_id) / sidecar["path"]

    assert activation_event["schema"] == "hive.ccplus.activation_event.v1"
    assert activation_event["event_type"] == "owner_feedback_misleading"
    assert activation_event["candidate_ref"]["candidate_id"] == f"session_feedback:{result['id']}"
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

    import app.services.session_feedback as session_feedback

    monkeypatch.setattr(session_feedback, "_ACTIVATION_FEEDBACK_SIDECAR_MAX_ENTRIES", 2)

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()


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
            )

    result = session_feedback.read_activation_feedback_sidecar(tmp_path, agent_id, newest_first=False, limit=10)

    assert result["total_lines"] == 2
    assert result["retention"]["max_entries"] == 2
    assert [entry["label"] for entry in result["entries"]] == ["misleading", "useful"]


@pytest.mark.asyncio
async def test_record_session_feedback_links_to_verified_decision_trace(tmp_path) -> None:
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.session_feedback import record_session_feedback
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = InMemoryDecisionTraceStore()
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


    result = await record_session_feedback(
        db,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session=SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
        current_user=SimpleNamespace(id=user_id),
        label="useful",
        reason="Asking first was correct.",
        data_root=tmp_path,
        decision_id=decision.id,
        decision_trace_store=decision_store,
    )

    feedback_row = next(row for row in db.added if isinstance(row, SessionFeedbackEvent))
    assert feedback_row.decision_trace_id == decision.id
    assert f"decision/{decision.id}" in feedback_row.attribution_json["source_refs"]
    assert result["attribution"]["decision_ref"] == f"decision/{decision.id}"
    assert decision_store.feedback_for_decision(decision.id)[0].reaction == "useful"


@pytest.mark.asyncio
async def test_record_session_feedback_rejects_cross_session_decision_trace(tmp_path) -> None:
    from app.services.session_feedback import record_session_feedback
    from tests.decision_trace_fake import InMemoryDecisionTraceStore

    db = _FakeDB()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    decision_store = InMemoryDecisionTraceStore()
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


    with pytest.raises(ValueError, match="decision_id does not belong to this session"):
        await record_session_feedback(
            db,
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=uuid.uuid4(), agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            current_user=SimpleNamespace(id=user_id),
            label="useful",
            data_root=tmp_path,
                decision_id=decision.id,
            decision_trace_store=decision_store,
        )


@pytest.mark.asyncio
async def test_record_misleading_session_feedback_preserves_negative_evaluation(tmp_path) -> None:
    from app.models.session_feedback import SessionFeedbackEvent
    from app.services.session_feedback import record_session_feedback

    db = _FakeDB()
    agent, tenant, user = (uuid.uuid4() for _ in range(3))
    reason = "The answer hid a failed migration."
    result = await record_session_feedback(
        db, agent=SimpleNamespace(id=agent, tenant_id=tenant), session=SimpleNamespace(id=uuid.uuid4()),
        current_user=SimpleNamespace(id=user), label="misleading", reason=reason, data_root=tmp_path,
    )
    row = next(row for row in db.added if isinstance(row, SessionFeedbackEvent))
    assert (row.label, row.reason, row.user_id) == ("misleading", reason, user)
    assert result["calibration_result"]["activation_event"]["feedback"]["credit"] < 0
    assert result["calibration_result"]["memory_status"] == "not_requested"
    assert not (tmp_path / str(agent) / "memory/explicit").exists()


@pytest.mark.asyncio
async def test_feedback_credit_reaches_recently_activated_sidecar_entries(tmp_path) -> None:
    """M3 FeedbackCredit: owner feedback must move the lifecycle-sidecar credit
    of memories activated during this session — and only those — closing the
    heat_delta write-only loop. Sidecar-only: no MD prose touched."""
    from datetime import UTC, datetime, timedelta

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
    from app.memory.session_working_set import advance_working_set, save_working_set

    save_working_set(tmp_path, agent_id, str(session_id), advance_working_set(None, ["usr-in-session"]))


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
    )
    assert negative["calibration_result"]["heat_decay_sidecar"]["credited_entry_ids"] == ["usr-in-session"]
    assert MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("usr-in-session").credit == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_feedback_credit_is_limited_to_its_own_sessions_recorded_working_set(tmp_path):
    from datetime import UTC, datetime, timedelta
    from app.memory.lifecycle_store import MemoryLifecycleStore, bump_access_telemetry, lifecycle_path
    from app.memory.session_working_set import advance_working_set, save_working_set
    from app.services.session_feedback import record_session_feedback

    agent, tenant, session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent))
    store.create_active("Used by the rated session", entry_id="rated-session-memory")
    store.create_active("Used simultaneously by another member", entry_id="other-session-memory")
    for entry_id in ["rated-session-memory", "other-session-memory"]:
        bump_access_telemetry(tmp_path, agent, entry_id=entry_id, now=now)
    save_working_set(tmp_path, agent, str(session), advance_working_set(None, ["rated-session-memory"]))


    result = await record_session_feedback(
        _FakeDB(), agent=SimpleNamespace(id=agent, tenant_id=tenant),
        session=SimpleNamespace(id=session, created_at=now - timedelta(minutes=1)),
        current_user=SimpleNamespace(id=uuid.uuid4()), label="useful", data_root=tmp_path,
    )
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent))
    assert store.get("rated-session-memory").credit > 0
    assert store.get("other-session-memory").credit == 0, "Concurrent sessions shared feedback credit"
    assert result["calibration_result"]["heat_decay_sidecar"]["credited_entry_ids"] == ["rated-session-memory"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "corrupt", "foreign_session", "wrong_schema", "malformed"])
async def test_missing_or_unbound_working_set_does_not_fall_back_to_global_recent_memories(tmp_path, state):
    from datetime import UTC, datetime
    import json
    from app.memory.lifecycle_store import MemoryLifecycleStore, bump_access_telemetry, lifecycle_path
    from app.memory.session_working_set import advance_working_set, save_working_set
    from app.services.session_feedback import record_session_feedback

    agent, tenant, session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent))
    store.create_active("Another session's recently used memory", entry_id="other-session-memory")
    bump_access_telemetry(tmp_path, agent, entry_id="other-session-memory")
    if state != "missing":
        save_working_set(tmp_path, agent, str(session), advance_working_set(None, ["other-session-memory"]))
        path = tmp_path / str(agent) / "memory/control/working_sets" / f"{session}.json"
        payload = json.loads(path.read_text())
        if state == "corrupt":
            path.write_text("{invalid")
        elif state == "foreign_session":
            payload["session_id"] = str(uuid.uuid4())
            path.write_text(json.dumps(payload))
        elif state == "malformed":
            payload["items"] = ["not a working-set item"]
            path.write_text(json.dumps(payload))
        else:
            payload["schema"] = "unrecognized.schema"
            path.write_text(json.dumps(payload))
        retained = path.read_bytes()
    result = await record_session_feedback(
        _FakeDB(), agent=SimpleNamespace(id=agent, tenant_id=tenant),
        session=SimpleNamespace(id=session, created_at=datetime.now(UTC)),
        current_user=SimpleNamespace(id=uuid.uuid4()), label="useful", data_root=tmp_path,
    )
    sidecar = result["calibration_result"]["heat_decay_sidecar"]
    assert sidecar["credited_entry_ids"] == []
    assert MemoryLifecycleStore(lifecycle_path(tmp_path, agent)).get("other-session-memory").credit == 0
    if state != "missing":
        assert sidecar["credit_status"] == "working_set_unavailable"
        assert path.read_bytes() == retained
