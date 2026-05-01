"""Finance analysis schemas consumed by workflows and calculators."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.finance_data.schemas import EntityMasterRecord, SourceLedger


class ResearchPacket(BaseModel):
    entity: EntityMasterRecord
    source_ledger: SourceLedger = Field(default_factory=SourceLedger)
    financials: dict[str, Any] = Field(default_factory=dict)
    filings: list[dict[str, Any]] = Field(default_factory=list)
    market_data: dict[str, Any] = Field(default_factory=dict)
    primary_market_data: dict[str, Any] = Field(default_factory=dict)

    def require_verified_field(self, field_path: str) -> None:
        if not self.source_ledger.is_verified(field_path):
            raise ValueError(f"{field_path} is [UNVERIFIED]")


class DcfAssumptions(BaseModel):
    discount_rate: float
    terminal_growth_rate: float
    net_debt: float = 0.0
    shares_outstanding: float | None = None


class AnalysisResult(BaseModel):
    calculation_id: str
    analysis_type: str
    entity_id: str | None = None
    confidence: Literal["low", "medium", "high"] = "medium"
    source_ids: tuple[str, ...] = ()


class ValuationResult(AnalysisResult):
    analysis_type: str = "valuation"
    enterprise_value: float
    equity_value: float
    per_share_value: float | None = None
    terminal_value: float
    present_value_fcf: float
    assumptions: DcfAssumptions


class Holding(BaseModel):
    symbol: str
    weight: float
    market_value: float | None = None


class RiskScenario(BaseModel):
    name: str = "base"
    confidence: float = 0.95
    horizon_days: int = 1


class RiskResult(AnalysisResult):
    analysis_type: str = "portfolio_risk"
    var: float | None = None
    cvar: float | None = None
    annualized_volatility: float | None = None


class ArtifactBundle(BaseModel):
    artifact_type: str
    entity_id: str
    title: str
    source_ids: tuple[str, ...] = ()
    analysis_results: tuple[AnalysisResult, ...] = ()
    files: tuple[str, ...] = ()


class WorkflowRunResult(BaseModel):
    workflow: str
    entity_id: str | None = None
    quality_gates: dict[str, str] = Field(default_factory=dict)
    analysis_results: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
