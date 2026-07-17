from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.memory.session_surfacing import (
    SESSION_AUTO_SURFACE_BUDGET_BYTES,
    surface_with_session_budget,
)


def _render_up_to(requested: int):
    def render(remaining: int) -> str:
        return "x" * min(requested, remaining)

    return render


def test_session_auto_surface_budget_is_cumulative_and_turn_idempotent(tmp_path) -> None:
    agent_id = uuid4()
    session_id = str(uuid4())

    first = surface_with_session_budget(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        turn_id="turn-1",
        render=_render_up_to(40_000),
    )
    replay = surface_with_session_budget(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        turn_id="turn-1",
        render=_render_up_to(40_000),
    )
    second = surface_with_session_budget(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        turn_id="turn-2",
        render=_render_up_to(40_000),
    )

    assert first.surfaced_bytes == 40_000
    assert replay.surfaced_bytes == 40_000
    assert replay.already_recorded is True
    assert second.surfaced_bytes == SESSION_AUTO_SURFACE_BUDGET_BYTES - 40_000
    assert second.remaining_bytes == 0


def test_concurrent_turns_cannot_overspend_session_budget(tmp_path) -> None:
    agent_id = uuid4()
    session_id = str(uuid4())

    def run(index: int):
        return surface_with_session_budget(
            data_root=tmp_path,
            agent_id=agent_id,
            session_id=session_id,
            turn_id=f"turn-{index}",
            render=_render_up_to(20_000),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))

    assert sum(result.surfaced_bytes for result in results) == SESSION_AUTO_SURFACE_BUDGET_BYTES
    ledger_path = tmp_path / str(agent_id) / "memory" / "control" / "session_surfacing" / f"{session_id}.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert payload["surfaced_bytes"] == SESSION_AUTO_SURFACE_BUDGET_BYTES
    assert all(result.remaining_bytes >= 0 for result in results)
