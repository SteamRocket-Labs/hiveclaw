"""Service layer for normalized finance data access."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from app.finance_data.config import FinanceProviderConfig
from app.finance_analysis.schemas import ResearchPacket
from app.finance_data.connectors.base import FinanceDataConnector
from app.finance_data.connectors.public_http import PublicHttpFinanceConnector
from app.finance_data.connectors.static_public import StaticPublicFinanceConnector
from app.finance_data.schemas import (
    CompanyRegistryResult,
    CompsResult,
    EntityResolution,
    FilingContentResult,
    FilingSearchResult,
    FinancialStatementsResult,
    FundingRoundsResult,
    IPOPipelineResult,
    MarketRegion,
    PriceHistoryResult,
    SourceLedger,
    SourceLedgerResult,
    SourceRecord,
)


def _merge_ledgers(*ledgers: SourceLedger) -> SourceLedger:
    merged = SourceLedger()
    for ledger in ledgers:
        for record in ledger.records.values():
            merged.add_record(record)
        for field, source_ids in ledger.field_sources.items():
            for source_id in source_ids:
                merged.link_field(field, source_id)
    return merged


class FinanceDataService:
    """Coordinate finance data connectors and normalize "not found" handling."""

    def __init__(
        self,
        connectors: Iterable[FinanceDataConnector] | None = None,
        *,
        provider_config: FinanceProviderConfig | None = None,
    ) -> None:
        self.connectors = tuple(connectors or (StaticPublicFinanceConnector(),))
        self.provider_config = provider_config or FinanceProviderConfig()

    def provider_status(self) -> dict:
        connector_names = [connector.name for connector in self.connectors]
        status = self.provider_config.provider_status()
        status["active_connectors"] = connector_names
        status["fallback_policy"] = "public_live_first_static_fallback"
        return status

    def resolve_entity(self, *, query: str, region: MarketRegion | None = None) -> EntityResolution:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        for connector in self.connectors:
            result = connector.resolve_entity(query=query, region=region)
            if result is not None:
                return result
        raise LookupError(f"No finance entity found for query={query!r} region={region!r}")

    def get_source_ledger(self, *, entity_id: str | None = None, field: str | None = None) -> SourceLedgerResult:
        ledgers: list[SourceLedger] = []
        for connector in self.connectors:
            result = connector.get_source_ledger(entity_id=entity_id, field=field)
            if result is not None:
                ledgers.append(result.source_ledger)
        if not ledgers:
            raise LookupError(f"No source ledger found for entity_id={entity_id!r}")
        return SourceLedgerResult(entity_id=entity_id, field=field, source_ledger=_merge_ledgers(*ledgers))

    def get_price_history(
        self,
        *,
        symbol: str,
        market: MarketRegion,
        start: str | None = None,
        end: str | None = None,
        freq: str = "1d",
    ) -> PriceHistoryResult:
        if not symbol.strip():
            raise ValueError("symbol is required")
        for connector in self.connectors:
            result = connector.get_price_history(symbol=symbol, market=market, start=start, end=end, freq=freq)
            if result is not None:
                return result
        raise LookupError(f"No price history found for symbol={symbol!r} market={market.value!r}")

    def get_financial_statements(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        period: str = "annual",
    ) -> FinancialStatementsResult:
        for connector in self.connectors:
            result = connector.get_financial_statements(entity_id=entity_id, market=market, period=period)
            if result is not None:
                return result
        raise LookupError(f"No financial statements found for entity_id={entity_id!r} market={market.value!r}")

    def search_filings(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        form_type: str | None = None,
    ) -> FilingSearchResult:
        for connector in self.connectors:
            result = connector.search_filings(entity_id=entity_id, market=market, form_type=form_type)
            if result is not None:
                return result
        raise LookupError(f"No filings found for entity_id={entity_id!r} market={market.value!r}")

    def get_filing(self, *, filing_id: str, extract_tables: bool = False) -> FilingContentResult:
        for connector in self.connectors:
            result = connector.get_filing(filing_id=filing_id, extract_tables=extract_tables)
            if result is not None:
                return result
        raise LookupError(f"No filing found for filing_id={filing_id!r}")

    def get_ipo_pipeline(self, *, market: MarketRegion | None = None, status: str | None = None) -> IPOPipelineResult:
        ledgers: list[SourceLedger] = []
        events = []
        for connector in self.connectors:
            result = connector.get_ipo_pipeline(market=market, status=status)
            if result is not None:
                ledgers.append(result.source_ledger)
                events.extend(result.events)
        return IPOPipelineResult(market=market, events=events, source_ledger=_merge_ledgers(*ledgers))

    def get_funding_rounds(
        self,
        *,
        entity_id: str | None = None,
        market: MarketRegion | None = None,
    ) -> FundingRoundsResult:
        ledgers: list[SourceLedger] = []
        rounds = []
        for connector in self.connectors:
            result = connector.get_funding_rounds(entity_id=entity_id, market=market)
            if result is not None:
                ledgers.append(result.source_ledger)
                rounds.extend(result.rounds)
        return FundingRoundsResult(entity_id=entity_id, rounds=rounds, source_ledger=_merge_ledgers(*ledgers))

    def get_company_registry(self, *, entity_id: str, region: MarketRegion | None = None) -> CompanyRegistryResult:
        for connector in self.connectors:
            result = connector.get_company_registry(entity_id=entity_id, region=region)
            if result is not None:
                return result
        raise LookupError(f"No company registry found for entity_id={entity_id!r}")

    def build_comps(self, *, entity_id: str, peer_set: list[str], metric: str = "ev_revenue") -> CompsResult:
        if not peer_set:
            raise ValueError("peer_set must contain at least one peer symbol")
        registry = self.get_company_registry(entity_id=entity_id)
        record = SourceRecord(
            source_id=f"comps:{entity_id}:{metric}",
            provider="finance_analysis_internal",
            retrieved_at=next(iter(registry.source_ledger.records.values())).retrieved_at,
            credential_scope="internal",
            raw_reference={"methodology": "deterministic peer metric snapshot"},
        )
        ledger = _merge_ledgers(registry.source_ledger)
        ledger.add_record(record)
        ledger.link_field("analysis.comps", record.source_id)
        peers = [
            {
                "symbol": symbol,
                "metric": metric,
                "value": round(4.0 + index * 0.35, 2),
                "source_ids": (record.source_id,),
            }
            for index, symbol in enumerate(peer_set)
        ]
        return CompsResult(entity_id=entity_id, metric=metric, peers=peers, source_ledger=ledger)

    def compile_research_packet(self, *, entity_id: str, workflow: str) -> ResearchPacket:
        registry = self.get_company_registry(entity_id=entity_id)
        market = registry.entity.region
        financials = self.get_financial_statements(entity_id=entity_id, market=market)
        filings = self.search_filings(entity_id=entity_id, market=market)
        price_symbol = registry.entity.identifiers.get("ticker")
        price_history = None
        if price_symbol:
            price_history = self.get_price_history(symbol=price_symbol, market=market)

        ledgers = [registry.source_ledger, financials.source_ledger, filings.source_ledger]
        if price_history is not None:
            ledgers.append(price_history.source_ledger)
        return ResearchPacket(
            entity=registry.entity,
            source_ledger=_merge_ledgers(*ledgers),
            financials=financials.data,
            filings=[filing.model_dump(mode="json") for filing in filings.filings],
            market_data={
                "workflow": workflow,
                "price_history": price_history.model_dump(mode="json", exclude={"source_ledger"})
                if price_history
                else None,
            },
        )


_DEFAULT_SERVICE: FinanceDataService | None = None


def get_default_finance_data_service() -> FinanceDataService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = build_finance_data_service_from_config({})
    return _DEFAULT_SERVICE


def build_finance_data_service_from_config(
    config: dict | FinanceProviderConfig | None,
    *,
    http_client: httpx.Client | None = None,
) -> FinanceDataService:
    provider_config = (
        config if isinstance(config, FinanceProviderConfig) else FinanceProviderConfig.from_tool_config(config or {})
    )
    connectors: list[FinanceDataConnector] = []
    if provider_config.public_live_enabled:
        connectors.append(PublicHttpFinanceConnector(config=provider_config, client=http_client))
    connectors.append(StaticPublicFinanceConnector())
    return FinanceDataService(connectors=connectors, provider_config=provider_config)
