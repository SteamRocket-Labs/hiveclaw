"""Public HTTP finance connector for live-but-optional data access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.finance_data.config import FinanceProviderConfig
from app.finance_data.schemas import (
    EntityMasterRecord,
    EntityResolution,
    EntityType,
    FilingRecord,
    FilingSearchResult,
    FinancialStatementsResult,
    MarketRegion,
    PriceHistoryResult,
    SourceLedger,
    SourceRecord,
)


def _record(source_id: str, provider: str, url: str | None, *, filing_id: str | None = None) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        provider=provider,
        url=url,
        filing_id=filing_id,
        retrieved_at=datetime.now(UTC),
        license="public_endpoint_terms",
        credential_scope="tenant_tool_config" if provider == "sec_edgar" else "public",
    )


def _ledger(record: SourceRecord, fields: list[str]) -> SourceLedger:
    ledger = SourceLedger()
    ledger.add_record(record)
    for field in fields:
        ledger.link_field(field, record.source_id)
    return ledger


class PublicHttpFinanceConnector:
    """Best-effort public connector.

    Network failures return ``None`` so FinanceDataService can fall back to the
    deterministic static connector. This makes the pack usable offline while
    still taking the live public path when the network/provider cooperates.
    """

    name = "public_http"

    def __init__(
        self,
        config: FinanceProviderConfig | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 6.0,
    ) -> None:
        self.config = config or FinanceProviderConfig()
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def resolve_entity(self, *, query: str, region: MarketRegion | None = None) -> EntityResolution | None:
        if not self.config.public_live_enabled:
            return None
        if region not in (None, MarketRegion.US):
            return None
        entry = self._sec_company_entry(query)
        if not entry:
            return None

        cik = f"{int(entry['cik_str']):010d}"
        ticker = str(entry["ticker"]).upper()
        entity = EntityMasterRecord(
            entity_id=f"entity:us:{ticker.lower()}",
            name=str(entry["title"]),
            entity_type=EntityType.COMPANY,
            region=MarketRegion.US,
            identifiers={"ticker": ticker, "cik": cik, "exchange": "US"},
        )
        source_url = "https://www.sec.gov/files/company_tickers.json"
        record = _record(f"sec:ticker:{ticker}", "sec_edgar", source_url)
        return EntityResolution(
            entity=entity.model_copy(update={"source_ids": (record.source_id,)}),
            source_ledger=_ledger(
                record,
                [
                    "entity.entity_id",
                    "entity.name",
                    "entity.region",
                    "entity.identifiers.ticker",
                    "entity.identifiers.cik",
                ],
            ),
        )

    def get_price_history(
        self,
        *,
        symbol: str,
        market: MarketRegion,
        start: str | None = None,
        end: str | None = None,
        freq: str = "1d",
    ) -> PriceHistoryResult | None:
        if not self.config.public_live_enabled:
            return None
        canonical_symbol = self._canonical_symbol(symbol, market)
        period1, period2 = self._date_range_to_unix(start, end)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{canonical_symbol}"
        params = {
            "period1": str(period1),
            "period2": str(period2),
            "interval": freq,
            "events": "history",
        }
        try:
            response = self.client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        rows = self._parse_yahoo_chart(payload, canonical_symbol)
        if not rows:
            return None
        record = _record(f"yahoo:chart:{canonical_symbol}", "yahoo_chart", str(httpx.URL(url, params=params)))
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
        if not self.config.public_live_enabled or market != MarketRegion.US:
            return None
        resolved = self.resolve_entity(query=entity_id.rsplit(":", 1)[-1], region=MarketRegion.US)
        if resolved is None:
            return None
        cik = resolved.entity.identifiers.get("cik")
        if not cik:
            return None
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            response = self.client.get(url, headers=self._sec_headers())
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        data = self._parse_companyfacts(payload)
        if not data:
            return None
        record = _record(f"sec:companyfacts:{cik}", "sec_edgar", url)
        return FinancialStatementsResult(
            entity_id=resolved.entity.entity_id,
            market=MarketRegion.US,
            period=period,
            data=data,
            source_ledger=_ledger(
                record,
                ["financials.revenue", "financials.operating_income", "financials.free_cash_flow"],
            ),
        )

    def search_filings(
        self,
        *,
        entity_id: str,
        market: MarketRegion,
        form_type: str | None = None,
    ) -> FilingSearchResult | None:
        if not self.config.public_live_enabled or market != MarketRegion.US:
            return None
        resolved = self.resolve_entity(query=entity_id.rsplit(":", 1)[-1], region=MarketRegion.US)
        if resolved is None:
            return None
        cik = resolved.entity.identifiers.get("cik")
        if not cik:
            return None
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            response = self.client.get(url, headers=self._sec_headers())
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        recent = payload.get("filings", {}).get("recent", {})
        filings = self._parse_recent_filings(recent, resolved.entity.entity_id, form_type)
        if not filings:
            return None
        record = _record(f"sec:submissions:{cik}", "sec_edgar", url)
        return FilingSearchResult(
            entity_id=resolved.entity.entity_id,
            market=MarketRegion.US,
            filings=filings,
            source_ledger=_ledger(record, [f"filings.{resolved.entity.entity_id}", "filings.search"]),
        )

    def get_source_ledger(self, *, entity_id: str | None = None, field: str | None = None):
        return None

    def get_filing(self, *, filing_id: str, extract_tables: bool = False):
        return None

    def get_ipo_pipeline(self, *, market: MarketRegion | None = None, status: str | None = None):
        return None

    def get_funding_rounds(self, *, entity_id: str | None = None, market: MarketRegion | None = None):
        return None

    def get_company_registry(self, *, entity_id: str, region: MarketRegion | None = None):
        resolved = self.resolve_entity(query=entity_id.rsplit(":", 1)[-1], region=region)
        if resolved is None:
            return None
        from app.finance_data.schemas import CompanyRegistryResult

        return CompanyRegistryResult(
            entity=resolved.entity,
            registry={
                "jurisdiction": resolved.entity.region.value,
                "identifiers": resolved.entity.identifiers,
                "status": "active",
            },
            source_ledger=resolved.source_ledger,
        )

    def _sec_headers(self) -> dict[str, str]:
        identity = self.config.edgar_identity or "Hive Finance Pack ops@example.invalid"
        return {"User-Agent": identity, "Accept-Encoding": "gzip, deflate"}

    def _sec_company_entry(self, query: str) -> dict[str, Any] | None:
        normalized = query.strip().casefold()
        if normalized.startswith("entity:us:"):
            normalized = normalized.rsplit(":", 1)[-1]
        try:
            response = self.client.get("https://www.sec.gov/files/company_tickers.json", headers=self._sec_headers())
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        entries = payload.values() if isinstance(payload, dict) else payload
        for entry in entries:
            ticker = str(entry.get("ticker", "")).casefold()
            title = str(entry.get("title", "")).casefold()
            cik = f"{int(entry.get('cik_str')):010d}" if entry.get("cik_str") is not None else ""
            if normalized in {ticker, title, cik} or normalized == title.replace(" ", ""):
                return entry
        return None

    def _parse_yahoo_chart(self, payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not isinstance(result, dict):
            return []
        timestamps = result.get("timestamp") or []
        quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
        rows: list[dict[str, Any]] = []
        for index, ts in enumerate(timestamps):
            close = self._series_value(quote, "close", index)
            if close is None:
                continue
            rows.append(
                {
                    "date": datetime.fromtimestamp(int(ts), UTC).date().isoformat(),
                    "symbol": symbol,
                    "open": self._series_value(quote, "open", index),
                    "high": self._series_value(quote, "high", index),
                    "low": self._series_value(quote, "low", index),
                    "close": close,
                    "volume": self._series_value(quote, "volume", index),
                }
            )
        return rows

    def _parse_companyfacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        facts = (payload.get("facts") or {}).get("us-gaap") or {}
        revenue = self._fact_values(facts, ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"))
        operating_income = self._fact_values(facts, ("OperatingIncomeLoss",))
        operating_cash_flow = self._fact_values(facts, ("NetCashProvidedByUsedInOperatingActivities",))
        capex = self._fact_values(facts, ("PaymentsToAcquirePropertyPlantAndEquipment",))
        if not revenue and not operating_income and not operating_cash_flow:
            return {}

        free_cash_flow: list[float] = []
        for index, cash_flow in enumerate(operating_cash_flow[-3:]):
            capex_value = capex[-3 + index] if len(capex) >= 3 else 0.0
            free_cash_flow.append(float(cash_flow) - abs(float(capex_value)))
        return {
            "currency": "USD",
            "revenue": revenue[-3:],
            "operating_income": operating_income[-3:],
            "free_cash_flow": free_cash_flow or operating_cash_flow[-3:],
        }

    def _parse_recent_filings(
        self,
        recent: dict[str, list[Any]],
        entity_id: str,
        form_type: str | None,
    ) -> list[FilingRecord]:
        forms = recent.get("form") or []
        accession_numbers = recent.get("accessionNumber") or []
        filing_dates = recent.get("filingDate") or []
        primary_docs = recent.get("primaryDocument") or []
        filings: list[FilingRecord] = []
        for index, form in enumerate(forms[:40]):
            if form_type and form != form_type:
                continue
            accession = str(accession_numbers[index])
            accession_compact = accession.replace("-", "")
            primary_doc = str(primary_docs[index]) if index < len(primary_docs) else ""
            filing_id = f"filing:sec:{entity_id.rsplit(':', 1)[-1]}:{accession_compact}"
            url = f"https://www.sec.gov/Archives/edgar/data/{entity_id.rsplit(':', 1)[-1]}/{accession_compact}/{primary_doc}"
            filed_at = None
            if index < len(filing_dates):
                filed_at = datetime.fromisoformat(str(filing_dates[index])).replace(tzinfo=UTC)
            filings.append(
                FilingRecord(
                    filing_id=filing_id,
                    entity_id=entity_id,
                    market=MarketRegion.US,
                    form_type=str(form),
                    filed_at=filed_at,
                    url=url,
                    source_id=f"sec:filing:{accession_compact}",
                )
            )
        return filings

    def _fact_values(self, facts: dict[str, Any], tags: tuple[str, ...]) -> list[float]:
        for tag in tags:
            units = (facts.get(tag) or {}).get("units") or {}
            values = units.get("USD") or units.get("usd") or []
            annual = [
                item
                for item in values
                if item.get("form") in {"10-K", "20-F", "40-F"} and isinstance(item.get("val"), (int, float))
            ]
            annual.sort(key=lambda item: (item.get("fy") or 0, item.get("filed") or ""))
            if annual:
                return [float(item["val"]) for item in annual[-3:]]
        return []

    def _canonical_symbol(self, symbol: str, market: MarketRegion) -> str:
        normalized = symbol.strip().upper()
        if market == MarketRegion.HK and normalized.isdigit():
            return f"{normalized.zfill(5)}.HK"
        if market == MarketRegion.CN_A and normalized.isdigit():
            return f"{normalized}{'.SS' if normalized.startswith('6') else '.SZ'}"
        return normalized

    def _date_range_to_unix(self, start: str | None, end: str | None) -> tuple[int, int]:
        now = datetime.now(UTC)
        start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC) if start else now - timedelta(days=365)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC) + timedelta(days=1) if end else now
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    def _series_value(self, quote: dict[str, Any], key: str, index: int) -> Any:
        values = quote.get(key) or []
        if index >= len(values):
            return None
        return values[index]
