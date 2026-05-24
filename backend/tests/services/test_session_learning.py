from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def test_session_learning_projection_records_and_renders_active_candidate(tmp_path) -> None:
    from app.services.session_learning import (
        load_session_learning_projections,
        record_session_learning_projection,
        render_session_learning_projection,
    )

    agent_id = uuid.uuid4()
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    projection = record_session_learning_projection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        candidate_id="cand-1",
        lesson="Use npm for this repository.",
        source_refs=["runtime_task:session-1"],
        evidence="user_stated",
        now=now,
        ttl_minutes=30,
    )

    assert projection["schema"] == "session_learning_projection.v1"
    assert projection["promotion_state"] == "candidate"

    active = load_session_learning_projections(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        now=now + timedelta(minutes=5),
    )
    rendered = render_session_learning_projection(active)

    assert len(active) == 1
    assert "Use npm for this repository." in rendered
    assert "cand-1" in rendered
    assert "user_stated" in rendered


def test_session_learning_projection_expires_and_rejects(tmp_path) -> None:
    from app.services.session_learning import (
        load_session_learning_projections,
        record_session_learning_projection,
        update_session_learning_projection_state,
    )

    agent_id = uuid.uuid4()
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    record_session_learning_projection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        candidate_id="cand-expired",
        lesson="Expired lesson.",
        source_refs=["runtime_task:session-1"],
        evidence="system_observed",
        now=now,
        ttl_minutes=1,
    )
    record_session_learning_projection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        candidate_id="cand-rejected",
        lesson="Rejected lesson.",
        source_refs=["runtime_task:session-1"],
        evidence="user_stated",
        now=now,
        ttl_minutes=60,
    )
    update_session_learning_projection_state(
        data_root=tmp_path,
        agent_id=agent_id,
        candidate_id="cand-rejected",
        promotion_state="rejected",
        now=now + timedelta(minutes=2),
    )

    active = load_session_learning_projections(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        now=now + timedelta(minutes=2),
    )

    assert active == []
