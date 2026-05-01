from __future__ import annotations

import pytest

from app.finance_analysis.workflow_runner import FinanceWorkflowRunner
from app.finance_data.connectors.static_public import StaticPublicFinanceConnector
from app.finance_data.schemas import MarketRegion
from app.finance_data.service import FinanceDataService


def test_secondary_equity_workflow_produces_artifacts_and_quality_gates() -> None:
    runner = FinanceWorkflowRunner(FinanceDataService(connectors=[StaticPublicFinanceConnector()]))

    result = runner.run_workflow(
        workflow="secondary-equity-deep-dive",
        query="AAPL",
        region=MarketRegion.US,
        assumptions={
            "discount_rate": 0.10,
            "terminal_growth_rate": 0.03,
            "net_debt": 50,
            "shares_outstanding": 25,
        },
        peer_set=["MSFT", "GOOGL"],
    )

    assert result.workflow == "secondary-equity-deep-dive"
    assert result.entity_id == "entity:us:aapl"
    assert result.quality_gates["source_ledger_complete"] == "passed"
    assert result.quality_gates["valuation_recomputable"] == "passed"
    assert result.analysis_results[0]["analysis_type"] == "valuation"
    assert result.artifacts["memo_markdown"].startswith("# Apple Inc. - Secondary Equity Deep Dive")
    assert "DCF" in result.artifacts["memo_markdown"]
    assert result.artifacts["source_ledger_json"]["records"]


def test_primary_and_ipo_workflows_are_executable_without_live_paid_sources() -> None:
    runner = FinanceWorkflowRunner(FinanceDataService(connectors=[StaticPublicFinanceConnector()]))

    primary = runner.run_workflow(
        workflow="primary-market-due-diligence",
        query="AAPL",
        region=MarketRegion.US,
    )
    ipo = runner.run_workflow(
        workflow="ipo-pipeline-monitor",
        region=MarketRegion.HK,
    )

    assert primary.quality_gates["kyc_checked"] == "passed"
    assert "funding_rounds" in primary.artifacts

    assert ipo.entity_id is None
    assert ipo.quality_gates["market_region_covered"] == "passed"
    assert ipo.artifacts["ipo_pipeline"][0]["market"] == "hk"


def test_unknown_finance_workflow_fails_loudly() -> None:
    runner = FinanceWorkflowRunner(FinanceDataService(connectors=[StaticPublicFinanceConnector()]))

    with pytest.raises(KeyError, match="Unknown finance workflow"):
        runner.run_workflow(workflow="unknown-workflow", query="AAPL")
