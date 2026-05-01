"""Finance analysis orchestration over normalized finance data."""

from __future__ import annotations

from app.finance_analysis.calculators.dcf import compute_dcf
from app.finance_analysis.schemas import AnalysisResult, ArtifactBundle, DcfAssumptions, ResearchPacket, ValuationResult


class FinanceAnalysisEngine:
    """Run deterministic calculators and assemble workflow artifacts.

    The engine intentionally consumes ResearchPacket objects instead of calling
    external APIs. Data access belongs in app.finance_data connectors.
    """

    def run_dcf(self, packet: ResearchPacket, assumptions: DcfAssumptions) -> ValuationResult:
        free_cash_flows = packet.financials.get("free_cash_flow")
        if not isinstance(free_cash_flows, list) or not all(
            isinstance(value, (int, float)) for value in free_cash_flows
        ):
            raise ValueError("ResearchPacket.financials['free_cash_flow'] must be a numeric list")

        result = compute_dcf([float(value) for value in free_cash_flows], assumptions)
        result.entity_id = packet.entity.entity_id
        result.source_ids = packet.source_ledger.all_source_ids()
        return result

    def build_ic_memo(self, packet: ResearchPacket, analysis_results: list[AnalysisResult]) -> ArtifactBundle:
        return ArtifactBundle(
            artifact_type="ic_memo",
            entity_id=packet.entity.entity_id,
            title=f"IC Memo - {packet.entity.name}",
            source_ids=packet.source_ledger.all_source_ids(),
            analysis_results=tuple(analysis_results),
        )
