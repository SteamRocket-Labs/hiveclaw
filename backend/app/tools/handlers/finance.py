"""Finance pack tools backed by normalized finance_data and finance_analysis layers."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.finance_analysis.calculators.dcf import compute_dcf
from app.finance_analysis.schemas import DcfAssumptions
from app.finance_analysis.workflow_runner import FinanceWorkflowRunner
from app.finance_data.config import FinanceProviderConfig
from app.finance_data.schemas import MarketRegion, SourceLedger
from app.finance_data.service import build_finance_data_service_from_config
from app.tools.decorator import ToolMeta, tool


_MARKET_ALIASES: dict[str, MarketRegion] = {
    "us": MarketRegion.US,
    "usa": MarketRegion.US,
    "u.s.": MarketRegion.US,
    "hk": MarketRegion.HK,
    "hongkong": MarketRegion.HK,
    "hong_kong": MarketRegion.HK,
    "cn": MarketRegion.CN_A,
    "china": MarketRegion.CN_A,
    "cn_a": MarketRegion.CN_A,
    "a": MarketRegion.CN_A,
    "a_share": MarketRegion.CN_A,
    "ashare": MarketRegion.CN_A,
    "global": MarketRegion.GLOBAL,
}

_FINANCE_CONFIG = {
    "provider_mode": "public_default",
    "public_live_enabled": True,
    "edgar_identity": "",
    "fmp_api_key": "",
    "polygon_api_key": "",
    "eodhd_api_key": "",
    "tushare_token": "",
    "wind_client_id": "",
    "wind_client_secret": "",
    "ifind_token": "",
    "choice_token": "",
    "qichacha_api_key": "",
    "tianyancha_api_key": "",
    "crunchbase_api_key": "",
    "pitchbook_api_key": "",
    "capital_iq_client_id": "",
    "capital_iq_client_secret": "",
}

_FINANCE_CONFIG_SCHEMA = {
    "fields": [
        {
            "key": "provider_mode",
            "label": "Provider mode",
            "type": "select",
            "options": [
                {"value": "public_default", "label": "Public default"},
                {"value": "tenant_paid", "label": "Tenant paid provider"},
            ],
            "default": "public_default",
        },
        {
            "key": "public_live_enabled",
            "label": "Enable live public HTTP sources",
            "type": "boolean",
            "default": True,
        },
        {
            "key": "edgar_identity",
            "label": "SEC EDGAR identity",
            "type": "password",
            "default": "",
            "placeholder": "company@example.com",
        },
        {
            "key": "fmp_api_key",
            "label": "FMP API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "tushare_token",
            "label": "Tushare token",
            "type": "password",
            "default": "",
        },
        {
            "key": "polygon_api_key",
            "label": "Polygon API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "eodhd_api_key",
            "label": "EODHD API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "wind_client_id",
            "label": "Wind client id",
            "type": "password",
            "default": "",
        },
        {
            "key": "wind_client_secret",
            "label": "Wind client secret",
            "type": "password",
            "default": "",
        },
        {
            "key": "ifind_token",
            "label": "iFinD token",
            "type": "password",
            "default": "",
        },
        {
            "key": "choice_token",
            "label": "Choice token",
            "type": "password",
            "default": "",
        },
        {
            "key": "qichacha_api_key",
            "label": "Qichacha API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "tianyancha_api_key",
            "label": "Tianyancha API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "crunchbase_api_key",
            "label": "Crunchbase API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "pitchbook_api_key",
            "label": "PitchBook API key",
            "type": "password",
            "default": "",
        },
        {
            "key": "capital_iq_client_id",
            "label": "Capital IQ client id",
            "type": "password",
            "default": "",
        },
        {
            "key": "capital_iq_client_secret",
            "label": "Capital IQ client secret",
            "type": "password",
            "default": "",
        },
    ]
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _market(value: Any, *, default: MarketRegion | None = None) -> MarketRegion | None:
    if value is None or value == "":
        return default
    if isinstance(value, MarketRegion):
        return value
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in _MARKET_ALIASES:
        return _MARKET_ALIASES[normalized]
    return MarketRegion(normalized)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _model_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _model_dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_dump(item) for item in value]
    return value


def _ok(data: Any, source_ledger: SourceLedger | None = None, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": True, "data": _model_dump(data)}
    if source_ledger is not None:
        payload["source_ledger"] = source_ledger.model_dump(mode="json")
    payload.update(extra)
    return _json(payload)


def _error(exc: Exception) -> str:
    return _json({"ok": False, "error_type": exc.__class__.__name__, "error": str(exc)})


def _peer_set(arguments: dict[str, Any]) -> list[str]:
    value = arguments.get("peer_set") or arguments.get("peers") or []
    if isinstance(value, str):
        return [item.strip().upper() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return []


async def _finance_tool_config(tool_name: str) -> dict[str, Any]:
    try:
        from app.core.execution_context import get_tool_tenant_id
        from app.services.tool_config_service import resolve_tool_config

        return await resolve_tool_config(tool_name, get_tool_tenant_id())
    except Exception:
        return dict(_FINANCE_CONFIG)


async def _finance_service(tool_name: str):
    return build_finance_data_service_from_config(await _finance_tool_config(tool_name))


@tool(
    ToolMeta(
        name="finance_get_provider_status",
        description=(
            "Inspect finance data provider readiness without exposing secrets. "
            "Shows public source status and which tenant-scoped paid providers are configured."
        ),
        parameters=_schema({}),
        category="finance",
        icon="📡",
        display_name="Get Finance Provider Status",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_provider_status(arguments: dict) -> str:
    try:
        config = FinanceProviderConfig.from_tool_config(await _finance_tool_config("finance_get_provider_status"))
        service = build_finance_data_service_from_config(config)
        return _ok(service.provider_status())
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_resolve_entity",
        description=(
            "Resolve a company, security, fund, person, or deal query into a normalized finance entity. "
            "Covers US, Hong Kong, and China A-share public defaults and returns field-level source attribution."
        ),
        parameters=_schema(
            {
                "query": {"type": "string", "description": "Company name, ticker, CIK, or known entity id."},
                "region": {
                    "type": "string",
                    "enum": ["us", "hk", "cn_a", "global"],
                    "description": "Optional market region.",
                },
            },
            ["query"],
        ),
        category="finance",
        icon="🎯",
        display_name="Resolve Finance Entity",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_resolve_entity(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_resolve_entity")
        result = service.resolve_entity(
            query=str(arguments.get("query") or ""),
            region=_market(arguments.get("region")),
        )
        return _ok({"entity": result.entity}, result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_source_ledger",
        description="Return source records and field-source mappings for a finance entity or a specific normalized field.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id, e.g. entity:us:aapl."},
                "field": {"type": "string", "description": "Optional field path, e.g. financials.free_cash_flow."},
            },
        ),
        category="finance",
        icon="📒",
        display_name="Get Finance Source Ledger",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_source_ledger(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_source_ledger")
        result = service.get_source_ledger(
            entity_id=arguments.get("entity_id"),
            field=arguments.get("field"),
        )
        return _ok(
            {"entity_id": result.entity_id, "field": result.field},
            result.source_ledger,
        )
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_price_history",
        description="Get normalized historical prices for US, HK, or A-share securities with source attribution.",
        parameters=_schema(
            {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. AAPL, 00700.HK, or 600519.SS."},
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Market region."},
                "start": {"type": "string", "description": "Optional ISO date start, YYYY-MM-DD."},
                "end": {"type": "string", "description": "Optional ISO date end, YYYY-MM-DD."},
                "freq": {"type": "string", "description": "Frequency, default 1d."},
            },
            ["symbol", "market"],
        ),
        category="finance",
        icon="📈",
        display_name="Get Price History",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_price_history(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_price_history")
        result = service.get_price_history(
            symbol=str(arguments.get("symbol") or ""),
            market=_market(arguments.get("market"), default=MarketRegion.US) or MarketRegion.US,
            start=arguments.get("start"),
            end=arguments.get("end"),
            freq=str(arguments.get("freq") or "1d"),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_financial_statements",
        description="Get normalized annual or quarterly financial statements for a resolved finance entity.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id."},
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Market region."},
                "period": {"type": "string", "description": "annual or quarterly. Default annual."},
            },
            ["entity_id", "market"],
        ),
        category="finance",
        icon="📊",
        display_name="Get Financial Statements",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_financial_statements(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_financial_statements")
        result = service.get_financial_statements(
            entity_id=str(arguments.get("entity_id") or ""),
            market=_market(arguments.get("market"), default=MarketRegion.US) or MarketRegion.US,
            period=str(arguments.get("period") or "annual"),
        )
        return _ok(
            result.data,
            result.source_ledger,
            entity_id=result.entity_id,
            market=result.market.value,
            period=result.period,
        )
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_search_filings",
        description="Search public filings for a resolved entity across SEC, HKEX, and China exchange disclosure surfaces.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id."},
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Market region."},
                "form_type": {"type": "string", "description": "Optional form type, e.g. 10-K or annual_report."},
            },
            ["entity_id", "market"],
        ),
        category="finance",
        icon="🔎",
        display_name="Search Filings",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_search_filings(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_search_filings")
        result = service.search_filings(
            entity_id=str(arguments.get("entity_id") or ""),
            market=_market(arguments.get("market"), default=MarketRegion.US) or MarketRegion.US,
            form_type=arguments.get("form_type"),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_filing",
        description="Fetch a filing content packet by filing id, optionally including extracted tables.",
        parameters=_schema(
            {
                "filing_id": {"type": "string", "description": "Filing id returned by finance_search_filings."},
                "extract_tables": {
                    "type": "boolean",
                    "description": "Include normalized extracted tables. Default false.",
                },
            },
            ["filing_id"],
        ),
        category="finance",
        icon="📄",
        display_name="Get Filing",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_filing(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_filing")
        result = service.get_filing(
            filing_id=str(arguments.get("filing_id") or ""),
            extract_tables=bool(arguments.get("extract_tables")),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_ipo_pipeline",
        description="Return public IPO pipeline events for US, HK, and A-share primary-market workflows.",
        parameters=_schema(
            {
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Optional market region."},
                "status": {"type": "string", "description": "Optional pipeline status filter."},
            },
        ),
        category="finance",
        icon="🚀",
        display_name="Get IPO Pipeline",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_ipo_pipeline(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_ipo_pipeline")
        result = service.get_ipo_pipeline(
            market=_market(arguments.get("market")),
            status=arguments.get("status"),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_funding_rounds",
        description="Return primary-market funding rounds for an entity or market. Paid providers remain tenant-scoped optional config.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Optional normalized entity id."},
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Optional market region."},
            },
        ),
        category="finance",
        icon="💰",
        display_name="Get Funding Rounds",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="sensitive",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_funding_rounds(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_funding_rounds")
        result = service.get_funding_rounds(
            entity_id=arguments.get("entity_id"),
            market=_market(arguments.get("market")),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_get_company_registry",
        description="Return company registry and KYC-style identifiers for a resolved entity.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id."},
                "region": {
                    "type": "string",
                    "enum": ["us", "hk", "cn_a"],
                    "description": "Optional expected entity region.",
                },
            },
            ["entity_id"],
        ),
        category="finance",
        icon="🏢",
        display_name="Get Company Registry",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="sensitive",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_get_company_registry(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_get_company_registry")
        result = service.get_company_registry(
            entity_id=str(arguments.get("entity_id") or ""),
            region=_market(arguments.get("region")),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_compute_dcf",
        description="Run a deterministic DCF calculation from free cash flows and explicit assumptions.",
        parameters=_schema(
            {
                "free_cash_flows": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Projected free cash flows.",
                },
                "financials": {"type": "object", "description": "Optional financials dict containing free_cash_flow."},
                "assumptions": {
                    "type": "object",
                    "properties": {
                        "discount_rate": {"type": "number"},
                        "terminal_growth_rate": {"type": "number"},
                        "net_debt": {"type": "number"},
                        "shares_outstanding": {"type": "number"},
                    },
                    "required": ["discount_rate", "terminal_growth_rate"],
                },
            },
            ["assumptions"],
        ),
        category="finance",
        icon="🧮",
        display_name="Compute DCF",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_compute_dcf(arguments: dict) -> str:
    try:
        financials = arguments.get("financials") if isinstance(arguments.get("financials"), dict) else {}
        free_cash_flows = arguments.get("free_cash_flows") or financials.get("free_cash_flow")
        if not isinstance(free_cash_flows, list):
            raise ValueError("free_cash_flows or financials.free_cash_flow must be a numeric list")
        assumptions = DcfAssumptions(**dict(arguments.get("assumptions") or {}))
        result = compute_dcf([float(value) for value in free_cash_flows], assumptions)
        result.entity_id = arguments.get("entity_id")
        return _ok(result)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_build_comps",
        description="Build a deterministic comparable-company metric snapshot for a resolved entity and peer set.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id."},
                "peer_set": {"type": "array", "items": {"type": "string"}, "description": "Peer tickers or symbols."},
                "metric": {"type": "string", "description": "Comparable metric, e.g. ev_revenue or pe."},
            },
            ["entity_id", "peer_set"],
        ),
        category="finance",
        icon="📐",
        display_name="Build Comps",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_build_comps(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_build_comps")
        result = service.build_comps(
            entity_id=str(arguments.get("entity_id") or ""),
            peer_set=_peer_set(arguments),
            metric=str(arguments.get("metric") or "ev_revenue"),
        )
        return _ok(result.model_dump(mode="json", exclude={"source_ledger"}), result.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_compile_research_packet",
        description="Compile a research packet with entity, financials, filings, market data, and source ledger for a finance workflow.",
        parameters=_schema(
            {
                "entity_id": {"type": "string", "description": "Normalized entity id."},
                "workflow": {"type": "string", "description": "Workflow name, e.g. secondary-equity-deep-dive."},
            },
            ["entity_id"],
        ),
        category="finance",
        icon="📦",
        display_name="Compile Research Packet",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_compile_research_packet(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_compile_research_packet")
        packet = service.compile_research_packet(
            entity_id=str(arguments.get("entity_id") or ""),
            workflow=str(arguments.get("workflow") or "secondary-equity-deep-dive"),
        )
        return _ok(packet.model_dump(mode="json", exclude={"source_ledger"}), packet.source_ledger)
    except Exception as exc:
        return _error(exc)


@tool(
    ToolMeta(
        name="finance_run_workflow",
        description=(
            "Run an end-to-end finance workflow and return memo artifacts, analysis results, quality gates, "
            "and source ledger. Supported workflows: secondary-equity-deep-dive, primary-market-due-diligence, "
            "ipo-pipeline-monitor, portfolio-risk-review."
        ),
        parameters=_schema(
            {
                "workflow": {
                    "type": "string",
                    "enum": [
                        "secondary-equity-deep-dive",
                        "primary-market-due-diligence",
                        "ipo-pipeline-monitor",
                        "portfolio-risk-review",
                    ],
                    "description": "Workflow name.",
                },
                "query": {"type": "string", "description": "Company/ticker query when entity_id is not known."},
                "entity_id": {"type": "string", "description": "Optional normalized entity id."},
                "region": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Optional entity region."},
                "market": {"type": "string", "enum": ["us", "hk", "cn_a"], "description": "Optional market filter."},
                "status": {"type": "string", "description": "Optional IPO/funding status filter."},
                "peer_set": {"type": "array", "items": {"type": "string"}, "description": "Optional peer symbols."},
                "holdings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional portfolio holdings for portfolio-risk-review.",
                },
                "assumptions": {"type": "object", "description": "Optional DCF assumptions."},
            },
            ["workflow"],
        ),
        category="finance",
        icon="⚙️",
        display_name="Run Finance Workflow",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="finance_pack",
        adapter="args_only",
        config=_FINANCE_CONFIG,
        config_schema=_FINANCE_CONFIG_SCHEMA,
    )
)
async def finance_run_workflow(arguments: dict) -> str:
    try:
        service = await _finance_service("finance_run_workflow")
        runner = FinanceWorkflowRunner(service)
        peer_set = _peer_set(arguments)
        result = runner.run_workflow(
            workflow=str(arguments.get("workflow") or "secondary-equity-deep-dive"),
            query=arguments.get("query"),
            entity_id=arguments.get("entity_id"),
            region=_market(arguments.get("region")),
            market=_market(arguments.get("market")),
            status=arguments.get("status"),
            peer_set=peer_set or None,
            holdings=arguments.get("holdings") if isinstance(arguments.get("holdings"), list) else None,
            assumptions=arguments.get("assumptions") if isinstance(arguments.get("assumptions"), dict) else None,
        )
        return _ok(result)
    except Exception as exc:
        return _error(exc)
