"""Deterministic public finance connector used as the default cloud baseline.

This connector is intentionally small and offline-safe. It gives the runtime a
real normalized data path, source-ledger semantics, and US/HK/A-share coverage
while paid or network-backed providers remain tenant-scoped optional adapters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.finance_data.schemas import (
    CompanyRegistryResult,
    EntityMasterRecord,
    EntityResolution,
    EntityType,
    FilingContentResult,
    FilingRecord,
    FilingSearchResult,
    FinancialStatementsResult,
    FundingRound,
    FundingRoundsResult,
    IPOEvent,
    IPOPipelineResult,
    MarketRegion,
    PriceHistoryResult,
    SourceLedger,
    SourceLedgerResult,
    SourceRecord,
)


_NOW = datetime(2026, 5, 2, tzinfo=UTC)


def _record(source_id: str, provider: str, url: str | None = None, *, filing_id: str | None = None) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        provider=provider,
        url=url,
        filing_id=filing_id,
        retrieved_at=_NOW,
        credential_scope="public",
        license="public_or_exchange_terms",
    )


def _ledger(record: SourceRecord, fields: list[str]) -> SourceLedger:
    ledger = SourceLedger()
    ledger.add_record(record)
    for field in fields:
        ledger.link_field(field, record.source_id)
    return ledger


def _merge_ledgers(*ledgers: SourceLedger) -> SourceLedger:
    merged = SourceLedger()
    for ledger in ledgers:
        for record in ledger.records.values():
            merged.add_record(record)
        for field, source_ids in ledger.field_sources.items():
            for source_id in source_ids:
                merged.link_field(field, source_id)
    return merged


class StaticPublicFinanceConnector:
    """Small public baseline connector with deterministic fixtures."""

    name = "static_public"

    def __init__(self) -> None:
        self._entities = self._build_entities()
        self._entity_aliases = self._build_aliases()
        self._financials = self._build_financials()
        self._price_rows = self._build_prices()
        self._filings = self._build_filings()
        self._filing_content = self._build_filing_content()
        self._funding_rounds = self._build_funding_rounds()
        self._ipo_events = self._build_ipo_events()

    def resolve_entity(self, *, query: str, region: MarketRegion | None = None) -> EntityResolution | None:
        normalized = self._normalize_query(query)
        entity_id = self._entity_aliases.get(normalized)
        if entity_id is None:
            return None

        entity = self._entities[entity_id]
        if region is not None and entity.region != region:
            return None

        record = _record(
            f"entity:{entity.region.value}:{entity.identifiers.get('ticker', entity.entity_id)}",
            self._registry_provider(entity.region),
            self._registry_url(entity),
        )
        return EntityResolution(
            entity=entity.model_copy(update={"source_ids": (record.source_id,)}),
            source_ledger=_ledger(
                record,
                [
                    "entity.entity_id",
                    "entity.name",
                    "entity.region",
                    "entity.identifiers.ticker",
                ],
            ),
        )

    def get_source_ledger(self, *, entity_id: str | None = None, field: str | None = None) -> SourceLedgerResult | None:
        ledgers: list[SourceLedger] = []
        if entity_id:
            entity = self._entities.get(entity_id)
            if entity is None:
                return None
            ledgers.append(self.get_company_registry(entity_id=entity_id, region=entity.region).source_ledger)  # type: ignore[union-attr]
            financials = self.get_financial_statements(entity_id=entity_id, market=entity.region)
            if financials:
                ledgers.append(financials.source_ledger)
            filings = self.search_filings(entity_id=entity_id, market=entity.region)
            if filings:
                ledgers.append(filings.source_ledger)
        else:
            for entity in self._entities.values():
                registry = self.get_company_registry(entity_id=entity.entity_id, region=entity.region)
                if registry:
                    ledgers.append(registry.source_ledger)

        ledger = _merge_ledgers(*ledgers)
        if field:
            filtered = SourceLedger()
            for source_id in ledger.field_sources.get(field, ()):
                if source_id in ledger.records:
                    filtered.add_record(ledger.records[source_id])
                    filtered.link_field(field, source_id)
            ledger = filtered
        return SourceLedgerResult(entity_id=entity_id, field=field, source_ledger=ledger)

    def get_price_history(
        self,
        *,
        symbol: str,
        market: MarketRegion,
        start: str | None = None,
        end: str | None = None,
        freq: str = "1d",
    ) -> PriceHistoryResult | None:
        canonical_symbol = self._canonical_symbol(symbol, market)
        rows = list(self._price_rows.get(canonical_symbol, ()))
        if not rows:
            return None
        if start:
            rows = [row for row in rows if str(row["date"]) >= start]
        if end:
            rows = [row for row in rows if str(row["date"]) <= end]

        record = _record(
            f"prices:{canonical_symbol}", self._market_data_provider(market), self._market_data_url(canonical_symbol)
        )
        return PriceHistoryResult(
            symbol=canonical_symbol,
            market=market,
            freq=freq,
            rows=rows,
            source_ledger=_ledger(record, [f"prices.{canonical_symbol}", "market_data.price_history"]),
        )

    def get_financial_statements(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        period: str = "annual",
    ) -> FinancialStatementsResult | None:
        data = self._financials.get(entity_id)
        if data is None:
            return None
        entity = self._entities.get(entity_id)
        if entity is None or entity.region != market:
            return None

        record = _record(
            f"financials:{entity_id}:2025",
            self._filing_provider(market),
            self._financials_url(entity),
        )
        return FinancialStatementsResult(
            entity_id=entity_id,
            market=market,
            period=period,
            data=dict(data),
            source_ledger=_ledger(
                record,
                [
                    "financials.revenue",
                    "financials.operating_income",
                    "financials.free_cash_flow",
                ],
            ),
        )

    def search_filings(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        form_type: str | None = None,
    ) -> FilingSearchResult | None:
        filings = [
            filing
            for filing in self._filings.values()
            if filing.entity_id == entity_id
            and filing.market == market
            and (form_type is None or filing.form_type == form_type)
        ]
        if not filings:
            return None
        record = _record(f"filings:{entity_id}", self._filing_provider(market), self._filings_url(market))
        return FilingSearchResult(
            entity_id=entity_id,
            market=market,
            filings=filings,
            source_ledger=_ledger(record, [f"filings.{entity_id}", "filings.search"]),
        )

    def get_filing(self, *, filing_id: str, extract_tables: bool = False) -> FilingContentResult | None:
        filing = self._filings.get(filing_id)
        content = self._filing_content.get(filing_id)
        if filing is None or content is None:
            return None

        record = _record(filing.source_id, self._filing_provider(filing.market), filing.url, filing_id=filing_id)
        payload = dict(content)
        if not extract_tables:
            payload.pop("tables", None)
        return FilingContentResult(
            filing_id=filing_id,
            content=payload,
            source_ledger=_ledger(record, [f"filing.{filing_id}", "filing.content"]),
        )

    def get_ipo_pipeline(
        self,
        *,
        market: MarketRegion | None = None,
        status: str | None = None,
    ) -> IPOPipelineResult | None:
        events = [
            event
            for event in self._ipo_events
            if (market is None or event.market == market) and (status is None or event.status == status)
        ]
        record = _record("ipo:pipeline:public", "exchange_disclosure", "https://www.hkexnews.hk/")
        return IPOPipelineResult(
            market=market,
            events=events,
            source_ledger=_ledger(record, ["ipo.pipeline", "primary_market.ipo_pipeline"]),
        )

    def get_funding_rounds(
        self,
        *,
        entity_id: str | None = None,
        market: MarketRegion | None = None,
    ) -> FundingRoundsResult | None:
        rounds = [
            item
            for item in self._funding_rounds
            if (entity_id is None or item.entity_id == entity_id)
            and (
                market is None
                or self._entities.get(
                    item.entity_id,
                    EntityMasterRecord(
                        entity_id="unknown",
                        name="unknown",
                        entity_type=EntityType.COMPANY,
                        region=MarketRegion.GLOBAL,
                    ),
                ).region
                == market
            )
        ]
        record = _record(
            "funding:public:sample", "sec_form_d_and_public_disclosure", "https://www.sec.gov/edgar/search/"
        )
        return FundingRoundsResult(
            entity_id=entity_id,
            rounds=rounds,
            source_ledger=_ledger(record, ["funding.rounds", "primary_market.funding_rounds"]),
        )

    def get_company_registry(
        self,
        *,
        entity_id: str,
        region: MarketRegion | None = None,
    ) -> CompanyRegistryResult | None:
        entity = self._entities.get(entity_id)
        if entity is None:
            return None
        if region is not None and entity.region != region:
            return None

        record = _record(
            f"registry:{entity.region.value}:{entity.identifiers.get('ticker', entity_id)}",
            self._registry_provider(entity.region),
            self._registry_url(entity),
        )
        return CompanyRegistryResult(
            entity=entity.model_copy(update={"source_ids": (record.source_id,)}),
            registry={
                "jurisdiction": entity.region.value,
                "identifiers": entity.identifiers,
                "status": "active",
            },
            source_ledger=_ledger(record, ["registry.company", "entity.name", "entity.identifiers.ticker"]),
        )

    def _build_entities(self) -> dict[str, EntityMasterRecord]:
        return {
            "entity:us:aapl": EntityMasterRecord(
                entity_id="entity:us:aapl",
                name="Apple Inc.",
                entity_type=EntityType.COMPANY,
                region=MarketRegion.US,
                identifiers={"ticker": "AAPL", "cik": "0000320193", "exchange": "NASDAQ"},
            ),
            "entity:hk:00700": EntityMasterRecord(
                entity_id="entity:hk:00700",
                name="Tencent Holdings Ltd.",
                entity_type=EntityType.COMPANY,
                region=MarketRegion.HK,
                identifiers={"ticker": "00700.HK", "exchange": "HKEX"},
            ),
            "entity:cn_a:600519": EntityMasterRecord(
                entity_id="entity:cn_a:600519",
                name="Kweichow Moutai Co., Ltd.",
                entity_type=EntityType.COMPANY,
                region=MarketRegion.CN_A,
                identifiers={"ticker": "600519.SS", "exchange": "SSE"},
            ),
        }

    def _build_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for entity_id, entity in self._entities.items():
            aliases[self._normalize_query(entity.name)] = entity_id
            aliases[self._normalize_query(entity.entity_id)] = entity_id
            for value in entity.identifiers.values():
                aliases[self._normalize_query(value)] = entity_id
                aliases[self._normalize_query(value.split(".")[0])] = entity_id
        return aliases

    def _build_financials(self) -> dict[str, dict[str, Any]]:
        return {
            "entity:us:aapl": {
                "currency": "USD",
                "fiscal_years": [2023, 2024, 2025],
                "revenue": [383.3, 391.0, 407.0],
                "operating_income": [114.3, 123.2, 130.0],
                "free_cash_flow": [100.0, 110.0, 121.0],
                "shares_outstanding": 25.0,
                "net_debt": 50.0,
            },
            "entity:hk:00700": {
                "currency": "CNY",
                "fiscal_years": [2023, 2024, 2025],
                "revenue": [609.0, 660.0, 712.0],
                "operating_income": [184.0, 210.0, 235.0],
                "free_cash_flow": [145.0, 153.0, 162.0],
            },
            "entity:cn_a:600519": {
                "currency": "CNY",
                "fiscal_years": [2023, 2024, 2025],
                "revenue": [150.6, 174.0, 198.0],
                "operating_income": [103.5, 121.0, 139.0],
                "free_cash_flow": [85.0, 91.0, 98.0],
            },
        }

    def _build_prices(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "AAPL": [
                {
                    "date": "2026-01-02",
                    "symbol": "AAPL",
                    "open": 187.0,
                    "high": 191.2,
                    "low": 186.5,
                    "close": 190.1,
                    "volume": 50500000,
                },
                {
                    "date": "2026-01-05",
                    "symbol": "AAPL",
                    "open": 190.5,
                    "high": 193.0,
                    "low": 189.8,
                    "close": 192.4,
                    "volume": 47200000,
                },
            ],
            "00700.HK": [
                {
                    "date": "2026-01-02",
                    "symbol": "00700.HK",
                    "open": 315.0,
                    "high": 322.0,
                    "low": 312.0,
                    "close": 320.0,
                    "volume": 23000000,
                },
            ],
            "600519.SS": [
                {
                    "date": "2026-01-02",
                    "symbol": "600519.SS",
                    "open": 1702.0,
                    "high": 1725.0,
                    "low": 1690.0,
                    "close": 1718.0,
                    "volume": 4100000,
                },
            ],
        }

    def _build_filings(self) -> dict[str, FilingRecord]:
        return {
            "filing:sec:aapl:2025-10k": FilingRecord(
                filing_id="filing:sec:aapl:2025-10k",
                entity_id="entity:us:aapl",
                market=MarketRegion.US,
                form_type="10-K",
                filed_at=datetime(2025, 10, 31, tzinfo=UTC),
                url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/aapl-20250927.htm",
                source_id="filing:sec:aapl:2025-10k",
            ),
            "filing:hkex:00700:2025-annual": FilingRecord(
                filing_id="filing:hkex:00700:2025-annual",
                entity_id="entity:hk:00700",
                market=MarketRegion.HK,
                form_type="annual_report",
                filed_at=datetime(2026, 3, 20, tzinfo=UTC),
                url="https://www.hkexnews.hk/",
                source_id="filing:hkex:00700:2025-annual",
            ),
            "filing:sse:600519:2025-annual": FilingRecord(
                filing_id="filing:sse:600519:2025-annual",
                entity_id="entity:cn_a:600519",
                market=MarketRegion.CN_A,
                form_type="annual_report",
                filed_at=datetime(2026, 3, 31, tzinfo=UTC),
                url="https://www.sse.com.cn/",
                source_id="filing:sse:600519:2025-annual",
            ),
        }

    def _build_filing_content(self) -> dict[str, dict[str, Any]]:
        return {
            "filing:sec:aapl:2025-10k": {
                "summary": "Apple Inc. annual report excerpt normalized for analysis.",
                "sections": {"business": "Consumer technology hardware, software, and services."},
                "tables": {
                    "income_statement": [{"fiscal_year": 2025, "revenue": 407.0, "operating_income": 130.0}],
                    "cash_flow": [{"fiscal_year": 2025, "free_cash_flow": 121.0}],
                },
            },
            "filing:hkex:00700:2025-annual": {
                "summary": "Tencent annual report excerpt normalized for analysis.",
                "sections": {"business": "Internet value-added services and fintech."},
                "tables": {"income_statement": [{"fiscal_year": 2025, "revenue": 712.0}]},
            },
            "filing:sse:600519:2025-annual": {
                "summary": "Kweichow Moutai annual report excerpt normalized for analysis.",
                "sections": {"business": "Premium baijiu production and sales."},
                "tables": {"income_statement": [{"fiscal_year": 2025, "revenue": 198.0}]},
            },
        }

    def _build_funding_rounds(self) -> list[FundingRound]:
        return [
            FundingRound(
                round_id="funding:sample:ai-chip-2026",
                entity_id="entity:us:aapl",
                announced_at=datetime(2026, 1, 15, tzinfo=UTC),
                round_type="strategic_investment",
                amount=250.0,
                currency="USD",
                investors=("Apple Inc.",),
                source_ids=("funding:public:sample",),
            )
        ]

    def _build_ipo_events(self) -> list[IPOEvent]:
        return [
            IPOEvent(
                ipo_id="ipo:hk:sample-ai-infra",
                entity_id="entity:hk:sample-ai-infra",
                market=MarketRegion.HK,
                status="filed",
                expected_listing_date=datetime(2026, 9, 30, tzinfo=UTC),
                source_ids=("ipo:pipeline:public",),
            ),
            IPOEvent(
                ipo_id="ipo:cn_a:sample-advanced-manufacturing",
                entity_id="entity:cn_a:sample-advanced-manufacturing",
                market=MarketRegion.CN_A,
                status="accepted",
                expected_listing_date=datetime(2026, 11, 30, tzinfo=UTC),
                source_ids=("ipo:pipeline:public",),
            ),
        ]

    def _canonical_symbol(self, symbol: str, market: MarketRegion) -> str:
        normalized = symbol.strip().upper()
        if market == MarketRegion.HK and normalized.isdigit():
            return f"{normalized.zfill(5)}.HK"
        if market == MarketRegion.CN_A and normalized.isdigit():
            suffix = ".SS" if normalized.startswith("6") else ".SZ"
            return f"{normalized}{suffix}"
        return normalized

    def _normalize_query(self, query: str) -> str:
        return query.strip().casefold().replace(" ", "")

    def _registry_provider(self, region: MarketRegion) -> str:
        return {
            MarketRegion.US: "sec_edgar",
            MarketRegion.HK: "hkex",
            MarketRegion.CN_A: "sse_szse_cninfo",
        }.get(region, "public_registry")

    def _filing_provider(self, region: MarketRegion) -> str:
        return {
            MarketRegion.US: "sec_edgar",
            MarketRegion.HK: "hkexnews",
            MarketRegion.CN_A: "cninfo_exchange_disclosure",
        }.get(region, "public_filings")

    def _market_data_provider(self, region: MarketRegion) -> str:
        return {
            MarketRegion.US: "yfinance_public",
            MarketRegion.HK: "yfinance_hk_public",
            MarketRegion.CN_A: "akshare_cn_public",
        }.get(region, "public_market_data")

    def _registry_url(self, entity: EntityMasterRecord) -> str:
        if entity.region == MarketRegion.US:
            return "https://www.sec.gov/edgar/search/"
        if entity.region == MarketRegion.HK:
            return "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities"
        if entity.region == MarketRegion.CN_A:
            return "https://www.sse.com.cn/assortment/stock/list/share/"
        return "https://www.gleif.org/"

    def _financials_url(self, entity: EntityMasterRecord) -> str:
        if entity.region == MarketRegion.US:
            return "https://www.sec.gov/edgar/search/"
        if entity.region == MarketRegion.HK:
            return "https://www.hkexnews.hk/"
        if entity.region == MarketRegion.CN_A:
            return "https://www.cninfo.com.cn/"
        return "https://example.com/finance"

    def _filings_url(self, market: MarketRegion) -> str:
        return {
            MarketRegion.US: "https://www.sec.gov/edgar/search/",
            MarketRegion.HK: "https://www.hkexnews.hk/",
            MarketRegion.CN_A: "https://www.cninfo.com.cn/",
        }.get(market, "https://example.com/filings")

    def _market_data_url(self, symbol: str) -> str:
        return f"https://finance.yahoo.com/quote/{symbol}"
