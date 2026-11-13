"""Required consumers finish together, including the failure path."""

import asyncio
import uuid

import pytest

from app.runtime.hooks import HookContext, HookEvent
from app.runtime import hooks_setup


def _context():
    outcome_id = uuid.uuid4()
    return HookContext(event=HookEvent.RESPONSE_COMPLETE, agent_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()), source="web", messages=[{"role": "assistant", "content": "Done."}],
        metadata={"response_commit": {"schema": "hive.response_commit.v1", "committed": True,
            "commit_kind": "session_v2_terminal_outcome", "idempotency_key": str(outcome_id),
            "source_refs": [f"session-run-outcome://{outcome_id}"]}})


@pytest.mark.parametrize("failure", [None, "session", "reflection"])
async def test_required_projections_join_before_success_or_failure(monkeypatch, failure):
    ctx = _context()
    source_ref = ctx.metadata["response_commit"]["source_refs"][0]
    started = {name: asyncio.Event() for name in ("session", "reflection")}
    release = asyncio.Event()
    finished = []

    async def session(_ctx):
        started["session"].set()
        await release.wait()
        finished.append("session")
        if failure == "session":
            raise RuntimeError("session transaction failed")
        return {"source_ref": source_ref, "transaction_id": "stable-session-receipt", "revision": 7}

    async def reflection(_ctx):
        started["reflection"].set()
        if failure == "reflection":
            raise RuntimeError("reflection transaction failed")
        await release.wait()
        finished.append("reflection")
        return {"source_ref": source_ref, "status": "skipped", "reason": "low_signal"}

    monkeypatch.setattr(hooks_setup, "_project_on_response", session)
    monkeypatch.setattr(hooks_setup, "_fast_reflection_on_response", reflection)
    task = asyncio.create_task(hooks_setup.project_committed_response_complete(ctx))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 1)
        await asyncio.sleep(0)
        assert not task.done(), "A failed consumer must not detach its still-running sibling"
        release.set()
        if failure:
            with pytest.raises(RuntimeError, match=f"{failure} transaction failed"):
                await task
            assert "session" in finished
            assert not ctx.metadata.get("required_response_complete_projected")
        else:
            receipt = await task
            assert finished == ["session", "reflection"] or finished == ["reflection", "session"]
            assert set(receipt) == {"source_refs", "receipt_sha256"}
            assert receipt["source_refs"] == [source_ref]
            assert len(receipt["receipt_sha256"]) == 64
            assert ctx.metadata["required_response_complete_projected"] is True
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_both_projection_failures_log_secondary_type_without_content(monkeypatch, caplog):
    ctx = _context()
    primary = ValueError("PRIVATE_SESSION_CONTENT")
    secondary = RuntimeError("PRIVATE_REFLECTION_CONTENT")

    async def session(_ctx):
        raise primary

    async def reflection(_ctx):
        raise secondary

    monkeypatch.setattr(hooks_setup, "_project_on_response", session)
    monkeypatch.setattr(hooks_setup, "_fast_reflection_on_response", reflection)
    with caplog.at_level("WARNING", logger=hooks_setup.__name__):
        with pytest.raises(ValueError) as caught:
            await hooks_setup.project_committed_response_complete(ctx)
    assert caught.value is primary
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
    assert "PRIVATE_SESSION_CONTENT" not in caplog.text and "PRIVATE_REFLECTION_CONTENT" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert not ctx.metadata.get("required_response_complete_projected")
