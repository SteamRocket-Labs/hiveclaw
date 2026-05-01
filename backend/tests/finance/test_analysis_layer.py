from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.finance_analysis.calculators.dcf import compute_dcf
from app.finance_analysis.engine import FinanceAnalysisEngine
from app.finance_analysis.schemas import DcfAssumptions, ResearchPacket
from app.finance_analysis.workflows import workflow_names, workflow_spec
from app.finance_data.schemas import EntityMasterRecord, EntityType, MarketRegion, SourceLedger, SourceRecord


def test_source_ledger_tracks_field_level_sources_and_unverified_fields():
    ledger = SourceLedger()
    ledger.add_record(
        SourceRecord(
            source_id="filing:sec:10k:2025",
            provider="sec_edgar",
            url="https://www.sec.gov/example",
            retrieved_at=datetime(2026, 5, 2, tzinfo=UTC),
            credential_scope="public",
        )
    )
    ledger.link_field("financials.revenue.2025", "filing:sec:10k:2025")

    assert ledger.sources_for_field("financials.revenue.2025")[0].provider == "sec_edgar"
    assert ledger.is_verified("financials.revenue.2025")
    assert not ledger.is_verified("financials.ebitda.2025")
    assert ledger.verification_label("financials.ebitda.2025") == "[UNVERIFIED]"


def test_dcf_calculator_is_deterministic_and_rejects_invalid_terminal_growth():
    assumptions = DcfAssumptions(
        discount_rate=0.10,
        terminal_growth_rate=0.03,
        net_debt=50.0,
        shares_outstanding=25.0,
    )

    result = compute_dcf([100.0, 110.0, 121.0], assumptions)

    assert result.enterprise_value == pytest.approx(1610.39, abs=0.01)
    assert result.equity_value == pytest.approx(1560.39, abs=0.01)
    assert result.per_share_value == pytest.approx(62.42, abs=0.01)
    assert result.calculation_id.startswith("dcf:v1:")

    with pytest.raises(ValueError, match="terminal_growth_rate"):
        compute_dcf([100.0], DcfAssumptions(discount_rate=0.03, terminal_growth_rate=0.03))


def test_finance_analysis_engine_builds_research_packet_and_runs_dcf():
    entity = EntityMasterRecord(
        entity_id="entity:us:aapl",
        name="Apple Inc.",
        entity_type=EntityType.COMPANY,
        region=MarketRegion.US,
        identifiers={"ticker": "AAPL", "cik": "0000320193"},
    )
    ledger = SourceLedger()
    ledger.add_record(
        SourceRecord(
            source_id="filing:sec:10k:2025",
            provider="sec_edgar",
            url="https://www.sec.gov/example",
            retrieved_at=datetime(2026, 5, 2, tzinfo=UTC),
            credential_scope="public",
        )
    )
    ledger.link_field("free_cash_flow.2026", "filing:sec:10k:2025")
    packet = ResearchPacket(entity=entity, source_ledger=ledger, financials={"free_cash_flow": [100, 110, 121]})

    engine = FinanceAnalysisEngine()
    dcf = engine.run_dcf(
        packet,
        DcfAssumptions(discount_rate=0.10, terminal_growth_rate=0.03, net_debt=50, shares_outstanding=25),
    )

    assert dcf.per_share_value == pytest.approx(62.42, abs=0.01)
    bundle = engine.build_ic_memo(packet, [dcf])
    assert bundle.artifact_type == "ic_memo"
    assert bundle.entity_id == "entity:us:aapl"
    assert bundle.source_ids == ("filing:sec:10k:2025",)


def test_finance_workflow_specs_define_subagents_artifacts_and_gates():
    assert {
        "secondary-equity-deep-dive",
        "primary-market-due-diligence",
        "ipo-pipeline-monitor",
        "portfolio-risk-review",
    }.issubset(set(workflow_names()))

    spec = workflow_spec("secondary-equity-deep-dive")

    assert "filing_reader" in spec.subagents
    assert "valuation" in spec.subagents
    assert "source_ledger_complete" in spec.quality_gates
    assert "reports/{entity_id}-deep-dive.md" in spec.artifacts
