"""Memory eval benchmark — retrieval quality and retirement safety (P9).

Two deterministic evals over fixture corpora (no LLM, no network):

- ``evaluate_memory_retrieval``: BM25 vs PPR over a fixed wiki network with
  direct-hit and multi-hop cases (recall@3 / MRR per method). The verdict
  picks ``wiki_retrieval.DEFAULT_WIKI_METHOD`` — the experiment decides the
  default, not taste.
- ``evaluate_retirement_safety``: the decay lane must never surface
  protected or promoted entries as retirement candidates, and retirement
  must archive every removed line (reversibility, spec §4.9).

Both build their corpus under the caller's ``data_root`` (tests pass
tmp_path; operators pass a scratch dir) with a deterministic agent id, and
return structured reports in the prompt_eval/task_eval house style.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from pathlib import Path

_EVAL_AGENT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "hive-memory-eval.fixture")

# ── Retrieval-quality fixture: a small operations wiki ──
#
# Topology (links carry relevance the lexical layer cannot see):
#   deployment-pipeline ── depends_on ──> rollback-procedure
#   deployment-pipeline ── depends_on ──> secrets-rotation
#   rollback-procedure ── references ──> incident-response
#   incident-response ── depends_on ──> postmortem-template
#   stray-glossary (isolated, shares words with several queries)

_PAGES: dict[str, str] = {
    "deployment-pipeline": (
        "---\ntitle: Deployment Pipeline\ntype: concept\ntags: [deploy, release]\nstatus: active\n---\n\n"
        "## Current Claim\n\nEvery release flows through staged canary gates before production rollout.\n\n"
        "## Scope\n\nBackend release engineering.\n\n"
        "## Evidence\n\n- [decision] canary gates run before rollout #deploy\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\ndeploy, release, canary\n\n"
        "## Relations\n\n- depends_on [[Rollback Procedure]]\n- depends_on [[Secrets Rotation]]\n"
    ),
    "rollback-procedure": (
        "---\ntitle: Rollback Procedure\ntype: concept\ntags: [deploy]\nstatus: active\n---\n\n"
        "## Current Claim\n\nRoll back by re-pinning the previous image digest, never by hotfix-forward.\n\n"
        "## Scope\n\nFailed releases.\n\n"
        "## Evidence\n\n- [fact] digest re-pin restores service in minutes #deploy\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\nrollback, digest\n\n"
        "## Relations\n\n- references [[Incident Response]]\n"
    ),
    "secrets-rotation": (
        "---\ntitle: Secrets Rotation\ntype: concept\ntags: [security]\nstatus: active\n---\n\n"
        "## Current Claim\n\nMaster keys rotate quarterly with dual-control approval.\n\n"
        "## Scope\n\nProduction credentials.\n\n"
        "## Evidence\n\n- [decision] dual control for master keys #security\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\nsecrets, keys\n"
    ),
    "incident-response": (
        "---\ntitle: Incident Response\ntype: concept\ntags: [ops]\nstatus: active\n---\n\n"
        "## Current Claim\n\nSev1 pages the on-call pair; comms updates go out every 30 minutes.\n\n"
        "## Scope\n\nProduction incidents.\n\n"
        "## Evidence\n\n- [fact] paired on-call halves the mitigation time #ops\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\nincident, on-call\n\n"
        "## Relations\n\n- depends_on [[Postmortem Template]]\n"
    ),
    "postmortem-template": (
        "---\ntitle: Postmortem Template\ntype: concept\ntags: [ops]\nstatus: active\n---\n\n"
        "## Current Claim\n\nBlameless write-ups capture timeline, impact, and action items within 48 hours.\n\n"
        "## Scope\n\nAfter every Sev1/Sev2.\n\n"
        "## Evidence\n\n- [fact] 48h capture keeps details accurate #ops\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\npostmortem, blameless\n"
    ),
    "stray-glossary": (
        "---\ntitle: Stray Glossary\ntype: concept\ntags: [misc]\nstatus: active\n---\n\n"
        "## Current Claim\n\nA glossary of release and incident words with no operational links.\n\n"
        "## Scope\n\nTerminology only.\n\n"
        "## Evidence\n\n- [fact] words: release, incident, rollout, on-call #misc\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-01] created\n\n"
        "## Retrieval Tags\n\nglossary\n"
    ),
}

# kind=direct: query words appear on the expected page (lexical reachable).
# kind=multi_hop: the expected page shares no query terms — only the link
# network connects it to the query topic.
_CASES: list[dict] = [
    {"query": "canary gates production rollout", "expected": "wiki/deployment-pipeline", "kind": "direct"},
    {"query": "re-pin previous image digest", "expected": "wiki/rollback-procedure", "kind": "direct"},
    {"query": "quarterly master key dual control", "expected": "wiki/secrets-rotation", "kind": "direct"},
    {"query": "blameless timeline action items", "expected": "wiki/postmortem-template", "kind": "direct"},
    # Multi-hop: "canary rollout release" words live on deployment-pipeline;
    # rollback-procedure / secrets-rotation are its dependencies.
    {"query": "canary release rollout gates", "expected": "wiki/rollback-procedure", "kind": "multi_hop"},
    {"query": "canary release rollout gates", "expected": "wiki/secrets-rotation", "kind": "multi_hop"},
    # Multi-hop two levels: incident words → postmortem dependency.
    {"query": "sev1 on-call pages comms", "expected": "wiki/postmortem-template", "kind": "multi_hop"},
]

_RECALL_K = 3


def _seed_retrieval_corpus(data_root: Path, agent_id: uuid.UUID) -> None:
    wiki_dir = Path(data_root) / str(agent_id) / "memory" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for slug, body in _PAGES.items():
        (wiki_dir / f"{slug}.md").write_text(body, encoding="utf-8")


def evaluate_memory_retrieval(*, data_root: Path) -> dict:
    """Run the BM25-vs-PPR benchmark. Returns a structured report."""
    from app.memory.relation_graph import build_relation_graph
    from app.memory.wiki_retrieval import search_wiki_pages

    agent_id = _EVAL_AGENT_ID
    _seed_retrieval_corpus(data_root, agent_id)
    graph = build_relation_graph(data_root, agent_id)

    case_rows: list[dict] = []
    totals: dict[str, dict[str, float]] = {
        "bm25": {"hits": 0.0, "rr": 0.0},
        "ppr": {"hits": 0.0, "rr": 0.0},
    }
    multi_hop_hits = {"bm25": 0.0, "ppr": 0.0}
    multi_hop_total = sum(1 for case in _CASES if case["kind"] == "multi_hop")

    for case in _CASES:
        row: dict = {"query": case["query"], "expected": case["expected"], "kind": case["kind"]}
        for method in ("bm25", "ppr"):
            hits = search_wiki_pages(data_root, agent_id, case["query"], method=method, limit=_RECALL_K, graph=graph)
            ranked_ids = [hit["page_id"] for hit in hits]
            recalled = case["expected"] in ranked_ids
            rank = ranked_ids.index(case["expected"]) + 1 if recalled else 0
            row[method] = {"recalled": recalled, "rank": rank, "top": ranked_ids}
            if recalled:
                totals[method]["hits"] += 1
                totals[method]["rr"] += 1.0 / rank
                if case["kind"] == "multi_hop":
                    multi_hop_hits[method] += 1
        case_rows.append(row)

    case_count = len(_CASES)
    methods_report = {
        method: {
            "recall_at_3": round(stats["hits"] / case_count, 3),
            "mrr": round(stats["rr"] / case_count, 3),
        }
        for method, stats in totals.items()
    }
    multi_hop_report = {
        "bm25_recall": round(multi_hop_hits["bm25"] / multi_hop_total, 3) if multi_hop_total else 0.0,
        "ppr_recall": round(multi_hop_hits["ppr"] / multi_hop_total, 3) if multi_hop_total else 0.0,
        "cases": multi_hop_total,
    }

    # The experiment's verdict: PPR wins the default when it strictly beats
    # BM25 on the multi-hop slice without losing overall recall.
    ppr_wins = (
        multi_hop_report["ppr_recall"] > multi_hop_report["bm25_recall"]
        and methods_report["ppr"]["recall_at_3"] >= methods_report["bm25"]["recall_at_3"]
    )

    return {
        "agent_id": str(agent_id),
        "cases": case_rows,
        "methods": methods_report,
        "multi_hop": multi_hop_report,
        "default_method": "ppr" if ppr_wins else "bm25",
    }


# ── Retirement safety ──


def _seed_retirement_corpus(data_root: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = Path(data_root) / str(agent_id) / "memory"
    t3_dir = mem_dir / "t3"
    t3_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# T3 Capabilities",
        "",
        # Protected foundational principle — cold but preservation-flagged.
        "- [2026-01-01][entry_id=mem_protected][access_count=0][last_accessed=never] "
        "never bypass the memory write gate for durable writes",
        # Promoted strategy — left the candidate pool.
        "- [2026-02-01][entry_id=mem_promoted][promoted_to=skill][access_count=0][last_accessed=never] "
        "research design verify workflow promoted to a skill",
        # Hot entry — must not be ranked for retirement ahead of cold ones.
        "- [2026-05-01][entry_id=mem_hot][access_count=9][last_accessed=2026-06-04T00:00:00+00:00] "
        "deploys require staged canary gates",
        # Plain cold entries — the legitimate decay-lane candidates.
        "- [2026-01-02][entry_id=mem_cold1][access_count=0][last_accessed=never] legacy proxy port mapping",
        "- [2026-01-03][entry_id=mem_cold2][access_count=0][last_accessed=never] deprecated webhook retry table",
    ]
    (t3_dir / "capabilities.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mem_dir


def evaluate_retirement_safety(*, data_root: Path) -> dict:
    """Assert the decay lane's safety properties on a fixture corpus."""
    from app.memory.md_store import list_retirement_candidates
    from app.memory.t3_store import retire_t3_entries

    agent_id = uuid.uuid5(uuid.NAMESPACE_DNS, "hive-retirement-eval.fixture")
    mem_dir = _seed_retirement_corpus(data_root, agent_id)

    checks: list[dict] = []

    candidates = list_retirement_candidates(
        data_root,
        agent_id,
        limit=10,
        protected_markers=["never bypass the memory write gate"],
    )
    candidate_ids = [candidate["entry_id"] for candidate in candidates]

    checks.append(
        {
            "name": "protected_entries_never_candidates",
            "passed": "mem_protected" not in candidate_ids,
            "detail": f"candidates={candidate_ids}",
        }
    )
    checks.append(
        {
            "name": "promoted_entries_never_candidates",
            "passed": "mem_promoted" not in candidate_ids,
            "detail": f"candidates={candidate_ids}",
        }
    )
    checks.append(
        {
            "name": "cold_entries_rank_before_hot",
            "passed": bool(candidate_ids)
            and candidate_ids[0] in ("mem_cold1", "mem_cold2")
            and (candidate_ids.index("mem_hot") > 1 if "mem_hot" in candidate_ids else True),
            "detail": f"order={candidate_ids}",
        }
    )

    # Reversibility: retiring the cold entries archives every removed line
    # with a lifecycle record — nothing is silently deleted.
    retired = retire_t3_entries(
        data_root,
        agent_id,
        filename="t3/capabilities.md",
        drops=["legacy proxy port mapping", "deprecated webhook retry table"],
        reason="cap_eviction",
    )
    archive_text = (mem_dir / "archive.md").read_text(encoding="utf-8") if (mem_dir / "archive.md").exists() else ""
    lifecycle_records: list[dict] = []
    lifecycle_path = mem_dir / "lifecycle.json"
    if lifecycle_path.exists():
        lifecycle_records = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    archived_ids = {record["id"] for record in lifecycle_records if record.get("status") == "archived"}

    checks.append(
        {
            "name": "cap_eviction_archives_everything",
            "passed": (
                retired == 2
                and "legacy proxy port mapping" in archive_text
                and "deprecated webhook retry table" in archive_text
                and {"mem_cold1", "mem_cold2"} <= archived_ids
            ),
            "detail": f"retired={retired}, archived_lifecycle_ids={sorted(archived_ids)}",
        }
    )

    active_after = (mem_dir / "t3" / "capabilities.md").read_text(encoding="utf-8")
    checks.append(
        {
            "name": "active_file_keeps_protected_and_hot",
            "passed": "never bypass the memory write gate" in active_after and "canary gates" in active_after,
            "detail": "protected + hot entries survive the retirement pass",
        }
    )

    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def run_memory_eval_suite(*, data_root: Path) -> dict:
    """Run the deterministic memory retrieval gates used by CI."""
    from app.memory.wiki_retrieval import DEFAULT_WIKI_METHOD

    retrieval_report = evaluate_memory_retrieval(data_root=data_root)
    retirement_report = evaluate_retirement_safety(data_root=data_root)
    default_matches_eval = retrieval_report["default_method"] == DEFAULT_WIKI_METHOD
    checks = [
        {
            "name": "default_method_matches_eval_verdict",
            "passed": default_matches_eval,
            "detail": (
                f"configured={DEFAULT_WIKI_METHOD}, "
                f"eval_verdict={retrieval_report['default_method']}"
            ),
        },
        {
            "name": "retirement_safety_passes",
            "passed": bool(retirement_report["passed"]),
            "detail": "protected/promoted entries survive and retired entries are archived",
        },
    ]
    return {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "memory_retrieval": retrieval_report,
        "retirement_safety": retirement_report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic memory retrieval and retirement evals.")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Scratch AGENT_DATA_DIR root for fixture corpora. Defaults to a temporary directory.",
    )
    args = parser.parse_args(argv)

    if args.data_root is None:
        with tempfile.TemporaryDirectory(prefix="hive-memory-eval-") as tmp:
            report = run_memory_eval_suite(data_root=Path(tmp))
    else:
        args.data_root.mkdir(parents=True, exist_ok=True)
        report = run_memory_eval_suite(data_root=args.data_root)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
