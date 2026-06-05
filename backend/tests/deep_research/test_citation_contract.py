from __future__ import annotations




def test_ledger_preserves_provided_source_id(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    rec = ledger.add_source(
        url="https://x.example/a",
        title="t",
        publisher="x.example",
        source_type=SourceType.PRIMARY,
        content="body",
        source_id="src_fixedid1234",
    )
    assert rec.source_id == "src_fixedid1234"


def test_ledger_mints_new_id_on_collision(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    first = ledger.add_source(
        url="https://x.example/a",
        title="t",
        publisher="x.example",
        source_type=SourceType.PRIMARY,
        content="body",
        source_id="src_dupe12345678",
    )
    second = ledger.add_source(
        url="https://x.example/b",
        title="t2",
        publisher="x.example",
        source_type=SourceType.PRIMARY,
        content="body2",
        source_id="src_dupe12345678",
    )
    assert first.source_id == "src_dupe12345678"
    assert second.source_id != first.source_id
    assert second.source_id.startswith("src_")


_WORKER_IDS = {"lane alpha": "src_wfalpha01", "lane beta": "src_wfbeta0002"}


def _passing_audit_report(source_ids: list[str]) -> str:
    a = source_ids[0]
    b = source_ids[1] if len(source_ids) > 1 else source_ids[0]
    return f"""# Citation-stable Audit

## Executive Thesis

Integrated evidence over the fetched sources from 2024, 2025, and 2026 supports a 35% growth
claim across 12 jurisdictions with 18 transfer-control checks and 7 reporting checkpoints in
2026. Converging sources rather than one bullish source carry the thesis. Sources: {a}, {b}.

## Method And Source Standard

Primary disclosures outrank secondary commentary; every id in the prose resolves to a fetched
page in the ledger, and weak sources cannot solely support a key claim.

## Findings

- The 35% growth claim holds across 12 jurisdictions in 2026, corroborated not cherry-picked.
  Sources: {a}, {b}.
- The workflow exposes 18 transfer-control checks and 7 reporting checkpoints, more complex
  than a simple token sale. Sources: {b}.

## Source Ledger

- `{a}` issuer disclosure
- `{b}` regulator filing
"""


class _StableIdRunner:
    async def run(self, topic: str, *, request, cancel_event=None):
        from app.services.deep_research.schemas import SourceRecord, SourceType, WorkerResult

        sid = _WORKER_IDS[topic]
        return WorkerResult(
            topic=topic,
            intermediate_report="Digest: integrated evidence, 35% growth, 18 controls in 2026.",
            sources=[
                SourceRecord(
                    source_id=sid,
                    url=f"https://{sid}.example/page",
                    title="Worker source",
                    publisher=f"{sid}.example",
                    source_type=SourceType.PRIMARY,
                    content="Title: stable\nIssuer discloses 35% growth, 12 jurisdictions, 18 controls in 2026.",
                    fetch_tool="web_fetch",
                )
            ],
            status="ok",
        )


class _CiteReasoner:
    async def refine_plan(self, request, plan):
        return plan

    async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results, **_kw):
        return _passing_audit_report(list(ledger.sources))


