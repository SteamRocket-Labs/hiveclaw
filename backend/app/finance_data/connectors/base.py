"""Connector protocol for normalized finance data access."""

from __future__ import annotations

from typing import Protocol

from app.finance_data.schemas import (
    CompanyRegistryResult,
    EntityResolution,
    FilingContentResult,
    FilingSearchResult,
    FinancialStatementsResult,
    FundingRoundsResult,
    IPOPipelineResult,
    MarketRegion,
    PriceHistoryResult,
    SourceLedgerResult,
)


class FinanceDataConnector(Protocol):
    """Read-only connector interface consumed by FinanceDataService."""

    name: str

    def resolve_entity(self, *, query: str, region: MarketRegion | None = None) -> EntityResolution | None: ...

    def get_source_ledger(
        self, *, entity_id: str | None = None, field: str | None = None
    ) -> SourceLedgerResult | None: ...

    def get_price_history(
        self,
        *,
        symbol: str,
        market: MarketRegion,
        start: str | None = None,
        end: str | None = None,
        freq: str = "1d",
    ) -> PriceHistoryResult | None: ...

    def get_financial_statements(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        period: str = "annual",
    ) -> FinancialStatementsResult | None: ...

    def search_filings(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        form_type: str | None = None,
    ) -> FilingSearchResult | None: ...

    def get_filing(self, *, filing_id: str, extract_tables: bool = False) -> FilingContentResult | None: ...

    def get_ipo_pipeline(
        self,
        *,
        market: MarketRegion | None = None,
        status: str | None = None,
    ) -> IPOPipelineResult | None: ...

    def get_funding_rounds(
        self,
        *,
        entity_id: str | None = None,
        market: MarketRegion | None = None,
    ) -> FundingRoundsResult | None: ...

    def get_company_registry(
        self,
        *,
        entity_id: str,
        region: MarketRegion | None = None,
    ) -> CompanyRegistryResult | None: ...
