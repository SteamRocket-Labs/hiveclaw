from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_finance_handlers_return_json_with_source_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.finance_data.connectors.static_public import StaticPublicFinanceConnector
    from app.finance_data.service import FinanceDataService
    from app.tools.handlers.finance import (
        finance_compile_research_packet,
        finance_get_financial_statements,
        finance_get_price_history,
        finance_resolve_entity,
        finance_search_filings,
    )

    service = FinanceDataService(connectors=[StaticPublicFinanceConnector()])

    async def fake_service(_tool_name: str):
        return service

    monkeypatch.setattr("app.tools.handlers.finance._finance_service", fake_service)

    resolved = json.loads(await finance_resolve_entity({"query": "AAPL", "region": "us"}))
    entity_id = resolved["data"]["entity"]["entity_id"]
    prices = json.loads(
        await finance_get_price_history(
            {
                "symbol": "AAPL",
                "market": "us",
                "start": "2026-01-01",
                "end": "2026-01-05",
            }
        )
    )
    financials = json.loads(
        await finance_get_financial_statements(
            {
                "entity_id": entity_id,
                "market": "us",
                "period": "annual",
            }
        )
    )
    filings = json.loads(
        await finance_search_filings(
            {
                "entity_id": entity_id,
                "market": "us",
                "form_type": "10-K",
            }
        )
    )
    packet = json.loads(
        await finance_compile_research_packet(
            {
                "entity_id": entity_id,
                "workflow": "secondary-equity-deep-dive",
            }
        )
    )

    assert resolved["ok"] is True
    assert resolved["data"]["entity"]["identifiers"]["ticker"] == "AAPL"
    assert resolved["source_ledger"]["field_sources"]["entity.identifiers.ticker"]

    assert prices["data"]["rows"][0]["close"] > 0
    assert prices["source_ledger"]["field_sources"]["prices.AAPL"]

    assert financials["data"]["free_cash_flow"] == [100.0, 110.0, 121.0]
    assert filings["data"]["filings"][0]["form_type"] == "10-K"

    assert packet["data"]["entity"]["entity_id"] == entity_id
    assert packet["data"]["financials"]["free_cash_flow"] == [100.0, 110.0, 121.0]


@pytest.mark.asyncio
async def test_finance_compute_dcf_and_build_comps_are_deterministic() -> None:
    from app.tools.handlers.finance import finance_build_comps, finance_compute_dcf

    dcf = json.loads(
        await finance_compute_dcf(
            {
                "free_cash_flows": [100, 110, 121],
                "assumptions": {
                    "discount_rate": 0.10,
                    "terminal_growth_rate": 0.03,
                    "net_debt": 50,
                    "shares_outstanding": 25,
                },
            }
        )
    )
    comps = json.loads(
        await finance_build_comps(
            {
                "entity_id": "entity:us:aapl",
                "peer_set": ["MSFT", "GOOGL"],
                "metric": "ev_revenue",
            }
        )
    )

    assert dcf["ok"] is True
    assert dcf["data"]["per_share_value"] == pytest.approx(62.42, abs=0.01)
    assert dcf["data"]["calculation_id"].startswith("dcf:v1:")

    assert comps["ok"] is True
    assert comps["data"]["entity_id"] == "entity:us:aapl"
    assert comps["data"]["metric"] == "ev_revenue"
    assert comps["data"]["peers"][0]["symbol"] == "MSFT"


@pytest.mark.asyncio
async def test_finance_provider_status_and_run_workflow_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.finance_data.connectors.static_public import StaticPublicFinanceConnector
    from app.finance_data.service import FinanceDataService
    from app.tools.handlers.finance import finance_get_provider_status, finance_run_workflow

    service = FinanceDataService(connectors=[StaticPublicFinanceConnector()])

    async def fake_service(_tool_name: str):
        return service

    async def fake_config(_tool_name: str):
        return {
            "provider_mode": "tenant_paid",
            "fmp_api_key": "secret-fmp",
            "wind_client_id": "wind-client",
            "wind_client_secret": "secret-wind",
        }

    monkeypatch.setattr("app.tools.handlers.finance._finance_service", fake_service)
    monkeypatch.setattr("app.tools.handlers.finance._finance_tool_config", fake_config)

    status = json.loads(await finance_get_provider_status({}))
    workflow = json.loads(
        await finance_run_workflow(
            {
                "workflow": "secondary-equity-deep-dive",
                "query": "AAPL",
                "region": "us",
                "peer_set": ["MSFT", "GOOGL"],
                "assumptions": {
                    "discount_rate": 0.10,
                    "terminal_growth_rate": 0.03,
                    "net_debt": 50,
                    "shares_outstanding": 25,
                },
            }
        )
    )

    assert status["ok"] is True
    assert status["data"]["provider_mode"] == "tenant_paid"
    assert status["data"]["paid_sources"]["fmp"]["configured"] is True
    assert "secret-fmp" not in json.dumps(status)

    assert workflow["ok"] is True
    assert workflow["data"]["workflow"] == "secondary-equity-deep-dive"
    assert workflow["data"]["quality_gates"]["valuation_recomputable"] == "passed"
    assert workflow["data"]["artifacts"]["memo_markdown"].startswith("# Apple Inc.")
