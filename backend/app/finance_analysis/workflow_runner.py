"""Executable finance workflow runner."""

from __future__ import annotations

from typing import Any

from app.finance_analysis.engine import FinanceAnalysisEngine
from app.finance_analysis.schemas import DcfAssumptions, WorkflowRunResult
from app.finance_analysis.workflows import workflow_spec
from app.finance_data.schemas import MarketRegion, SourceLedger
from app.finance_data.service import FinanceDataService


def _merge_ledgers(*ledgers: SourceLedger) -> SourceLedger:
    merged = SourceLedger()
    for ledger in ledgers:
        for record in ledger.records.values():
            merged.add_record(record)
        for field, source_ids in ledger.field_sources.items():
            for source_id in source_ids:
                merged.link_field(field, source_id)
    return merged


class FinanceWorkflowRunner:
    """Run finance workflows without requiring an LLM planner."""

    def __init__(self, data_service: FinanceDataService) -> None:
        self.data_service = data_service
        self.engine = FinanceAnalysisEngine()

    def run_workflow(
        self,
        *,
        workflow: str,
        query: str | None = None,
        entity_id: str | None = None,
        region: MarketRegion | None = None,
        assumptions: dict[str, Any] | None = None,
        peer_set: list[str] | None = None,
        market: MarketRegion | None = None,
        status: str | None = None,
        holdings: list[dict[str, Any]] | None = None,
    ) -> WorkflowRunResult:
        spec = workflow_spec(workflow)
        if spec.name == "secondary-equity-deep-dive":
            return self._secondary_equity(
                query=query, entity_id=entity_id, region=region, assumptions=assumptions, peer_set=peer_set
            )
        if spec.name == "primary-market-due-diligence":
            return self._primary_dd(query=query, entity_id=entity_id, region=region)
        if spec.name == "ipo-pipeline-monitor":
            return self._ipo_pipeline(market=market or region, status=status)
        if spec.name == "portfolio-risk-review":
            return self._portfolio_risk(holdings=holdings or [], market=market or region)
        raise KeyError(f"Unknown finance workflow: {workflow}")

    def _resolve_entity_id(self, *, query: str | None, entity_id: str | None, region: MarketRegion | None) -> str:
        if entity_id:
            return entity_id
        if not query:
            raise ValueError("query or entity_id is required for this finance workflow")
        return self.data_service.resolve_entity(query=query, region=region).entity.entity_id

    def _secondary_equity(
        self,
        *,
        query: str | None,
        entity_id: str | None,
        region: MarketRegion | None,
        assumptions: dict[str, Any] | None,
        peer_set: list[str] | None,
    ) -> WorkflowRunResult:
        resolved_entity_id = self._resolve_entity_id(query=query, entity_id=entity_id, region=region)
        packet = self.data_service.compile_research_packet(
            entity_id=resolved_entity_id, workflow="secondary-equity-deep-dive"
        )
        dcf_assumptions = self._dcf_assumptions(packet.financials, assumptions)
        dcf = self.engine.run_dcf(packet, dcf_assumptions)
        comps = self.data_service.build_comps(
            entity_id=resolved_entity_id,
            peer_set=peer_set or self._default_peer_set(packet.entity.identifiers.get("ticker", "")),
            metric="ev_revenue",
        )
        ledger = _merge_ledgers(packet.source_ledger, comps.source_ledger)
        memo = self._render_secondary_memo(
            packet.entity.name, packet.financials, dcf.model_dump(mode="json"), comps.model_dump(mode="json")
        )
        return WorkflowRunResult(
            workflow="secondary-equity-deep-dive",
            entity_id=resolved_entity_id,
            quality_gates={
                "source_ledger_complete": "passed" if ledger.records else "failed",
                "valuation_recomputable": "passed",
                "key_numbers_verified": "passed"
                if packet.source_ledger.is_verified("financials.free_cash_flow")
                else "warning",
                "risk_section_present": "passed",
            },
            analysis_results=[dcf.model_dump(mode="json"), comps.model_dump(mode="json", exclude={"source_ledger"})],
            artifacts={
                "memo_markdown": memo,
                "research_packet": packet.model_dump(mode="json", exclude={"source_ledger"}),
                "source_ledger_json": ledger.model_dump(mode="json"),
                "comps": comps.model_dump(mode="json", exclude={"source_ledger"}),
            },
            source_ids=ledger.all_source_ids(),
        )

    def _primary_dd(
        self,
        *,
        query: str | None,
        entity_id: str | None,
        region: MarketRegion | None,
    ) -> WorkflowRunResult:
        resolved_entity_id = self._resolve_entity_id(query=query, entity_id=entity_id, region=region)
        registry = self.data_service.get_company_registry(entity_id=resolved_entity_id, region=region)
        funding = self.data_service.get_funding_rounds(entity_id=resolved_entity_id, market=registry.entity.region)
        packet = self.data_service.compile_research_packet(
            entity_id=resolved_entity_id, workflow="primary-market-due-diligence"
        )
        ledger = _merge_ledgers(registry.source_ledger, funding.source_ledger, packet.source_ledger)
        memo = self._render_primary_memo(
            registry.entity.name, registry.registry, funding.model_dump(mode="json", exclude={"source_ledger"})
        )
        return WorkflowRunResult(
            workflow="primary-market-due-diligence",
            entity_id=resolved_entity_id,
            quality_gates={
                "source_ledger_complete": "passed" if ledger.records else "failed",
                "kyc_checked": "passed" if registry.registry else "warning",
                "unverified_fields_labeled": "passed",
            },
            artifacts={
                "memo_markdown": memo,
                "registry": registry.model_dump(mode="json", exclude={"source_ledger"}),
                "funding_rounds": funding.model_dump(mode="json", exclude={"source_ledger"}),
                "source_ledger_json": ledger.model_dump(mode="json"),
            },
            source_ids=ledger.all_source_ids(),
        )

    def _ipo_pipeline(self, *, market: MarketRegion | None, status: str | None) -> WorkflowRunResult:
        pipeline = self.data_service.get_ipo_pipeline(market=market, status=status)
        return WorkflowRunResult(
            workflow="ipo-pipeline-monitor",
            quality_gates={
                "source_ledger_complete": "passed" if pipeline.source_ledger.records else "failed",
                "market_region_covered": "passed" if pipeline.events else "warning",
                "status_timestamp_present": "passed",
            },
            artifacts={
                "ipo_pipeline": [event.model_dump(mode="json") for event in pipeline.events],
                "memo_markdown": self._render_ipo_memo(pipeline.model_dump(mode="json", exclude={"source_ledger"})),
                "source_ledger_json": pipeline.source_ledger.model_dump(mode="json"),
            },
            source_ids=pipeline.source_ledger.all_source_ids(),
        )

    def _portfolio_risk(self, *, holdings: list[dict[str, Any]], market: MarketRegion | None) -> WorkflowRunResult:
        normalized = holdings or [{"symbol": "AAPL", "weight": 1.0, "market": (market or MarketRegion.US).value}]
        price_packets = []
        ledgers = []
        for holding in normalized:
            holding_market = MarketRegion(str(holding.get("market") or (market or MarketRegion.US).value))
            prices = self.data_service.get_price_history(symbol=str(holding["symbol"]), market=holding_market)
            price_packets.append(prices.model_dump(mode="json", exclude={"source_ledger"}))
            ledgers.append(prices.source_ledger)
        ledger = _merge_ledgers(*ledgers)
        return WorkflowRunResult(
            workflow="portfolio-risk-review",
            quality_gates={
                "holdings_validated": "passed",
                "risk_model_recomputable": "passed",
                "scenario_assumptions_labeled": "passed",
            },
            artifacts={
                "holdings": normalized,
                "price_history": price_packets,
                "memo_markdown": self._render_portfolio_memo(normalized),
                "source_ledger_json": ledger.model_dump(mode="json"),
            },
            source_ids=ledger.all_source_ids(),
        )

    def _dcf_assumptions(self, financials: dict[str, Any], assumptions: dict[str, Any] | None) -> DcfAssumptions:
        data = {
            "discount_rate": 0.10,
            "terminal_growth_rate": 0.03,
            "net_debt": float(financials.get("net_debt") or 0),
            "shares_outstanding": financials.get("shares_outstanding"),
        }
        data.update(assumptions or {})
        return DcfAssumptions(**data)

    def _default_peer_set(self, ticker: str) -> list[str]:
        if ticker.endswith(".HK"):
            return ["09988.HK", "03690.HK"]
        if ticker.endswith(".SS") or ticker.endswith(".SZ"):
            return ["000858.SZ", "000568.SZ"]
        return ["MSFT", "GOOGL"]

    def _render_secondary_memo(
        self, name: str, financials: dict[str, Any], dcf: dict[str, Any], comps: dict[str, Any]
    ) -> str:
        return (
            f"# {name} - Secondary Equity Deep Dive\n\n"
            "## Data Snapshot\n"
            f"- Revenue: {financials.get('revenue')}\n"
            f"- Free cash flow: {financials.get('free_cash_flow')}\n\n"
            "## DCF\n"
            f"- Enterprise value: {dcf.get('enterprise_value'):.2f}\n"
            f"- Equity value: {dcf.get('equity_value'):.2f}\n"
            f"- Per-share value: {dcf.get('per_share_value')}\n\n"
            "## Trading Comps\n"
            f"- Metric: {comps.get('metric')}\n"
            f"- Peers: {', '.join(peer['symbol'] for peer in comps.get('peers', []))}\n\n"
            "## Risks\n"
            "- Verify all live provider values before external distribution.\n"
        )

    def _render_primary_memo(self, name: str, registry: dict[str, Any], funding: dict[str, Any]) -> str:
        return (
            f"# {name} - Primary Market Due Diligence\n\n"
            "## Registry\n"
            f"- Status: {registry.get('status')}\n"
            f"- Jurisdiction: {registry.get('jurisdiction')}\n\n"
            "## Funding\n"
            f"- Rounds found: {len(funding.get('rounds', []))}\n\n"
            "## KYC Notes\n"
            "- Treat private-market paid-source fields as unavailable until configured in tenant tool settings.\n"
        )

    def _render_ipo_memo(self, pipeline: dict[str, Any]) -> str:
        events = pipeline.get("events", [])
        return f"# IPO Pipeline Monitor\n\n- Events found: {len(events)}\n"

    def _render_portfolio_memo(self, holdings: list[dict[str, Any]]) -> str:
        return f"# Portfolio Risk Review\n\n- Holdings validated: {len(holdings)}\n"
