from __future__ import annotations

import json


def test_claim_without_source_is_forced_to_unsupported(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import ClaimStatus, SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://rwa.example/report",
        title="RWA market report",
        publisher="RWA Example",
        source_type=SourceType.PRIMARY,
        content="Tokenized treasury funds grew in 2026.",
    )

    verified = ledger.add_claim(
        text="Tokenized treasury funds grew in 2026.",
        status=ClaimStatus.VERIFIED,
        source_ids=[source.source_id],
        evidence="Directly stated in fetched source text.",
    )
    unsupported = ledger.add_claim(
        text="The whole RWA market will triple next month.",
        status=ClaimStatus.VERIFIED,
        source_ids=["missing-source"],
        evidence="No fetched source supports this claim.",
    )

    assert verified.status == ClaimStatus.VERIFIED
    assert unsupported.status == ClaimStatus.UNSUPPORTED
    assert unsupported.source_ids == []

    claims = [json.loads(line) for line in (tmp_path / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
    assert claims[-1]["status"] == "unsupported"


def test_ledger_records_contradictions_without_upgrading_claim_status(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import ClaimStatus, SourceType

    ledger = EvidenceLedger(tmp_path)
    first = ledger.add_source(
        url="https://issuer.example/a",
        title="Issuer update",
        publisher="Issuer",
        source_type=SourceType.PRIMARY,
        content="Protocol A launched in May 2026.",
    )
    second = ledger.add_source(
        url="https://registry.example/a",
        title="Registry update",
        publisher="Registry",
        source_type=SourceType.PRIMARY,
        content="Protocol A launch is pending.",
    )

    claim = ledger.add_claim(
        text="Protocol A has launched.",
        status=ClaimStatus.CONTRADICTED,
        source_ids=[first.source_id, second.source_id],
        evidence="Issuer and registry disagree.",
        contradiction_group="protocol-a-launch",
    )

    assert claim.status == ClaimStatus.CONTRADICTED
    assert claim.contradiction_group == "protocol-a-launch"
    assert ledger.summary()["contradicted_claims"] == 1
