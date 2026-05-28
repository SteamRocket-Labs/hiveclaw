from __future__ import annotations

import asyncio
import json

import pytest


def _long_v2_report(source_ids: list[str]) -> str:
    refs = source_ids + source_ids[:1]
    return f"""# V2 RWA Source-Ledger Audit

## Executive Thesis

The v2 worker-orchestrator path collected digest-first evidence across 2024, 2025,
and 2026. Issuer A, Regulator B, and Platform C each support a
specific part of the custody, transfer-control, reporting, and secondary-liquidity
claim set. The audit confidence is moderate because 3 fetched sources cover 3
independent lanes, while jurisdiction-specific interpretation still needs legal
review. Sources: {refs[0]}, {refs[1]}, {refs[2]}.

## Method And Source Standard

The run used parallel orchestrator-worker calls to collect fetched source text,
then compressed those results into worker digests before final synthesis. Primary
issuer and regulator evidence outranks secondary market commentary. Search snippets
were not cited as evidence, and every source id in the prose must resolve to the
ledger before completion.

## Findings

- Issuer A reports 35% adoption growth across 12 jurisdictions in 2026, but the
  growth claim is only complete when paired with Regulator B disclosure language.
  Sources: {refs[0]}, {refs[1]}.
- Platform C documents 18 transfer-control checks and 7 reporting checkpoints,
  which makes the product workflow materially more complex than a simple token
  sale. Sources: {refs[2]}, {refs[0]}.
- The source set covers 4 custody constraints that affect 3 product categories; the
  report should treat the custody statement as verified while keeping secondary
  liquidity as inferred. Sources: {refs[1]}, {refs[2]}.

## Source Ledger

- `{refs[0]}` issuer source
- `{refs[1]}` regulator source
- `{refs[2]}` platform source

The worker digest path is long enough to satisfy the synthesis quality gate while
remaining grounded in source ids. Additional audit notes preserve open gaps, source
counts, and citation mapping so the final report can be inspected without replaying
raw source text through the writer model.
"""


class _DigestReasoner:
    def __init__(self):
        self.worker_results_seen = None

    async def refine_plan(self, request, plan):
        return plan

    async def extract_claims(self, request, source):
        return [
            {
                "text": f"{source.publisher} supports a v2 worker-sourced research claim with concrete evidence.",
                "status": "verified",
                "source_ids": [source.source_id],
                "evidence": source.content[:240],
            }
        ]

    async def summarize_source(self, request, source):
        return {
            "source_id": source.source_id,
            "relevance_score": 0.9,
            "credibility_score": 0.8,
            "key_entities": ["Issuer A", "Regulator B", "Platform C"],
            "key_numbers": ["35% growth", "12 jurisdictions", "18 controls"],
            "key_dates": ["2026"],
            "mechanisms": ["transfer controls", "custody reporting"],
            "limitations": ["secondary liquidity still inferred"],
            "source_bound_summary": f"{source.publisher} provides worker-sourced evidence.",
        }

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results):
        self.worker_results_seen = worker_results
        return _long_v2_report(list(ledger.sources))


class _ParallelWorkerRunner:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.topics: list[str] = []

    async def run(self, topic: str, *, request, cancel_event=None):
        from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

        self.topics.append(topic)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        source_index = len(self.topics)
        try:
            await asyncio.sleep(0.03)
            content = (
                f"Title: Worker source {source_index}\n"
                f"Issuer A and Regulator B disclose 35% growth, 12 jurisdictions, "
                f"18 controls, and 7 reporting checkpoints in 2026 for topic {topic}."
            )
            return WorkerResult(
                topic=topic,
                intermediate_report=f"Digest {source_index}: {topic} found concrete source-bound evidence.",
                sources=[
                    SourceRecord(
                        source_id="",
                        url=f"https://worker{source_index}.example/source-{source_index}",
                        title=f"Worker source {source_index}",
                        publisher=f"worker{source_index}.example",
                        source_type=SourceType.PRIMARY,
                        content=content,
                        lane_id="",
                        fetch_tool="web_fetch",
                    )
                ],
                status="ok",
                tokens_used=50 + source_index,
            )
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_orchestrator_v2_runs_parallel_workers_persists_digests_and_synthesizes_from_them(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fail_if_linear_tool_called(tool_name: str, arguments: dict) -> str:
        raise AssertionError(f"v2 worker path should not call linear tool invoker directly: {tool_name}")

    reasoner = _DigestReasoner()
    worker_runner = _ParallelWorkerRunner()
    result = await DeepResearchOrchestrator(
        fail_if_linear_tool_called,
        reasoner=reasoner,
        worker_runner=worker_runner,
    ).run(
        ResearchRequest(
            question="Audit RWA custody claims with v2 workers.",
            mode="source_ledger_audit",
            depth="quick",
            max_rounds=1,
            max_sources=3,
            concurrency=3,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed"
    assert worker_runner.max_active > 1, "v2 orchestrator must fan out worker subtasks concurrently"
    assert len(reasoner.worker_results_seen) >= 3
    assert (tmp_path / "worker_reports.jsonl").is_file()
    worker_reports = [
        json.loads(line)
        for line in (tmp_path / "worker_reports.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(worker_reports) >= 3
    assert all(report["intermediate_report"].startswith("Digest") for report in worker_reports)
    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))
    assert final["worker_reports_path"] == (tmp_path / "worker_reports.jsonl").as_posix()
    assert final["quality_gates"]["synthesis"] == "passed"
    lane_summaries = [
        json.loads(line)
        for line in (tmp_path / "lane_summaries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lane_summaries, "orchestrator must infer lane ids for runtime worker-captured sources"
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Footnotes" in report
    assert "src_unknown" not in report
