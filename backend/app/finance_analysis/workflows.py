"""Named finance workflow contracts.

These are declarative boundaries for orchestration. They do not execute LLMs
or call external data sources.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinanceWorkflowSpec:
    name: str
    description: str
    subagents: tuple[str, ...]
    required_tools: tuple[str, ...]
    artifacts: tuple[str, ...]
    quality_gates: tuple[str, ...]


_WORKFLOWS: tuple[FinanceWorkflowSpec, ...] = (
    FinanceWorkflowSpec(
        name="secondary-equity-deep-dive",
        description="Sell-side style listed-company research with filing review, valuation, catalysts, and risks.",
        subagents=(
            "data_compiler",
            "filing_reader",
            "statement_analyst",
            "market_comps",
            "valuation",
            "risk_compliance",
            "ic_memo_writer",
        ),
        required_tools=(
            "finance_resolve_entity",
            "finance_compile_research_packet",
            "finance_get_financial_statements",
            "finance_search_filings",
            "finance_get_filing",
            "finance_compute_dcf",
            "finance_build_comps",
        ),
        artifacts=(
            "reports/{entity_id}-deep-dive.md",
            "models/{entity_id}-valuation.xlsx",
            "sources/{entity_id}-source-ledger.json",
        ),
        quality_gates=(
            "source_ledger_complete",
            "valuation_recomputable",
            "key_numbers_verified",
            "risk_section_present",
        ),
    ),
    FinanceWorkflowSpec(
        name="primary-market-due-diligence",
        description="Company, people, fund, investor, KYC, funding, and deal diligence for private-market work.",
        subagents=(
            "data_compiler",
            "primary_market_dd",
            "risk_compliance",
            "macro_geopolitics",
            "ic_memo_writer",
        ),
        required_tools=(
            "finance_resolve_entity",
            "finance_get_company_registry",
            "finance_get_funding_rounds",
            "finance_compile_research_packet",
        ),
        artifacts=(
            "reports/{entity_id}-primary-dd.md",
            "sources/{entity_id}-source-ledger.json",
        ),
        quality_gates=("source_ledger_complete", "kyc_checked", "unverified_fields_labeled"),
    ),
    FinanceWorkflowSpec(
        name="ipo-pipeline-monitor",
        description="US/HK/A-share IPO pipeline monitor with filing/source attribution.",
        subagents=("data_compiler", "filing_reader", "risk_compliance", "ic_memo_writer"),
        required_tools=(
            "finance_get_ipo_pipeline",
            "finance_search_filings",
            "finance_get_filing",
            "finance_compile_research_packet",
        ),
        artifacts=("reports/ipo-pipeline-{date}.md", "sources/ipo-pipeline-{date}-source-ledger.json"),
        quality_gates=("source_ledger_complete", "market_region_covered", "status_timestamp_present"),
    ),
    FinanceWorkflowSpec(
        name="portfolio-risk-review",
        description="Portfolio exposure, concentration, VaR/CVaR, drawdown, stress, and factor-risk review.",
        subagents=("data_compiler", "portfolio_risk", "macro_geopolitics", "ic_memo_writer"),
        required_tools=("finance_get_price_history", "finance_get_source_ledger", "finance_compile_research_packet"),
        artifacts=("reports/{portfolio_id}-risk-review.md", "models/{portfolio_id}-risk.xlsx"),
        quality_gates=("holdings_validated", "risk_model_recomputable", "scenario_assumptions_labeled"),
    ),
)

_WORKFLOW_MAP = {workflow.name: workflow for workflow in _WORKFLOWS}


def workflow_names() -> tuple[str, ...]:
    return tuple(_WORKFLOW_MAP)


def workflow_spec(name: str) -> FinanceWorkflowSpec:
    try:
        return _WORKFLOW_MAP[name]
    except KeyError as exc:
        raise KeyError(f"Unknown finance workflow: {name}") from exc
