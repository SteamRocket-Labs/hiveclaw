"""End-to-end closure tests for the in-session self-evolution loop (P0-P4).

These exercise the real services with real file IO (no mocks of the core),
proving the loop is actually wired end to end — capture -> ledger candidate
(with manifest) -> next-turn session projection -> skill flywheel with a
verification gate -> RESPONSE_COMPLETE scheduling — rather than merely asserting
that the right strings exist in the right files.
"""

from __future__ import annotations

import asyncio
import uuid

from app.services.evolution_ledger import load_evolution_ledger
from app.services.fast_reflection_service import create_fast_reflection_candidate
from app.services.session_learning import render_active_session_learning_projection


def test_memory_loop_capture_makes_lesson_visible_next_turn(tmp_path) -> None:
    agent_id = uuid.uuid4()
    session_id = "loop-mem"

    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        messages=[
            {"role": "assistant", "content": "I'll send the report in a marketing tone."},
            {"role": "user", "content": "不对，以后这个报告不要用营销口吻，要按审计格式列证据。"},
        ],
        metadata={"final_response": "明白。"},
    )
    assert result["status"] == "candidate_created"

    workspace = tmp_path / str(agent_id)
    # capture: a manifest-bearing candidate is in the ledger
    candidate = next(e for e in load_evolution_ledger(workspace) if e.get("event") == "candidate")
    assert candidate["manifest"]["schema"] == "hive_evolution_manifest.v1"

    # visible: the lesson renders for the *next* turn of the SAME session
    # (this is exactly what invoker.py injects into the dynamic suffix)
    projection = render_active_session_learning_projection(
        data_root=tmp_path, agent_id=agent_id, session_id=session_id
    )
    assert "审计格式" in projection
    assert "Session Learning" in projection

    # isolation: a different session must not see it
    other = render_active_session_learning_projection(
        data_root=tmp_path, agent_id=agent_id, session_id="unrelated-session"
    )
    assert other == ""


def test_skill_loop_repeated_workflow_creates_verified_skill_candidate(tmp_path) -> None:
    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="loop-skill",
        messages=[{"role": "user", "content": "same release workflow as always"}],
        metadata={"repeated_workflow_signature": "release -> tag -> build -> deploy -> smoke"},
    )

    skill = result["skill_candidate"]
    # skill side of the flywheel actually turned, and the verification gate ran
    assert skill["status"] == "skill_candidate_created"
    assert skill["verification_passed"] is True

    workspace = tmp_path / str(agent_id)
    entries = load_evolution_ledger(workspace)
    assert any(
        e.get("event") == "candidate" and e.get("target_type") == "skill_candidate" for e in entries
    )
    # the durable skill draft is written under candidates, never auto-activated
    assert (workspace / "evolution" / "skill_candidates" / skill["candidate_id"] / "SKILL.md").exists()
    assert not (workspace / "skills").exists()


def test_response_complete_handler_is_registered_for_fast_reflection() -> None:
    from app.runtime import hooks_setup

    entry = next(
        c for c in hooks_setup._MEMORY_HOOK_CONFIGURATION if c.get("handler") == "fast_reflection_on_response"
    )
    assert entry["event"] == hooks_setup.HookEvent.RESPONSE_COMPLETE.value
    assert entry["key"] == "memory.response_complete.fast_reflection"
    assert (
        hooks_setup._MEMORY_HOOK_HANDLERS["fast_reflection_on_response"]
        is hooks_setup._fast_reflection_on_response
    )


async def test_scheduled_fast_reflection_lands_candidate(tmp_path) -> None:
    from app.runtime.hooks_setup import schedule_fast_reflection_candidate

    agent_id = uuid.uuid4()
    res = schedule_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="loop-hook",
        messages=[{"role": "user", "content": "下次别再用 yarn 了，这个项目以后都用 pnpm。"}],
        metadata={},
    )
    assert res["status"] == "scheduled"

    # drain the background task the scheduler created — proves the real async
    # RESPONSE_COMPLETE path (create_task -> to_thread -> service) lands a candidate
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=5)

    entries = load_evolution_ledger(tmp_path / str(agent_id))
    assert any(e.get("event") == "candidate" and e.get("target_type") == "fast_reflection" for e in entries)
