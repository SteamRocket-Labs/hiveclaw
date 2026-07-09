"""M0 memory-recall baseline eval (dynamic-memory-activation design §8).

This is the anchor gate for the dynamic recall layer work:

- Q-shrink self-check: retiring the QKV mechanical layer must leave every
  score and every ranked order below byte-identical (the audit concluded QKV
  is non-load-bearing; if a number moves, roll back and re-investigate).
- Component gate for M2 (BaseLevel) / M5 (ContextBoost): each component must
  raise the aggregate scores relative to this pure-RRF/PPR baseline before it
  is allowed to ship. Two cases carry deliberate headroom for that proof:
  ``api-timeout-headroom`` (recall 0.0 — lexical stuffing beats the gold
  runbook until usage frequency reinforces it) and
  ``governance-2hop-headroom`` (recall 2/3 — the gold retention page shares
  no query term and sits two hops from the seed).

The fixture is a deterministic on-disk knowledge/milestones page graph and the
runners call the REAL retrieval paths (``search_wiki_pages`` and
``MemoryRetriever.retrieve`` + ``ActivationScorer``) — no mocks, no LLM.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.evals.memory_recall_eval import (
    build_memory_recall_fixture,
    default_memory_recall_cases,
    run_memory_recall_eval,
    run_retriever_pipeline_eval,
)

FIXTURE_AGENT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000e0")

# ---------------------------------------------------------------------------
# Anchored baseline snapshots (pure RRF/PPR, pre-dynamic-recall-layer).
#
# Produced by running the real retrieval paths over the deterministic fixture.
# They are BEHAVIOR ANCHORS, not correctness targets: Q-shrink must not move
# them; M2/M5 must move recall/MRR UP via new scorer components (and will
# re-anchor deliberately, with the delta recorded as evidence).
# ---------------------------------------------------------------------------

BASELINE_RECALL_AT_K = 0.833333
BASELINE_MRR = 0.857143

WIKI_PPR_RANKED = {
    "railway-deploy": [
        "knowledge/railway-deployment",
        "milestones/2026-06-deploy-overhaul",
        "knowledge/vercel-sandbox",
        "knowledge/release-checklist",
        "knowledge/code-execution-policy",
    ],
    "sandbox-exec": [
        "knowledge/vercel-sandbox",
        "knowledge/railway-deployment",
        "knowledge/code-execution-policy",
        "knowledge/release-checklist",
        "milestones/2026-06-deploy-overhaul",
    ],
    "memory-governance": [
        "knowledge/memory-gate",
        "knowledge/platform-gate",
        "milestones/2026-05-memory-two-planes",
        "knowledge/audit-retention-policy",
    ],
    "feishu-channel": [
        "knowledge/feishu-integration",
        "milestones/2026-04-feishu-launch",
    ],
    "postgres-pool": [
        "knowledge/postgres-tuning",
        "knowledge/feishu-integration",
        "milestones/2026-04-feishu-launch",
    ],
    "api-timeout-headroom": [
        "knowledge/api-timeout-postmortem-archive",
    ],
    "governance-2hop-headroom": [
        "knowledge/memory-gate",
        "knowledge/platform-gate",
        "milestones/2026-05-memory-two-planes",
    ],
    "negative-topic": [],
}

RETRIEVER_PIPELINE_RANKED = {
    "railway-deploy": [
        "memory/knowledge/railway-deployment.md",
        "memory/milestones/2026-06-deploy-overhaul.md",
        "memory/knowledge/vercel-sandbox.md",
        "memory/knowledge/release-checklist.md",
        "memory/knowledge/code-execution-policy.md",
    ],
    "sandbox-exec": [
        "memory/knowledge/vercel-sandbox.md",
        "memory/knowledge/code-execution-policy.md",
        "memory/knowledge/railway-deployment.md",
        "memory/knowledge/release-checklist.md",
        "memory/milestones/2026-06-deploy-overhaul.md",
    ],
    "memory-governance": [
        "memory/knowledge/memory-gate.md",
        "memory/knowledge/platform-gate.md",
        "memory/milestones/2026-05-memory-two-planes.md",
        "memory/knowledge/audit-retention-policy.md",
    ],
    "feishu-channel": [
        "memory/knowledge/feishu-integration.md",
        "memory/milestones/2026-04-feishu-launch.md",
    ],
    "postgres-pool": [
        "memory/knowledge/postgres-tuning.md",
        "memory/knowledge/feishu-integration.md",
        "memory/milestones/2026-04-feishu-launch.md",
    ],
    "api-timeout-headroom": [
        "memory/knowledge/api-timeout-postmortem-archive.md",
        "memory/knowledge/api-timeout-runbook.md",
        "memory/knowledge/railway-deployment.md",
        "memory/knowledge/vercel-sandbox.md",
        "memory/knowledge/release-checklist.md",
    ],
    "governance-2hop-headroom": [
        "memory/knowledge/memory-gate.md",
        "memory/knowledge/platform-gate.md",
        "memory/milestones/2026-05-memory-two-planes.md",
        "memory/knowledge/audit-retention-policy.md",
    ],
    "negative-topic": [],
}


@pytest.fixture()
def fixture_root(tmp_path: Path) -> Path:
    build_memory_recall_fixture(tmp_path, FIXTURE_AGENT_ID)
    return tmp_path


def test_fixture_and_cases_are_stable(fixture_root: Path) -> None:
    cases = default_memory_recall_cases()
    assert [case.id for case in cases] == [
        "railway-deploy",
        "sandbox-exec",
        "memory-governance",
        "feishu-channel",
        "postgres-pool",
        "api-timeout-headroom",
        "governance-2hop-headroom",
        "negative-topic",
    ]
    pages = sorted(
        str(path.relative_to(fixture_root / str(FIXTURE_AGENT_ID) / "memory"))
        for path in (fixture_root / str(FIXTURE_AGENT_ID) / "memory").rglob("*.md")
    )
    assert len(pages) == 14


def test_wiki_ppr_baseline_is_deterministic_and_anchored(fixture_root: Path) -> None:
    first = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="ppr")
    second = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="ppr")
    assert first == second, "wiki PPR recall eval must be fully deterministic"

    assert first["benchmark"] == "memory_recall_baseline"
    assert first["method"] == "ppr"
    assert first["case_count"] == 8
    ranked = {case_id: report["ranked_page_ids"] for case_id, report in first["cases"].items()}
    assert ranked == WIKI_PPR_RANKED
    assert first["recall_at_k"] == pytest.approx(BASELINE_RECALL_AT_K)
    assert first["mrr"] == pytest.approx(BASELINE_MRR)


def test_wiki_ppr_reaches_two_hop_page_lexical_search_cannot(fixture_root: Path) -> None:
    """Multi-hop proof: code-execution-policy shares no term with the railway
    query and is reachable only via railway-deployment -> vercel-sandbox ->
    code-execution-policy."""
    ppr = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="ppr")
    assert "knowledge/code-execution-policy" in ppr["cases"]["railway-deploy"]["ranked_page_ids"]

    bm25 = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="bm25")
    assert "knowledge/code-execution-policy" not in bm25["cases"]["railway-deploy"]["ranked_page_ids"]


def test_component_headroom_is_reserved_for_dynamic_recall_layer(fixture_root: Path) -> None:
    """The two hard cases must stay imperfect under pure RRF/PPR — they are
    the proof surface for M2 (BaseLevel) and M5 (ContextBoost)."""
    report = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="ppr")
    assert report["cases"]["api-timeout-headroom"]["recall_at_k"] == 0.0
    assert report["cases"]["governance-2hop-headroom"]["recall_at_k"] == pytest.approx(2 / 3)
    assert report["recall_at_k"] < 1.0


@pytest.mark.asyncio
async def test_retriever_pipeline_baseline_is_deterministic_and_anchored(tmp_path: Path) -> None:
    # Determinism is judged across two FRESH fixture roots: within one root a
    # second run may legitimately score higher, because retrieval bumps the
    # access telemetry that feeds BaseLevel ("the more a memory is used, the
    # easier it is to recall" is the designed behavior, not drift).
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    build_memory_recall_fixture(root_a, FIXTURE_AGENT_ID)
    build_memory_recall_fixture(root_b, FIXTURE_AGENT_ID)
    first = await run_retriever_pipeline_eval(root_a, FIXTURE_AGENT_ID)
    second = await run_retriever_pipeline_eval(root_b, FIXTURE_AGENT_ID)
    assert first == second, "pipeline eval must be deterministic across fresh fixture roots"

    assert first["benchmark"] == "memory_retriever_pipeline_baseline"
    assert first["case_count"] == 8
    ranked = {case_id: report["ranked_sources"] for case_id, report in first["cases"].items()}
    assert ranked == RETRIEVER_PIPELINE_RANKED
    assert first["recall_at_k"] == pytest.approx(BASELINE_RECALL_AT_K)
    assert first["mrr"] == pytest.approx(BASELINE_MRR)


def test_negative_case_leaks_nothing(fixture_root: Path) -> None:
    report = run_memory_recall_eval(fixture_root, FIXTURE_AGENT_ID, method="ppr")
    assert report["cases"]["negative-topic"]["ranked_page_ids"] == []
    assert report["cases"]["negative-topic"]["recall_at_k"] == 1.0
