from __future__ import annotations

from app.finance_data.connectors.static_public import StaticPublicFinanceConnector
from app.finance_data.schemas import MarketRegion
from app.finance_data.service import FinanceDataService


def test_finance_data_service_covers_us_hk_and_a_share_entities_with_sources() -> None:
    service = FinanceDataService(connectors=[StaticPublicFinanceConnector()])

    us = service.resolve_entity(query="AAPL", region=MarketRegion.US)
    hk = service.resolve_entity(query="00700", region=MarketRegion.HK)
    cn = service.resolve_entity(query="600519", region=MarketRegion.CN_A)

    assert us.entity.entity_id == "entity:us:aapl"
    assert us.entity.identifiers["ticker"] == "AAPL"
    assert us.source_ledger.is_verified("entity.identifiers.ticker")

    assert hk.entity.region == MarketRegion.HK
    assert hk.entity.identifiers["ticker"] == "00700.HK"
    assert hk.source_ledger.is_verified("entity.region")

    assert cn.entity.region == MarketRegion.CN_A
    assert cn.entity.identifiers["ticker"] == "600519.SS"
    assert cn.source_ledger.is_verified("entity.name")


def test_finance_data_service_returns_prices_financials_filings_and_research_packet() -> None:
    service = FinanceDataService(connectors=[StaticPublicFinanceConnector()])
    resolved = service.resolve_entity(query="AAPL", region=MarketRegion.US)

    prices = service.get_price_history(
        symbol="AAPL",
        market=MarketRegion.US,
        start="2026-01-01",
        end="2026-01-05",
        freq="1d",
    )
    financials = service.get_financial_statements(
        entity_id=resolved.entity.entity_id,
        market=MarketRegion.US,
        period="annual",
    )
    filings = service.search_filings(
        entity_id=resolved.entity.entity_id,
        market=MarketRegion.US,
        form_type="10-K",
    )
    filing = service.get_filing(filing_id=filings.filings[0].filing_id, extract_tables=True)
    packet = service.compile_research_packet(
        entity_id=resolved.entity.entity_id,
        workflow="secondary-equity-deep-dive",
    )

    assert prices.rows[0]["symbol"] == "AAPL"
    assert prices.source_ledger.is_verified("prices.AAPL")

    assert financials.data["free_cash_flow"] == [100.0, 110.0, 121.0]
    assert financials.source_ledger.is_verified("financials.free_cash_flow")

    assert filings.filings[0].form_type == "10-K"
    assert filings.source_ledger.is_verified("filings.entity:us:aapl")

    assert filing.content["tables"]["income_statement"][0]["revenue"] > 0
    assert filing.source_ledger.is_verified("filing.filing:sec:aapl:2025-10k")

    assert packet.entity.entity_id == "entity:us:aapl"
    assert packet.financials["free_cash_flow"] == [100.0, 110.0, 121.0]
    assert packet.filings[0]["form_type"] == "10-K"
    assert packet.source_ledger.all_source_ids()
