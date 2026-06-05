from __future__ import annotations




def _passing_audit_report(source_ids: list[str]) -> str:
    return f"""# Worker Audit With Adversarial Review

## Executive Thesis

Integrated evidence across 3 sources in 2024-2026 supports custody, transfer-control,
and reporting claims. Converging signals from {source_ids[0]} and {source_ids[1]} put
growth at 35% over 12 jurisdictions; {source_ids[2]} adds 18 control checks.

## Method And Source Standard

Primary disclosures outrank secondary commentary; 4 ledger ids resolve to fetched pages.

## Findings

- The 35% growth claim is corroborated, not cherry-picked, across 2 independent sources.
  Sources: {source_ids[0]}, {source_ids[1]}.
- The workflow exposes 18 transfer-control checks and 7 reporting checkpoints in 2026.
  Sources: {source_ids[2]}, {source_ids[0]}.

## Contradictions And Gaps

- The strongest counter-argument is that the 35% figure may be self-reported; an
  independent audit is still missing. Sources: {source_ids[1]}.

## Source Ledger

- `{source_ids[0]}` issuer
- `{source_ids[1]}` regulator
- `{source_ids[2]}` platform
"""


class _DAReasoner:
    def __init__(self):
        self.da_called = False
        self.devils_advocate_seen = None

    async def refine_plan(self, request, plan):
        return plan

    async def devils_advocate_review(self, request, plan, ledger, *, worker_results, lane_summaries=None):
        self.da_called = True
        return {
            "cherry_picking": ["only bullish issuer sources cited"],
            "alternative_explanations": ["growth could be market-wide, not platform-specific"],
            "strongest_counter_argument": "The 35% growth figure may be self-reported and unaudited.",
            "whats_missing": ["independent audit of the 35% figure"],
            "so_what": "Material — the figure anchors the thesis.",
        }

    async def synthesize_from_digests(
        self,
        request,
        plan,
        ledger,
        evaluation,
        *,
        worker_results,
        source_notes=None,
        lane_summaries=None,
        devils_advocate=None,
    ):
        self.devils_advocate_seen = devils_advocate
        return _passing_audit_report(list(ledger.sources))


class _NoDAReasoner:
    """Reasoner without a devils_advocate_review method — DA must be skipped gracefully."""

    async def refine_plan(self, request, plan):
        return plan

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results):
        return _passing_audit_report(list(ledger.sources))


class _SimpleWorkerRunner:
    async def run(self, topic: str, *, request, cancel_event=None):
        from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

        idx = abs(hash(topic)) % 1000
        return WorkerResult(
            topic=topic,
            intermediate_report=f"Digest for {topic}: integrated evidence, 35% growth, 18 controls in 2026.",
            sources=[
                SourceRecord(
                    source_id="",
                    url=f"https://worker{idx}.example/s",
                    title=f"Worker source {idx}",
                    publisher=f"worker{idx}.example",
                    source_type=SourceType.PRIMARY,
                    content=f"Title: src {idx}\nIssuer discloses 35% growth, 12 jurisdictions, 18 controls in 2026.",
                    fetch_tool="web_fetch",
                )
            ],
            status="ok",
        )


async def _no_linear(tool_name: str, arguments: dict) -> str:
    raise AssertionError(f"linear path must not run: {tool_name}")


def _request():
    from app.services.deep_research.schemas import ResearchRequest

    return ResearchRequest(
        question="Audit custody claims with workers.",
        mode="source_ledger_audit",
        depth="quick",
        max_rounds=1,
        max_sources=3,
        concurrency=3,
    )


