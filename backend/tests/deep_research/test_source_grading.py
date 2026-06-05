from __future__ import annotations

import json



def _record(source_type, *, url="https://x.example/a", publisher=""):
    from app.services.deep_research.schemas import SourceRecord

    return SourceRecord(
        source_id="src_test",
        url=url,
        title="t",
        publisher=publisher,
        source_type=source_type,
        content="c",
    )


def test_grade_source_maps_types_and_domains():
    from app.services.deep_research.grading import grade_source
    from app.services.deep_research.schemas import SourceType

    assert grade_source(_record(SourceType.PRIMARY)) == ("tier1", "A")
    assert grade_source(_record(SourceType.REGULATORY)) == ("tier1", "A")
    assert grade_source(_record(SourceType.DATASET)) == ("tier2", "B")
    assert grade_source(_record(SourceType.TECHNICAL)) == ("tier2", "B")
    assert grade_source(_record(SourceType.SECONDARY)) == ("tier3", "C")
    # UNKNOWN → domain heuristics
    assert grade_source(_record(SourceType.UNKNOWN, publisher="sec.gov")) == ("tier1", "B")
    assert grade_source(_record(SourceType.UNKNOWN, url="https://medium.com/@x/post")) == ("tier4", "D")
    assert grade_source(_record(SourceType.UNKNOWN, publisher="randomnews.example")) == ("tier3", "C")


def test_ledger_add_source_attaches_grade(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    rec = ledger.add_source(
        url="https://regulator.example/filing",
        title="Filing",
        publisher="regulator.example",
        source_type=SourceType.REGULATORY,
        content="some content",
    )
    assert rec.evidence_tier == "tier1"
    assert rec.evidence_grade == "A"
    line = (tmp_path / "sources.jsonl").read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    assert payload["evidence_tier"] == "tier1"
    assert payload["evidence_grade"] == "A"


def test_synthesis_instruction_weights_by_tier():
    from app.services.deep_research.synthesis_gates import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    text = build_digest_synthesis_instruction(ResearchRequest(question="Research X"), "English")
    assert "evidence_tier" in text
    assert "tier4" in text
    assert "sole support" in text


def _passing_audit_report(source_ids: list[str]) -> str:
    return f"""# V2 Worker Audit

## Executive Thesis

The audit integrates 3 fetched sources across 2024, 2025, and 2026 covering custody,
transfer controls, and reporting. Converging evidence from {source_ids[0]} and
{source_ids[1]} establishes the 35% growth claim, while {source_ids[2]} adds 18 control
checks. Confidence is moderate over 12 jurisdictions.

## Method And Source Standard

Primary disclosures outrank secondary commentary; tier1 sources carry the argument and
search snippets were never cited as evidence. Every id resolves to the ledger.

## Findings

- The growth claim holds at 35% across 12 jurisdictions, corroborated rather than
  cherry-picked. Sources: {source_ids[0]}, {source_ids[1]}.
- The workflow has 18 transfer-control checks and 7 reporting checkpoints in 2026.
  Sources: {source_ids[2]}, {source_ids[0]}.

## Source Ledger

- `{source_ids[0]}` issuer
- `{source_ids[1]}` regulator
- `{source_ids[2]}` platform
"""


class _NoPerSourceReasoner:
    def __init__(self):
        self.extract_claims_called = False
        self.summarize_source_called = False
        self.worker_results_seen = None

    async def refine_plan(self, request, plan):
        return plan

    async def extract_claims(self, request, source):
        self.extract_claims_called = True
        return []

    async def summarize_source(self, request, source):
        self.summarize_source_called = True
        return {}

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results):
        self.worker_results_seen = worker_results
        return _passing_audit_report(list(ledger.sources))


class _SimpleWorkerRunner:
    async def run(self, topic: str, *, request, cancel_event=None):
        from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

        idx = abs(hash(topic)) % 1000
        return WorkerResult(
            topic=topic,
            intermediate_report=f"Digest for {topic}: integrated source-bound evidence with 35% and 18 controls.",
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


