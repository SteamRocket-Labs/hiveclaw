"""M4+M5 ContextBoost line (dynamic-memory-activation design §4.2, §8).

M4: the session working set ``W_t`` — pointers to memories/entities touched
this session with evolving strength — persists per session, survives restart,
and carries NO content bodies (ACL hard boundary: refs + numbers only).

M5: recall seeds Personalized PageRank with the working set, so memories
graph-adjacent to what the session is already about get a bounded boost —
the same query recalls differently in different conversational contexts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.memory.activation import ActivationContext, ActivationScorer
from app.memory.session_working_set import (
    WORKING_SET_MAX_ITEMS,
    advance_working_set,
    load_working_set,
    save_working_set,
)
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import PrincipalStack

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# M4 — working set evolution + persistence + ACL boundary
# ---------------------------------------------------------------------------


def test_advance_working_set_adds_decays_and_evicts() -> None:
    first = advance_working_set(None, ["knowledge/railway-deployment", "usr-style"], now=NOW)
    assert first.turn_index == 1
    strengths = {item["ref"]: item["strength"] for item in first.items}
    assert strengths == {"knowledge/railway-deployment": 1.0, "usr-style": 1.0}

    second = advance_working_set(first, ["knowledge/memory-gate"], now=NOW)
    strengths = {item["ref"]: item["strength"] for item in second.items}
    assert strengths["knowledge/memory-gate"] == 1.0
    assert strengths["knowledge/railway-deployment"] == pytest.approx(0.8)

    evolved = second
    for _ in range(12):
        evolved = advance_working_set(evolved, ["knowledge/memory-gate"], now=NOW)
    refs = {item["ref"] for item in evolved.items}
    assert "knowledge/railway-deployment" not in refs, "strength below floor must be evicted"
    assert "knowledge/memory-gate" in refs


def test_working_set_is_capped() -> None:
    ws = None
    for index in range(WORKING_SET_MAX_ITEMS + 10):
        ws = advance_working_set(ws, [f"knowledge/page-{index}"], now=NOW)
    assert ws is not None
    assert len(ws.items) <= WORKING_SET_MAX_ITEMS


def test_working_set_persists_and_recovers_with_refs_only(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()
    ws = advance_working_set(None, ["knowledge/railway-deployment"], now=NOW)
    save_working_set(tmp_path, agent_id, "session-1", ws)

    recovered = load_working_set(tmp_path, agent_id, "session-1")
    assert recovered.turn_index == 1
    assert recovered.items[0]["ref"] == "knowledge/railway-deployment"

    # ACL hard boundary (design §4.2): pointers and statistics only — the
    # persisted file must never carry memory bodies or payload-like fields.
    raw = json.loads(
        (tmp_path / str(agent_id) / "memory" / "control" / "working_sets" / "session-1.json").read_text(
            encoding="utf-8"
        )
    )
    for item in raw["items"]:
        assert set(item) <= {"ref", "strength", "last_turn", "ts"}

    assert load_working_set(tmp_path, agent_id, "session-never").turn_index == 0


# ---------------------------------------------------------------------------
# M5 — scorer boost + graph diffusion
# ---------------------------------------------------------------------------


def _item(content: str, metadata: dict) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content=content,
        score=0.6,
        source="memory/knowledge/x.md",
        metadata={"sensitivity": "PL1_public", **metadata},
    )


def test_scorer_applies_bounded_context_boost() -> None:
    scorer = ActivationScorer()
    context = ActivationContext(query="unrelated", principal_stack=PrincipalStack(), now=NOW)
    boosted = scorer.score(_item("body", {"context_boost": "1.0"}), context)
    plain = scorer.score(_item("body", {}), context)

    assert boosted.raw_score - plain.raw_score == pytest.approx(scorer.policy.context_boost_weight)
    assert "context_boost" in boosted.reasons
    assert "context_boost" not in plain.reasons


@pytest.mark.asyncio
async def test_same_query_recalls_differently_under_different_working_sets(tmp_path: Path) -> None:
    """The design's acceptance behavior: identical query, different session
    context, different recall order — the lexically-tied checklist pair is
    disambiguated by which cluster the session already touched."""
    from app.evals.memory_recall_eval import build_memory_recall_fixture
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
    build_memory_recall_fixture(tmp_path, agent_id)
    retriever = MemoryRetriever(data_root=tmp_path)
    query = "review checklist steps"

    def _context(working_set: tuple[tuple[str, float], ...]) -> ActivationContext:
        return ActivationContext(query=query, principal_stack=PrincipalStack(), now=NOW, working_set=working_set)

    neutral = await retriever.retrieve(
        agent_id, query, session_id=None, tenant_id=None, activation_context=_context(())
    )
    governance_context = await retriever.retrieve(
        agent_id,
        query,
        session_id=None,
        tenant_id=None,
        activation_context=_context((("knowledge/platform-gate", 1.0),)),
    )

    gate_checklist = "memory/knowledge/gate-review-checklist.md"
    release_checklist = "memory/knowledge/release-checklist.md"
    neutral_rank = [item.source for item in neutral]
    governance_rank = [item.source for item in governance_context]
    assert neutral_rank.index(release_checklist) < neutral_rank.index(gate_checklist), (
        "without context the lexically-stronger release checklist wins"
    )
    assert governance_rank.index(gate_checklist) < governance_rank.index(release_checklist), (
        "a session already about platform-gate must pull its cluster's checklist up"
    )


@pytest.mark.asyncio
async def test_recall_eval_context_boost_flips_disambiguation_without_hijacking(tmp_path: Path) -> None:
    from app.evals.memory_recall_eval import (
        build_memory_recall_fixture,
        run_retriever_pipeline_eval,
    )
    from tests.evals.test_memory_recall_baseline import (
        BASELINE_MRR,
        BASELINE_RECALL_AT_K,
        FIXTURE_AGENT_ID,
    )

    build_memory_recall_fixture(tmp_path, FIXTURE_AGENT_ID)

    baseline = await run_retriever_pipeline_eval(tmp_path, FIXTURE_AGENT_ID, now=NOW)
    assert baseline["recall_at_k"] == pytest.approx(BASELINE_RECALL_AT_K)
    assert baseline["mrr"] == pytest.approx(BASELINE_MRR)

    context = (("knowledge/platform-gate", 1.0),)
    boosted = await run_retriever_pipeline_eval(
        tmp_path,
        FIXTURE_AGENT_ID,
        now=NOW,
        working_sets={
            "context-disambiguation-headroom": context,
            "governance-2hop-headroom": context,
        },
    )

    assert boosted["cases"]["context-disambiguation-headroom"]["recall_at_k"] == 1.0, (
        "session context must flip the lexically-tied checklist pair"
    )
    # Non-hijack guard: a bounded boost must NOT push the zero-overlap
    # retention page past a genuine lexical hit.
    assert boosted["cases"]["governance-2hop-headroom"]["recall_at_k"] == pytest.approx(2 / 3)
    assert boosted["recall_at_k"] > baseline["recall_at_k"]
    assert boosted["mrr"] > baseline["mrr"]
