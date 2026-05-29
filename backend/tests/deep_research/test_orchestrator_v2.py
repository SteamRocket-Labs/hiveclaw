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


def test_max_sources_default_scales_with_depth():
    from app.services.deep_research.schemas import ResearchRequest

    assert ResearchRequest.from_arguments({"question": "q", "depth": "quick"}).max_sources == 6
    assert ResearchRequest.from_arguments({"question": "q", "depth": "standard"}).max_sources == 12
    assert ResearchRequest.from_arguments({"question": "q", "depth": "full"}).max_sources == 30
    assert ResearchRequest.from_arguments({"question": "q", "depth": "flagship"}).max_sources == 40
    # An explicit value always wins over the depth default.
    assert ResearchRequest.from_arguments({"question": "q", "depth": "full", "max_sources": 5}).max_sources == 5


def test_select_sources_round_robin_is_fair_across_workers():
    from collections import Counter

    from app.services.deep_research.orchestrator import _select_sources_round_robin
    from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

    def _worker(worker_index: int, source_count: int) -> WorkerResult:
        return WorkerResult(
            topic=f"topic-{worker_index}",
            intermediate_report="digest",
            status="ok",
            sources=[
                SourceRecord(
                    source_id="",
                    url=f"https://w{worker_index}.example/s{s}",
                    title=f"W{worker_index} source {s}",
                    publisher=f"w{worker_index}.example",
                    source_type=SourceType.PRIMARY,
                    content="c" * 120,
                )
                for s in range(source_count)
            ],
        )

    # Worker 0 floods 5 sources; workers 1 and 2 bring 3 each. Budget is below the total.
    workers = [_worker(0, 5), _worker(1, 3), _worker(2, 3)]
    selected = _select_sources_round_robin(workers, max_sources=6)

    assert len(selected) == 6
    contributing = {result.topic for result, _ in selected}
    assert contributing == {"topic-0", "topic-1", "topic-2"}, "every worker must contribute under a tight budget"
    counts = Counter(result.topic for result, _ in selected)
    assert counts["topic-0"] <= 2, "the first worker must not monopolise the source budget"


def test_select_sources_round_robin_dedupes_and_uses_full_budget():
    from app.services.deep_research.orchestrator import _select_sources_round_robin
    from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

    shared = SourceRecord(
        source_id="",
        url="https://dup.example/shared",
        title="shared",
        publisher="dup.example",
        source_type=SourceType.PRIMARY,
        content="c" * 120,
    )

    def _worker(worker_index: int, urls: list[str]) -> WorkerResult:
        return WorkerResult(
            topic=f"topic-{worker_index}",
            intermediate_report="digest",
            status="ok",
            sources=[
                SourceRecord(
                    source_id="",
                    url=url,
                    title=url,
                    publisher="x.example",
                    source_type=SourceType.PRIMARY,
                    content="c" * 120,
                )
                for url in urls
            ],
        )

    workers = [
        _worker(0, ["https://a.example/1", shared.url]),
        _worker(1, [shared.url, "https://b.example/1"]),
    ]
    selected = _select_sources_round_robin(workers, max_sources=8)
    urls = [source.url for _, source in selected]
    assert urls.count(shared.url) == 1, "duplicate urls across workers must be ledgered once"
    assert len(selected) == 3


@pytest.mark.asyncio
async def test_worker_topics_fallback_focuses_each_worker_on_its_lane():
    from app.services.deep_research.orchestrator import _worker_topics
    from app.services.deep_research.schemas import ResearchLane, ResearchPlan, ResearchRequest, SearchQuery

    long_question = "Analyze the RWA tokenization market " + "across many dimensions and angles " * 30
    plan = ResearchPlan(
        question=long_question,
        mode="industry_research",
        lanes=[
            ResearchLane(
                lane_id="market",
                label="Market Data",
                goal="size the market",
                queries=[SearchQuery(query="RWA market size 2026")],
            ),
            ResearchLane(
                lane_id="regulatory",
                label="Regulation",
                goal="map the rules",
                queries=[SearchQuery(query="RWA regulation MiCA")],
            ),
        ],
    )
    request = ResearchRequest(question=long_question, mode="industry_research")

    # reasoner=None forces the deterministic fallback path.
    topics = await _worker_topics(None, request, plan)

    assert len(topics) == 2
    assert "Market Data" in topics[0] and "Regulation" in topics[1], "each worker must target its own lane"
    # The 10-dimension mega-question must NOT be pasted verbatim into every worker (RC3 token blow-up).
    assert long_question not in topics[0]
    assert long_question not in topics[1]


