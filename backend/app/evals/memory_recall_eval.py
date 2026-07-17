"""M0 offline memory-recall eval harness (dynamic-memory-activation design §8).

Deterministic, DB-free, LLM-free eval over the REAL memory retrieval paths:

- ``run_memory_recall_eval``: ranks the fixture page graph through
  ``search_wiki_pages`` (BM25 seeds, optional PPR expansion) — the knowledge
  plane's production ranking function.
- ``run_retriever_pipeline_eval``: runs the full ``MemoryRetriever.retrieve``
  pipeline including ``ActivationScorer``, so later scorer components
  (BaseLevel, ContextBoost, TaskModulation) show up in this scorecard.

Both runners emit a scorecard with per-case ranked ids plus recall@k and MRR.
The companion baseline test pins the exact ranked orders: Q-shrink (QKV
retirement) must not move a single position; M2/M5 must raise the aggregate
scores relative to this baseline before shipping.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from contextlib import contextmanager

from app.memory.activation import ActivationContext
from app.memory.lifecycle_store import bump_access_telemetry, lifecycle_path
from app.memory.relation_graph import KNOWLEDGE_PAGE_DIRS
from app.memory.retriever import MemoryRetriever
from app.memory.wiki_retrieval import search_wiki_pages
from app.services.principal_context import PrincipalStack


@dataclass(frozen=True, slots=True)
class MemoryRecallCase:
    id: str
    query: str
    expected_page_ids: tuple[str, ...]
    k: int = 5


_FIXTURE_PAGES: tuple[tuple[str, str], ...] = (
    (
        "knowledge/railway-deployment.md",
        """---
title: Railway Deployment
status: active
tags: railway, deploy
---

Railway production deploy runs three archives: backend, backend-api and
frontend. Submit each service then poll until every target reports success.

## Relations
- relates_to [[Release Checklist]]
- relates_to [[Vercel Sandbox]]
""",
    ),
    (
        "knowledge/release-checklist.md",
        """---
title: Release Checklist
status: active
tags: checklist, verification
---

Release checklist steps: archive upload order, status polling and health
verification steps, then a final checklist review before announcing
completion of the checklist.

## Relations
- part_of [[Railway Deployment]]
""",
    ),
    (
        "knowledge/vercel-sandbox.md",
        """---
title: Vercel Sandbox
status: active
tags: sandbox
---

Vercel sandbox is the hosted provider for agent-controlled code execution;
credentials stay isolated from the agent environment.

## Relations
- relates_to [[Code Execution Policy]]
""",
    ),
    (
        "knowledge/code-execution-policy.md",
        """---
title: Code Execution Policy
status: active
tags: policy
---

Code execution must go through the provider boundary: bubblewrap or
sandbox-exec on trusted hosts, never a raw subprocess.
""",
    ),
    (
        "knowledge/memory-gate.md",
        """---
title: Memory Gate
status: active
tags: memory, governance
---

Memory gate reviews candidate packages against the rubric before anything
reaches durable governance surfaces.

## Relations
- relates_to [[Platform Gate]]
""",
    ),
    (
        "knowledge/platform-gate.md",
        """---
title: Platform Gate
status: active
tags: commit
---

Commits accepted blocks atomically and records an audit trail for every
write.

## Relations
- relates_to [[Audit Retention Policy]]
""",
    ),
    (
        "knowledge/audit-retention-policy.md",
        """---
title: Audit Retention Policy
status: active
tags: retention
---

Retention windows for committed records and evidence bundles.
""",
    ),
    (
        "knowledge/gate-review-checklist.md",
        """---
title: Gate Review Checklist
status: active
tags: checklist
---

Review checklist steps before a gate decision is recorded. The committee
walks the evidence bundle, confirms provenance annotations on every block,
and then files the final disposition into the register with its rationale.

## Relations
- part_of [[Platform Gate]]
""",
    ),
    (
        "knowledge/api-timeout-runbook.md",
        """---
title: Api Timeout Runbook
status: active
tags: api, timeout
---

Retry budget and backoff guidance for api timeout incidents in production
services. Current escalation path lives here.
""",
    ),
    (
        "knowledge/api-timeout-postmortem-archive.md",
        """---
title: Api Timeout Postmortem Archive
status: active
tags: api, timeout
---

Archived api timeout retry postmortems: timeout retry analysis, api retry
timelines, timeout retry graphs and retry appendix material kept for
reference only.
""",
    ),
    (
        "knowledge/feishu-integration.md",
        """---
title: Feishu Integration
status: active
tags: feishu, channel
---

Feishu channel uses a websocket long connection for message delivery and
interactive cards. 飞书长连接負責消息投递。
""",
    ),
    (
        "knowledge/postgres-tuning.md",
        """---
title: Postgres Tuning
status: active
tags: database
---

Postgres connection pool sizing: watch queuepool waits and statement
timeouts under load.
""",
    ),
    (
        "milestones/2026-06-deploy-overhaul.md",
        """---
title: 2026 06 Deploy Overhaul
status: active
tags: milestone
---

June milestone: production deploy overhaul completed across services.

