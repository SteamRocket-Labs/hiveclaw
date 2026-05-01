from __future__ import annotations

import httpx

from app.finance_data.config import FinanceProviderConfig
from app.finance_data.connectors.public_http import PublicHttpFinanceConnector
from app.finance_data.schemas import MarketRegion
from app.finance_data.service import FinanceDataService, build_finance_data_service_from_config


def test_public_http_connector_resolves_sec_entity_and_yahoo_prices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files/company_tickers.json"):
            return httpx.Response(
                200,
                json={
                    "0": {
                        "cik_str": 320193,
                        "ticker": "AAPL",
                        "title": "Apple Inc.",
                    }
                },
            )
        if request.url.path.endswith("/v8/finance/chart/AAPL"):
            return httpx.Response(
                200,
                json={
                    "chart": {
                        "result": [
                            {
                                "timestamp": [1767312000, 1767571200],
                                "meta": {"currency": "USD", "symbol": "AAPL"},
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [187.0, 190.5],
                                            "high": [191.2, 193.0],
                                            "low": [186.5, 189.8],
                                            "close": [190.1, 192.4],
                                            "volume": [50500000, 47200000],
                                        }
                                    ]
                                },
                            }
                        ],
                        "error": None,
                    }
                },
            )
        return httpx.Response(404, json={"error": str(request.url)})

    connector = PublicHttpFinanceConnector(
        config=FinanceProviderConfig(public_live_enabled=True, edgar_identity="hive-test@example.com"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    resolved = connector.resolve_entity(query="AAPL", region=MarketRegion.US)
    prices = connector.get_price_history(
        symbol="AAPL",
        market=MarketRegion.US,
        start="2026-01-01",
        end="2026-01-05",
    )

    assert resolved is not None
    assert resolved.entity.entity_id == "entity:us:aapl"
    assert resolved.entity.identifiers["cik"] == "0000320193"
    assert resolved.source_ledger.is_verified("entity.identifiers.cik")

    assert prices is not None
    assert prices.rows[0]["date"] == "2026-01-02"
    assert prices.rows[0]["close"] == 190.1
    assert prices.source_ledger.is_verified("prices.AAPL")


def test_finance_provider_config_exposes_paid_interfaces_without_global_env() -> None:
    config = FinanceProviderConfig.from_tool_config(
        {
            "provider_mode": "tenant_paid",
            "public_live_enabled": True,
            "edgar_identity": "ops@example.com",
            "fmp_api_key": "fmp-secret",
            "tushare_token": "tu-secret",
            "wind_client_id": "wind-client",
            "wind_client_secret": "wind-secret",
            "qichacha_api_key": "qc-secret",
        }
    )
    status = config.provider_status()

    assert config.provider_mode == "tenant_paid"
    assert config.public_live_enabled is True
    assert status["public_sources"]["sec_edgar"]["configured"] is True
    assert status["paid_sources"]["fmp"]["configured"] is True
    assert status["paid_sources"]["tushare"]["configured"] is True
    assert status["paid_sources"]["wind"]["configured"] is True
    assert status["paid_sources"]["qichacha"]["configured"] is True
    assert "fmp-secret" not in str(status)
    assert "wind-secret" not in str(status)


def test_finance_service_from_config_uses_public_connector_then_static_fallback() -> None:
    service = build_finance_data_service_from_config(
        {
            "public_live_enabled": True,
            "edgar_identity": "ops@example.com",
        },
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))),
    )

    assert isinstance(service, FinanceDataService)
    resolved = service.resolve_entity(query="AAPL", region=MarketRegion.US)

    assert resolved.entity.entity_id == "entity:us:aapl"
    assert resolved.source_ledger.is_verified("entity.name")
