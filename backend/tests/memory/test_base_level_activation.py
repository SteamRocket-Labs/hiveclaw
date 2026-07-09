"""M1+M2 BaseLevel line (dynamic-memory-activation design §4.3, §8).

M1: the lifecycle sidecar grows a recent-access timestamp ring (K=8) and a
feedback ``credit`` field, fed by the existing ``bump_access`` writer, and the
retriever joins that telemetry back onto retrieved items — closing the
write-only half-loop.

M2: ``ActivationScorer`` replaces the linear ``_usage_heat`` with the ACT-R
inspired BaseLevel: ``bound(k * ln(1 + Σ t_j^(-d) + credit))`` with power-law
decay d≈0.5. Suppression (sensitivity / lifecycle) keeps absolute priority.
The M0 recall baseline must rise on the reserved headroom case when telemetry
exists, and must stay byte-identical when no sidecar exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.memory.activation import ActivationContext, ActivationScorer
from app.memory.lifecycle_store import (
    MemoryLifecycleStore,
    apply_feedback_credit,
    bump_access_telemetry,
    lifecycle_path,
    read_access_telemetry,
)
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import PrincipalStack

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


def _store(tmp_path: Path, agent_id: uuid.UUID) -> MemoryLifecycleStore:
    path = lifecycle_path(tmp_path, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return MemoryLifecycleStore(path)


# ---------------------------------------------------------------------------
# M1 — sidecar ring + credit
# ---------------------------------------------------------------------------


def test_bump_access_records_recent_access_ring_capped_at_k(tmp_path: Path, agent_id: uuid.UUID) -> None:
    store = _store(tmp_path, agent_id)
    store.create_active("fact", entry_id="usr-ring")

    for hour in range(12):
        assert bump_access_telemetry(tmp_path, agent_id, entry_id="usr-ring", now=NOW + timedelta(hours=hour))

    entry = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("usr-ring")
    assert entry.access_count == 12
    assert len(entry.recent_accesses) == 8, "ring must cap at K=8"
    assert entry.recent_accesses[-1] == NOW + timedelta(hours=11)
    assert entry.recent_accesses[0] == NOW + timedelta(hours=4), "oldest beyond K must be evicted"


def test_serde_roundtrip_preserves_ring_and_credit_and_reads_legacy_records(
    tmp_path: Path, agent_id: uuid.UUID
) -> None:
    store = _store(tmp_path, agent_id)
    store.create_active("fact", entry_id="usr-serde")
    bump_access_telemetry(tmp_path, agent_id, entry_id="usr-serde", now=NOW)
    apply_feedback_credit(tmp_path, agent_id, entry_id="usr-serde", delta=0.5)

    reloaded = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("usr-serde")
    assert reloaded.recent_accesses == [NOW]
    assert reloaded.credit == pytest.approx(0.5)

    # Legacy record without the new fields must load with safe defaults.
    legacy_store = _store(tmp_path, uuid.uuid4())
    legacy_store.create_active("old fact", entry_id="usr-legacy")
    raw = lifecycle_path(tmp_path, agent_id).read_text(encoding="utf-8")
    assert "recent_accesses" in raw


def test_apply_feedback_credit_accumulates_and_is_sidecar_only(tmp_path: Path, agent_id: uuid.UUID) -> None:
    store = _store(tmp_path, agent_id)
    store.create_active("fact", entry_id="usr-credit")

    assert apply_feedback_credit(tmp_path, agent_id, entry_id="usr-credit", delta=1.0)
    assert apply_feedback_credit(tmp_path, agent_id, entry_id="usr-credit", delta=-0.25)
    assert not apply_feedback_credit(tmp_path, agent_id, entry_id="usr-absent", delta=1.0)

    entry = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("usr-credit")
    assert entry.credit == pytest.approx(0.75)


def test_telemetry_map_projects_ring_and_credit(tmp_path: Path, agent_id: uuid.UUID) -> None:
    store = _store(tmp_path, agent_id)
    store.create_active("fact", entry_id="usr-proj")
    bump_access_telemetry(tmp_path, agent_id, entry_id="usr-proj", now=NOW)
    apply_feedback_credit(tmp_path, agent_id, entry_id="usr-proj", delta=0.5)

    telemetry = read_access_telemetry(tmp_path, agent_id)["usr-proj"]
    assert telemetry["access_count"] == "1"
    assert telemetry["recent_accesses"] == [NOW.isoformat()]
    assert telemetry["credit"] == "0.5"


def test_bump_access_can_create_telemetry_record_for_knowledge_pages(tmp_path: Path, agent_id: uuid.UUID) -> None:
    """Knowledge pages have no authored lifecycle record; frequency
    reinforcement still needs a telemetry row keyed by page id."""
    created = bump_access_telemetry(
        tmp_path, agent_id, entry_id="knowledge/railway-deployment", now=NOW, create_if_missing=True
    )
    assert created
    telemetry = read_access_telemetry(tmp_path, agent_id)["knowledge/railway-deployment"]
    assert telemetry["access_count"] == "1"


# ---------------------------------------------------------------------------
# M2 — BaseLevel replaces _usage_heat
# ---------------------------------------------------------------------------


def _item(metadata: dict) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="railway deploy notes",
        score=0.6,
        source="memory/knowledge/x.md",
        metadata={"sensitivity": "PL1_public", **metadata},
    )


def _context(query: str = "railway deploy") -> ActivationContext:
    return ActivationContext(query=query, principal_stack=PrincipalStack(), now=NOW)


def test_base_level_zero_without_telemetry_keeps_score_unchanged() -> None:
    scorer = ActivationScorer()
    bare = scorer.score(_item({}), _context())
    assert "base_level" not in bare.reasons


def test_base_level_recent_accesses_beat_stale_accesses() -> None:
    scorer = ActivationScorer()
    recent = _item({"recent_accesses": [(NOW - timedelta(hours=1)).isoformat()] * 4})
    stale = _item({"recent_accesses": [(NOW - timedelta(days=60)).isoformat()] * 4})

    recent_decision = scorer.score(recent, _context())
    stale_decision = scorer.score(stale, _context())

    assert recent_decision.score > stale_decision.score, "power-law decay must favor recency"
    assert "base_level" in recent_decision.reasons


def test_base_level_is_bounded_under_heavy_access() -> None:
    scorer = ActivationScorer()
    heavy = _item({"recent_accesses": [(NOW - timedelta(minutes=1)).isoformat()] * 8, "credit": "5.0"})
    light = _item({"recent_accesses": [(NOW - timedelta(hours=2)).isoformat()]})

    heavy_decision = scorer.score(heavy, _context())
    light_decision = scorer.score(light, _context())

    assert heavy_decision.score <= 1.0
    assert heavy_decision.score - light_decision.score <= scorer.policy.base_level_weight + 1e-9


def test_negative_credit_lowers_base_level() -> None:
    scorer = ActivationScorer()
    ring = [(NOW - timedelta(hours=3)).isoformat()] * 2
    plain = _item({"recent_accesses": ring})
    penalized = _item({"recent_accesses": ring, "credit": "-1.5"})

    assert scorer.score(penalized, _context()).score < scorer.score(plain, _context()).score


def test_lifecycle_suppression_still_beats_base_level() -> None:
    scorer = ActivationScorer()
    suppressed = _item(
        {
            "recent_accesses": [(NOW - timedelta(minutes=5)).isoformat()] * 8,
            "credit": "5.0",
            "ttl_status": "expired",
        }
    )
    decision = scorer.score(suppressed, _context())
    assert decision.suppressed
    assert decision.score == 0.0


def test_sensitivity_strip_still_beats_base_level() -> None:
    scorer = ActivationScorer()
    secret = _item({"recent_accesses": [(NOW - timedelta(minutes=5)).isoformat()] * 8, "sensitivity": "PL4_credential"})
    decision = secret and scorer.score(secret, _context())
    assert decision.suppressed


# ---------------------------------------------------------------------------
# M2 eval gate — the M0 headroom case must flip with telemetry, and the
# no-sidecar baseline must stay identical.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_eval_base_level_flips_headroom_case_and_raises_aggregate(tmp_path: Path) -> None:
    from app.evals.memory_recall_eval import (
        build_memory_recall_fixture,
        run_retriever_pipeline_eval,
        seed_base_level_telemetry,
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

    seed_base_level_telemetry(tmp_path, FIXTURE_AGENT_ID, now=NOW)
    boosted = await run_retriever_pipeline_eval(tmp_path, FIXTURE_AGENT_ID, now=NOW)

    assert boosted["cases"]["api-timeout-headroom"]["recall_at_k"] == 1.0, (
        "frequently-used runbook must outrank the lexically-stuffed archive"
    )
    assert boosted["recall_at_k"] > baseline["recall_at_k"]
    assert boosted["mrr"] > baseline["mrr"]