## Relations
- relates_to [[Railway Deployment]]
""",
    ),
    (
        "milestones/2026-05-memory-two-planes.md",
        """---
title: 2026 05 Memory Two Planes
status: active
tags: milestone
---

May milestone: memory system converged on the two planes design.

## Relations
- relates_to [[Memory Gate]]
""",
    ),
    (
        "milestones/2026-04-feishu-launch.md",
        """---
title: 2026 04 Feishu Launch
status: active
tags: milestone
---

April milestone: feishu channel launch reached general availability.

## Relations
- relates_to [[Feishu Integration]]
""",
    ),
)

_DEFAULT_CASES: tuple[MemoryRecallCase, ...] = (
    MemoryRecallCase(
        id="railway-deploy",
        query="railway production deploy",
        expected_page_ids=(
            "knowledge/railway-deployment",
            "knowledge/release-checklist",
            "milestones/2026-06-deploy-overhaul",
        ),
    ),
    MemoryRecallCase(
        id="sandbox-exec",
        query="sandbox code execution provider",
        expected_page_ids=(
            "knowledge/vercel-sandbox",
            "knowledge/code-execution-policy",
        ),
    ),
    MemoryRecallCase(
        id="memory-governance",
        query="memory gate governance rubric",
        expected_page_ids=(
            "knowledge/memory-gate",
            "knowledge/platform-gate",
        ),
    ),
    MemoryRecallCase(
        id="feishu-channel",
        query="feishu channel message delivery",
        expected_page_ids=(
            "knowledge/feishu-integration",
            "milestones/2026-04-feishu-launch",
        ),
    ),
    MemoryRecallCase(
        id="postgres-pool",
        query="postgres connection pool sizing",
        expected_page_ids=("knowledge/postgres-tuning",),
    ),
    # Hard case (BaseLevel headroom, M2): the archive page stuffs the query
    # terms so lexical ranking puts it above the runbook; frequency-of-use
    # reinforcement is what should eventually flip the order.
    MemoryRecallCase(
        id="api-timeout-headroom",
        query="api timeout retry",
        expected_page_ids=("knowledge/api-timeout-runbook",),
        k=1,
    ),
    # Hard case (ContextBoost headroom, M5): two checklist pages tie on the
    # query terms; only the session context (already about platform-gate)
    # can disambiguate toward the governance-cluster checklist.
    MemoryRecallCase(
        id="context-disambiguation-headroom",
        query="review checklist steps",
        expected_page_ids=("knowledge/gate-review-checklist",),
        k=1,
    ),
    # Non-hijack guard: the retention page shares no term with the query while
    # a competitor carries a real lexical hit — a bounded context boost must
    # NOT push the zero-overlap page past genuine relevance (recall stays 2/3).
    MemoryRecallCase(
        id="governance-2hop-headroom",
        query="memory gate governance rubric",
        expected_page_ids=(
            "knowledge/memory-gate",
            "knowledge/platform-gate",
            "knowledge/audit-retention-policy",
        ),
        k=3,
    ),
    MemoryRecallCase(
        id="negative-topic",
        query="quantum blockchain oracle",
        expected_page_ids=(),
    ),
)


def build_memory_recall_fixture(data_root: Path, agent_id: uuid.UUID) -> None:
    """Write the deterministic knowledge/milestones page graph to disk."""
    memory_dir = Path(data_root) / str(agent_id) / "memory"
    for relative_path, content in _FIXTURE_PAGES:
        path = memory_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def default_memory_recall_cases() -> tuple[MemoryRecallCase, ...]:
    return _DEFAULT_CASES


@contextmanager
def _sidecar_snapshot(data_root: Path, agent_id: uuid.UUID):
    """Restore the lifecycle sidecar after the block (case isolation)."""
    path = lifecycle_path(data_root, agent_id)
    before = path.read_bytes() if path.exists() else None
    try:
        yield
    finally:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)


def seed_base_level_telemetry(data_root: Path, agent_id: uuid.UUID, *, now: datetime) -> None:
    """Seed deterministic usage history for the M2 BaseLevel comparison run.

    The api-timeout runbook is the frequently-consulted page (a full recent
    ring); the lexically-stuffed archive has never been touched. With the
    BaseLevel term active, frequency must flip the headroom case.
    """
    for hours_ago in range(8, 0, -1):
        bump_access_telemetry(
            data_root,
            agent_id,
            entry_id="knowledge/api-timeout-runbook",
            now=now - timedelta(hours=hours_ago),
            create_if_missing=True,
        )


def _score_cases(
    ranked_ids_by_case: dict[str, list[str]],
    cases: tuple[MemoryRecallCase, ...],
    *,
    ranked_scores_by_case: dict[str, list[float]] | None = None,
) -> dict:
    case_reports: dict[str, dict] = {}
    recall_values: list[float] = []
    reciprocal_ranks: list[float] = []
    for case in cases:
        ranked = ranked_ids_by_case.get(case.id, [])
        top = ranked[: case.k]
        expected = set(case.expected_page_ids)
        if expected:
            found = [page_id for page_id in top if page_id in expected]
            recall = len(set(found)) / len(expected)
            reciprocal = 0.0
            for position, page_id in enumerate(top, start=1):
                if page_id in expected:
                    reciprocal = 1.0 / position
                    break
            reciprocal_ranks.append(reciprocal)
        else:
            # Candidate visibility is not a semantic relevance claim. A
            # negative query passes when every visible top-k candidate has a
            # zero ranking signal; do not require mechanical candidate deletion.
            top_scores = (ranked_scores_by_case or {}).get(case.id, [])[: case.k]
            recall = 1.0 if not any(float(score or 0.0) > 0.0 for score in top_scores) else 0.0
        recall_values.append(recall)
        case_reports[case.id] = {
            "query": case.query,
            "k": case.k,
            "expected_page_ids": list(case.expected_page_ids),
            "recall_at_k": round(recall, 6),
            "reciprocal_rank": round(reciprocal_ranks[-1], 6) if expected else None,
        }
    aggregate_recall = sum(recall_values) / len(recall_values) if recall_values else 0.0
    aggregate_mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    return {
        "case_count": len(cases),
        "recall_at_k": round(aggregate_recall, 6),
        "mrr": round(aggregate_mrr, 6),
        "cases": case_reports,
    }


def run_memory_recall_eval(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    cases: tuple[MemoryRecallCase, ...] | None = None,
    method: str = "ppr",
) -> dict:
    """Rank fixture pages through the real ``search_wiki_pages`` path."""
    active_cases = cases or _DEFAULT_CASES
    ranked_by_case: dict[str, list[str]] = {}
    scores_by_case: dict[str, list[float]] = {}
    for case in active_cases:
        hits = search_wiki_pages(
            data_root,
            agent_id,
            case.query,
            method=method,
            limit=case.k,
            page_dirs=KNOWLEDGE_PAGE_DIRS,
        )
        ranked_by_case[case.id] = [str(hit.get("page_id") or "") for hit in hits]
        scores_by_case[case.id] = [float(hit.get("score") or 0.0) for hit in hits]
    report = _score_cases(
        ranked_by_case,
        active_cases,
        ranked_scores_by_case=scores_by_case,
    )
    for case in active_cases:
        report["cases"][case.id]["ranked_page_ids"] = ranked_by_case[case.id]
        report["cases"][case.id]["ranked_scores"] = scores_by_case[case.id]
    report["benchmark"] = "memory_recall_baseline"
    report["method"] = method
    return report


async def run_retriever_pipeline_eval(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    cases: tuple[MemoryRecallCase, ...] | None = None,
    now: datetime | None = None,
    working_sets: dict[str, tuple[tuple[str, float], ...]] | None = None,
) -> dict:
    """Rank fixture pages through the full retriever + activation pipeline.

    Sources are memory-relative markdown paths (``memory/<dir>/<slug>.md``);
    expected page ids are mapped onto that shape for scoring so the same
    cases drive both runners. ``now`` pins the BaseLevel decay clock for
    deterministic scoring against seeded telemetry; ``working_sets`` supplies
    a per-case session working set for the M5 ContextBoost comparison run.
    """
    active_cases = cases or _DEFAULT_CASES
    retriever = MemoryRetriever(data_root=data_root)
    ranked_by_case: dict[str, list[str]] = {}
    scores_by_case: dict[str, list[float]] = {}
    for case in active_cases:
        context = ActivationContext(
            query=case.query,
            principal_stack=PrincipalStack(),
            now=now,
            working_set=(working_sets or {}).get(case.id, ()),
        )
        # Cases are independent query scenarios: retrieval's own access bumps
        # must not leak usage history from one case into the next, so the
        # sidecar is restored after each case (seeded telemetry stays fixed).
        with _sidecar_snapshot(data_root, agent_id):
            # This scorecard measures deterministic candidate ranking, not
            # live automatic body selection. Production prompt assembly uses
            # ``retrieve`` and therefore still requires the model selector.
            items = await retriever._gather_authorized_candidates(
                agent_id,
                case.query,
                session_id=None,
                tenant_id=None,
                activation_context=context,
            )
        ranked_by_case[case.id] = [item.source for item in items]
        scores_by_case[case.id] = [item.score for item in items]

    def _as_source(page_id: str) -> str:
        return f"memory/{page_id}.md"

    scoring_cases = tuple(
        MemoryRecallCase(
            id=case.id,
            query=case.query,
            expected_page_ids=tuple(_as_source(page_id) for page_id in case.expected_page_ids),
            k=case.k,
        )
        for case in active_cases
    )
    report = _score_cases(
        ranked_by_case,
        scoring_cases,
        ranked_scores_by_case=scores_by_case,
    )
    for case in active_cases:
        report["cases"][case.id]["ranked_sources"] = ranked_by_case[case.id]
        report["cases"][case.id]["ranked_scores"] = scores_by_case[case.id]
        report["cases"][case.id]["expected_page_ids"] = list(case.expected_page_ids)
    report["benchmark"] = "memory_retriever_pipeline_baseline"
    return report