class _SubFloorReasoner:
    """Synthesis comes in under the full-depth char floor, exercising the F5 narrowed fallback."""

    async def refine_plan(self, request, plan):
        return plan

    async def extract_claims(self, request, source):
        return [
            {
                "text": f"{source.publisher} reports 35% growth across 12 jurisdictions in 2026.",
                "status": "verified",
                "source_ids": [source.source_id],
                "evidence": source.content[:200],
            }
        ]

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results, **kwargs):
        ids = list(ledger.sources)
        refs = (ids + ids[:2])[:3]
        # Real sections + citations but deliberately under the full-depth (1200-char) floor.
        return (
            "# RWA Narrowed Brief\n\n"
            "## Executive Thesis\n\n"
            f"Issuer A and Platform C show 35% growth in 2026 [{refs[0]}], though coverage is partial [{refs[1]}].\n\n"
            "## Findings\n\n"
            f"- Issuer A reports 35% adoption growth across 12 jurisdictions in 2026 [{refs[0]}].\n"
            f"- Platform C documents 18 transfer-control checks in its custody workflow [{refs[1]}].\n\n"
            "## Source Ledger\n\n"
            f"- `{refs[0]}` issuer source\n"
            f"- `{refs[1]}` platform source\n"
        )


@pytest.mark.asyncio
async def test_orchestrator_v2_delivers_narrowed_report_when_synthesis_is_under_full_floor(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fail_if_linear_tool_called(tool_name: str, arguments: dict) -> str:
        raise AssertionError(f"v2 worker path should not call linear tool invoker: {tool_name}")

    result = await DeepResearchOrchestrator(
        fail_if_linear_tool_called,
        reasoner=_SubFloorReasoner(),
        worker_runner=_ParallelWorkerRunner(),
    ).run(
        ResearchRequest(
            question="Analyze the RWA tokenization market.",
            mode="industry_research",
            depth="full",
            max_rounds=1,
            max_sources=3,
            concurrency=3,
        ),
        artifact_dir=tmp_path,
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Coverage notice" in report, "a sub-floor report with real evidence must be delivered as narrowed"
    assert result.status != "failed", "evidence existed; deliver a narrowed report instead of failing the whole run"


def test_grade_source_recognizes_generic_authoritative_domains():
    from app.services.deep_research.grading import grade_source
    from app.services.deep_research.schemas import SourceRecord, SourceType

    def _src(url: str) -> SourceRecord:
        return SourceRecord(
            source_id="s", url=url, title="t", publisher="", source_type=SourceType.UNKNOWN, content="c" * 100
        )

    # Domain-general authoritative bodies (NOT web3-specific) must outrank a plain commercial blog.
    assert grade_source(_src("https://www.imf.org/en/publications/report"))[0] == "tier1"
    assert grade_source(_src("https://arxiv.org/abs/2601.01234"))[0] in {"tier1", "tier2"}
    assert grade_source(_src("https://randomstartup.example/blog/post"))[0] == "tier3"


def test_aggregate_lane_summaries_backfills_findings_from_sources(tmp_path):
    from app.services.deep_research.orchestrator import _aggregate_lane_summaries
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import EvaluationResult, ResearchLane, ResearchPlan, SourceType

    plan = ResearchPlan(
        question="q",
        mode="industry_research",
        lanes=[ResearchLane(lane_id="market", label="Market Data", goal="size it")],
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.add_source(
        url="https://x.example/1",
        title="RWA Market Report",
        publisher="x.example",
        source_type=SourceType.DATASET,
        content="RWA tokenization reached 35% growth in 2026. Further detail on custody follows.",
        lane_id="market",
    )
    evaluation = EvaluationResult(quality_gates={}, next_queries=[])

    # Worker path passes an empty source_notes map (P2 removed per-source summaries).
    summaries = _aggregate_lane_summaries(
        plan=plan, ledger=ledger, source_notes_by_id={}, evaluation=evaluation, round_index=1
    )

    assert summaries
    assert summaries[0]["key_findings"], "lane findings must be backfilled from sources when no notes exist (RC7)"


@pytest.mark.asyncio
async def test_devils_advocate_retries_when_first_response_is_not_json():
    import uuid

    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner
    from app.services.deep_research.schemas import ResearchPlan, ResearchRequest

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    calls: list[str] = []

    async def fake_invoke(title, content, *, mode=None, role=None):
        calls.append(content)
        if len(calls) == 1:
            return "Here is my critique in prose, not JSON: the evidence looks selective."
        return '{"strongest_counter_argument": "selection bias", "cherry_picking": ["only bullish sources"]}'

    reasoner._invoke = fake_invoke  # type: ignore[assignment]

    class _MiniLedger:
        sources: dict = {}
        claims: list = []

    plan = ResearchPlan(question="q", mode="industry_research", lanes=[])
    review = await reasoner.devils_advocate_review(
        ResearchRequest(question="q"), plan, _MiniLedger(), worker_results=[]
    )

    assert len(calls) == 2, "DA must retry once when the first response is not parseable JSON (RC5)"
    assert review and review["strongest_counter_argument"] == "selection bias"
