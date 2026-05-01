"""Finance provider configuration with tenant-scoped credential boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class FinanceProviderConfig:
    """Effective finance data provider configuration.

    Values come from tenant-scoped tool config. This object intentionally never
    reads process environment variables so multi-tenant deployments do not leak
    provider credentials across tenants.
    """

    provider_mode: str = "public_default"
    public_live_enabled: bool = True
    edgar_identity: str = ""
    fmp_api_key: str = ""
    polygon_api_key: str = ""
    eodhd_api_key: str = ""
    tushare_token: str = ""
    wind_client_id: str = ""
    wind_client_secret: str = ""
    ifind_token: str = ""
    choice_token: str = ""
    qichacha_api_key: str = ""
    tianyancha_api_key: str = ""
    crunchbase_api_key: str = ""
    pitchbook_api_key: str = ""
    capital_iq_client_id: str = ""
    capital_iq_client_secret: str = ""

    @classmethod
    def from_tool_config(cls, config: dict[str, Any] | None) -> "FinanceProviderConfig":
        raw = dict(config or {})
        return cls(
            provider_mode=str(raw.get("provider_mode") or "public_default"),
            public_live_enabled=_truthy(raw.get("public_live_enabled"), default=True),
            edgar_identity=str(raw.get("edgar_identity") or ""),
            fmp_api_key=str(raw.get("fmp_api_key") or ""),
            polygon_api_key=str(raw.get("polygon_api_key") or ""),
            eodhd_api_key=str(raw.get("eodhd_api_key") or ""),
            tushare_token=str(raw.get("tushare_token") or ""),
            wind_client_id=str(raw.get("wind_client_id") or ""),
            wind_client_secret=str(raw.get("wind_client_secret") or ""),
            ifind_token=str(raw.get("ifind_token") or ""),
            choice_token=str(raw.get("choice_token") or ""),
            qichacha_api_key=str(raw.get("qichacha_api_key") or ""),
            tianyancha_api_key=str(raw.get("tianyancha_api_key") or ""),
            crunchbase_api_key=str(raw.get("crunchbase_api_key") or ""),
            pitchbook_api_key=str(raw.get("pitchbook_api_key") or ""),
            capital_iq_client_id=str(raw.get("capital_iq_client_id") or ""),
            capital_iq_client_secret=str(raw.get("capital_iq_client_secret") or ""),
        )

    def provider_status(self) -> dict[str, Any]:
        def source(configured: bool, *, key: str | None = None, note: str = "") -> dict[str, Any]:
            return {
                "configured": configured,
                "credential_scope": "tenant_tool_config",
                "credential_key": key,
                "note": note,
            }

        return {
            "provider_mode": self.provider_mode,
            "public_live_enabled": self.public_live_enabled,
            "credential_boundary": "tenant_tool_config_only",
            "public_sources": {
                "sec_edgar": source(
                    bool(self.edgar_identity),
                    key="edgar_identity",
                    note="SEC requests use this as User-Agent contact when configured.",
                ),
                "yahoo_chart": source(True, note="Public chart endpoint; no credential required."),
                "hkexnews": source(
                    False, note="Public catalog declared; live connector can be added without changing tool surface."
                ),
                "cninfo": source(
                    False, note="Public catalog declared; live connector can be added without changing tool surface."
                ),
            },
            "paid_sources": {
                "fmp": source(bool(self.fmp_api_key), key="fmp_api_key"),
                "polygon": source(bool(self.polygon_api_key), key="polygon_api_key"),
                "eodhd": source(bool(self.eodhd_api_key), key="eodhd_api_key"),
                "tushare": source(bool(self.tushare_token), key="tushare_token"),
                "wind": source(
                    bool(self.wind_client_id and self.wind_client_secret), key="wind_client_id/wind_client_secret"
                ),
                "ifind": source(bool(self.ifind_token), key="ifind_token"),
                "choice": source(bool(self.choice_token), key="choice_token"),
                "qichacha": source(bool(self.qichacha_api_key), key="qichacha_api_key"),
                "tianyancha": source(bool(self.tianyancha_api_key), key="tianyancha_api_key"),
                "crunchbase": source(bool(self.crunchbase_api_key), key="crunchbase_api_key"),
                "pitchbook": source(bool(self.pitchbook_api_key), key="pitchbook_api_key"),
                "capital_iq": source(
                    bool(self.capital_iq_client_id and self.capital_iq_client_secret),
                    key="capital_iq_client_id/capital_iq_client_secret",
                ),
            },
        }
