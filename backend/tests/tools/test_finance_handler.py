from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_finance_handlers_return_json_with_source_ledger() -> None:
    from app.tools.handlers.finance import (
        finance_compile_research_packet,
        finance_get_financial_statements,
        finance_get_price_history,
        finance_resolve_entity,
        finance_search_filings,
    )

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
