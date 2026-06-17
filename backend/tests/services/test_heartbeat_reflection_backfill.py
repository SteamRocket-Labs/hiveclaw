from __future__ import annotations

import uuid

import pytest


def _record(content: str, *, outcome: str = "action_taken", score: int = 7) -> dict:
    return {
        "session_id": "hb-session-1",
        "assistant_message_id": "msg-1",
        "runtime_task_id": "rt-1",
        "content": content,
        "outcome_type": outcome,
        "outcome_lane": "agent_action" if outcome == "action_taken" else "idle",
        "score": score,
        "runtime_messages": [{"role": "assistant", "content": content}],
    }


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_emit_learning(monkeypatch):
    from app.services.heartbeat_reflection_backfill import run_heartbeat_reflection_backfill

    emitted: list[dict] = []

    async def fake_route(**kwargs):
        emitted.append(kwargs)
        return {"status": "emitted", "source_refs": ["chat_message:msg-1"]}

    monkeypatch.setattr("app.services.heartbeat_reflection_backfill._route_heartbeat_reflection_learning", fake_route)

    result = await run_heartbeat_reflection_backfill(
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_name="Agent",
        records=[_record("Reusable learning [OUTCOME:action_taken] [SCORE:7]")],
        dry_run=True,
    )

    assert result["would_process"] == 1
    assert result["processed"] == 0
    assert emitted == []


@pytest.mark.asyncio
async def test_backfill_skips_noop_and_heartbeat_ok(monkeypatch):
    from app.services.heartbeat_reflection_backfill import run_heartbeat_reflection_backfill

    emitted: list[dict] = []

    async def fake_route(**kwargs):
        emitted.append(kwargs)
        return {"status": "emitted"}

    monkeypatch.setattr("app.services.heartbeat_reflection_backfill._route_heartbeat_reflection_learning", fake_route)

    result = await run_heartbeat_reflection_backfill(
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_name="Agent",
        records=[_record("HEARTBEAT_OK [OUTCOME:noop] [SCORE:0]", outcome="noop", score=0)],
        dry_run=False,
        confirm=True,
    )

    assert result["skipped_low_signal"] == 1
    assert result["processed"] == 0
    assert emitted == []


@pytest.mark.asyncio
async def test_backfill_apply_requires_confirm():
    from app.services.heartbeat_reflection_backfill import run_heartbeat_reflection_backfill

    with pytest.raises(ValueError, match="confirm=True"):
        await run_heartbeat_reflection_backfill(
            agent_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            agent_name="Agent",
            records=[_record("Reusable learning [OUTCOME:action_taken] [SCORE:7]")],
            dry_run=False,
            confirm=False,
        )


@pytest.mark.asyncio
async def test_backfill_apply_uses_llm_learning_hook_not_regex_primary(monkeypatch):
    from app.services.heartbeat_reflection_backfill import run_heartbeat_reflection_backfill

    emitted: list[dict] = []

    async def fake_route(**kwargs):
        emitted.append(kwargs)
        return {"status": "emitted"}

    monkeypatch.setattr("app.services.heartbeat_reflection_backfill._route_heartbeat_reflection_learning", fake_route)

    result = await run_heartbeat_reflection_backfill(
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_name="Agent",
        records=[_record("Reusable learning [OUTCOME:action_taken] [SCORE:7]")],
        dry_run=False,
        confirm=True,
    )

    assert result["method"] == "llm_primary_hook"
    assert result["processed"] == 1
    assert len(emitted) == 1
