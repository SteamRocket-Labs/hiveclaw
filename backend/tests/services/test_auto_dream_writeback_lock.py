"""P1-W2-10 — auto_dream soul/focus writeback lock.

Two dream invocations can race when the heartbeat scheduler and a
trigger end both decide to fire dream within the same window. Both
paths run `_apply_dream_decisions`, which does multi-step
read-modify-write on shared MD files. flock makes that sequence
atomic per agent.

Tests cover:
  - the lock context manager actually acquires + releases an exclusive lock
  - missing workspace dirs no-op cleanly (early bootstrap)
  - `_apply_dream_decisions` calls the inner unlocked variant under the lock
  - concurrent calls serialize (one waits for the other)
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import uuid

import pytest

from app.services import auto_dream


@pytest.fixture
def agent_workspace(tmp_path, monkeypatch):
    """Build a fake workspace under tmp_path and point AGENT_DATA_DIR at it."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)
    agent_id = uuid.uuid4()
    agent_root = tmp_path / str(agent_id)
    agent_root.mkdir(parents=True)
    (agent_root / "memory").mkdir()
    return agent_id, agent_root


# ── Lock semantics ────────────────────────────────────────────


def test_lock_creates_dotfile(agent_workspace) -> None:
    agent_id, agent_root = agent_workspace
    lock_path = agent_root / ".dream_writeback.lock"
    assert not lock_path.exists()

    with auto_dream._dream_writeback_lock(agent_id):
        assert lock_path.exists()


def test_lock_releases_after_block(agent_workspace) -> None:
    """A second acquire on the same lock file must succeed once the first
    block exits — proves the unlock path actually fires."""
    agent_id, agent_root = agent_workspace
    lock_path = agent_root / ".dream_writeback.lock"

    with auto_dream._dream_writeback_lock(agent_id):
        pass

    # Try a non-blocking acquire on the same path — should succeed instantly.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_is_noop_when_workspace_missing(tmp_path, monkeypatch) -> None:
    """Early bootstrap: no agent dir yet → silently skip locking."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)
    nonexistent_agent = uuid.uuid4()

    # Should not raise, should not create any lock file.
    with auto_dream._dream_writeback_lock(nonexistent_agent):
        pass

    assert not (tmp_path / str(nonexistent_agent)).exists()


# ── Apply uses the lock ───────────────────────────────────────


def test_apply_dream_decisions_runs_under_lock(agent_workspace, monkeypatch) -> None:
    agent_id, _ = agent_workspace
    sentinel: list[str] = []

    import contextlib

    @contextlib.contextmanager
    def _spy_lock(_agent_id):
        sentinel.append("enter")
        yield
        sentinel.append("exit")

    monkeypatch.setattr(auto_dream, "_dream_writeback_lock", _spy_lock)
    monkeypatch.setattr(
        auto_dream,
        "_apply_dream_decisions_unlocked",
        lambda _agent_id, _decision: {"soul_added": 0, "spied": True},
    )

    result = auto_dream._apply_dream_decisions(agent_id, {"soul_promotions": []})

    assert sentinel == ["enter", "exit"]
    assert result["spied"] is True


# ── Concurrency: second writer blocks until first releases ─────


@pytest.mark.asyncio
async def test_concurrent_apply_calls_serialize(agent_workspace, monkeypatch) -> None:
    """Drive _apply_dream_decisions twice in parallel and assert the
    second one observes the first has already finished by the time it
    runs (i.e. they did not interleave)."""
    agent_id, _ = agent_workspace
    order: list[str] = []

    def slow_apply(_agent_id, _decision):
        order.append("start")
        # Hold the lock for a beat so the second call has to wait.
        import time
        time.sleep(0.05)
        order.append("end")
        return {}

    monkeypatch.setattr(auto_dream, "_apply_dream_decisions_unlocked", slow_apply)

    async def driver():
        await asyncio.to_thread(auto_dream._apply_dream_decisions, agent_id, {})

    await asyncio.gather(driver(), driver())

    # Each call records "start" then "end". If they ran in parallel we'd
    # see start, start, end, end. Locked execution gives us strict pairing.
    pairs = list(zip(order[::2], order[1::2]))
    assert all(p == ("start", "end") for p in pairs), order
