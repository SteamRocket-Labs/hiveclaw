"""Tier 3-4 SSE FastAPI endpoint tests.

These exercise `app.api.deep_research.stream_deep_research` directly with
fake auth + a tmp workspace, and iterate the returned StreamingResponse
body to confirm we emit `data: {...}\\n\\n` SSE frames terminated by a
`final` event.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _seed_failed_run(artifact_dir: Path, source_id: str = "src_aaaaaaaaaaaa") -> None:
    """Mirror the helper in tests/deep_research/test_deep_research_handler.py."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "claims.jsonl").write_text(
        json.dumps(
            {
                "claim_id": "claim_x",
                "text": "Issuer A reports 35% growth.",
                "status": "verified",
                "source_ids": [source_id],
                "evidence": "evidence",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "source_notes.jsonl").write_text(
        json.dumps(
            {
                "source_id": source_id,
                "key_entities": ["Issuer A"],
                "key_numbers": ["35% growth"],
                "source_bound_summary": "Issuer A reports 35% growth.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "lane_summaries.jsonl").write_text(
        json.dumps(
            {
                "lane_id": "primary",
                "evidence_strength": "moderate",
                "key_findings": ["growth confirmed"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "report.md").write_text(
        "# Deep Research — Synthesis Failed\n\n"
        "This is not a completed Deep Research report.\n",
        encoding="utf-8",
    )
    (artifact_dir / "final.json").write_text(
        json.dumps(
            {
                "schema": "deep_research_final.v1",
                "status": "failed",
                "source_count": 1,
                "claim_count": 1,
                "quality_gates": {"synthesis": "failed"},
                "gaps": ["Synthesis failed."],
            }
        ),
        encoding="utf-8",
    )


async def _consume_sse_body(response) -> list[dict]:
    """Iterate a StreamingResponse and parse SSE `data:` frames into events."""
    raw_chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            raw_chunks.append(chunk.encode("utf-8"))
        else:
            raw_chunks.append(chunk)
    text = b"".join(raw_chunks).decode("utf-8")

    events: list[dict] = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame.startswith("data:"):
            continue
        payload = frame[len("data:") :].strip()
        if not payload:
            continue
        events.append(json.loads(payload))
    return events


@pytest.mark.asyncio
async def test_stream_deep_research_yields_sse_events_until_final(monkeypatch, tmp_path):
    """T3-4 SSE route emits properly-framed `data:` events and terminates with `final`."""
    import app.api.deep_research as deep_research_api

    agent_id = uuid4()
    task_id = "abcdef0123456789abcdef0123456789"
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())

    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / str(agent_id)
    artifact_dir = (
        workspace
        / "runtime_artifacts"
        / "long_tasks"
        / task_id
        / "deep_research"
    )
    _seed_failed_run(artifact_dir)

    monkeypatch.setattr(deep_research_api, "WORKSPACE_ROOT", workspace_root)

    captured: dict = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        captured["agent_id"] = requested_agent_id
        captured["user"] = user_arg
        return None

    monkeypatch.setattr(deep_research_api, "check_agent_access", fake_check_agent_access)

    response = await deep_research_api.stream_deep_research(
        agent_id=agent_id,
        task_id=task_id,
        poll_interval=0.0,
        deadline_seconds=10.0,
        after_step=0,
        after_claim=0,
        after_source_note=0,
        after_lane_summary=0,
        after_reflection=0,
        after_controller_trace=0,
        current_user=user,
        db=SimpleNamespace(),
    )

    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"
    assert captured["agent_id"] == agent_id

    events = await _consume_sse_body(response)
    event_types = [event["event"] for event in events]
    assert "claim" in event_types
    assert "source_note" in event_types
    assert "lane_summary" in event_types
    assert "report" in event_types
    assert event_types[-1] == "final"
    assert events[-1]["payload"]["status"] == "failed"
    assert all(event["task_id"] == task_id for event in events)


@pytest.mark.asyncio
async def test_stream_deep_research_rejects_missing_task_id(monkeypatch, tmp_path):
    """T3-4: blank task_id is rejected with 400."""
    import app.api.deep_research as deep_research_api
    from fastapi import HTTPException

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    workspace_root = tmp_path / "workspaces"
    (workspace_root / str(agent_id)).mkdir(parents=True)

    monkeypatch.setattr(deep_research_api, "WORKSPACE_ROOT", workspace_root)

    async def fake_check_agent_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(deep_research_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await deep_research_api.stream_deep_research(
            agent_id=agent_id,
            task_id="   ",
            poll_interval=0.5,
            deadline_seconds=600.0,
            after_step=0,
            after_claim=0,
            after_source_note=0,
            after_lane_summary=0,
            after_reflection=0,
            after_controller_trace=0,
            current_user=user,
            db=SimpleNamespace(),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_stream_deep_research_returns_404_for_missing_agent_workspace(monkeypatch, tmp_path):
    """T3-4: nonexistent agent workspace surfaces as 404 rather than blowing up the stream."""
    import app.api.deep_research as deep_research_api
    from fastapi import HTTPException

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    # NOTE: deliberately do not create workspace_root/<agent_id>

    monkeypatch.setattr(deep_research_api, "WORKSPACE_ROOT", workspace_root)

    async def fake_check_agent_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(deep_research_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await deep_research_api.stream_deep_research(
            agent_id=agent_id,
            task_id="task_xxx",
            poll_interval=0.5,
            deadline_seconds=600.0,
            after_step=0,
            after_claim=0,
            after_source_note=0,
            after_lane_summary=0,
            after_reflection=0,
            after_controller_trace=0,
            current_user=user,
            db=SimpleNamespace(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_stream_deep_research_passes_cursor_through_to_generator(monkeypatch, tmp_path):
    """T3-4: after_* query params are stitched into the generator's cursors."""
    import app.api.deep_research as deep_research_api

    agent_id = uuid4()
    task_id = "0123456789abcdef0123456789abcdef"
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())

    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / str(agent_id)
    artifact_dir = (
        workspace
        / "runtime_artifacts"
        / "long_tasks"
        / task_id
        / "deep_research"
    )
    _seed_failed_run(artifact_dir)

    monkeypatch.setattr(deep_research_api, "WORKSPACE_ROOT", workspace_root)

    async def fake_check_agent_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(deep_research_api, "check_agent_access", fake_check_agent_access)

    response = await deep_research_api.stream_deep_research(
        agent_id=agent_id,
        task_id=task_id,
        poll_interval=0.0,
        deadline_seconds=5.0,
        after_step=0,
        after_claim=1,           # Skip the only claim
        after_source_note=1,     # Skip the only source_note
        after_lane_summary=1,    # Skip the only lane_summary
        after_reflection=0,
        after_controller_trace=0,
        current_user=user,
        db=SimpleNamespace(),
    )

    events = await _consume_sse_body(response)
    event_types = [event["event"] for event in events]
    # Cursors made the generator skip the pre-emitted rows
    assert "claim" not in event_types
    assert "source_note" not in event_types
    assert "lane_summary" not in event_types
    # report + final still surface
    assert "report" in event_types
    assert event_types[-1] == "final"
