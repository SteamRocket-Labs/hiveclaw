"""Deterministic DCF calculator."""

from __future__ import annotations

import hashlib
import json

from app.finance_analysis.schemas import DcfAssumptions, ValuationResult


def compute_dcf(free_cash_flows: list[float], assumptions: DcfAssumptions) -> ValuationResult:
    if not free_cash_flows:
        raise ValueError("free_cash_flows must not be empty")
    if assumptions.discount_rate <= 0:
        raise ValueError("discount_rate must be positive")
    if assumptions.terminal_growth_rate >= assumptions.discount_rate:
        raise ValueError("terminal_growth_rate must be below discount_rate")
    if assumptions.shares_outstanding is not None and assumptions.shares_outstanding <= 0:
        raise ValueError("shares_outstanding must be positive when provided")

    discount_rate = assumptions.discount_rate
    present_value_fcf = sum(
        fcf / ((1 + discount_rate) ** period) for period, fcf in enumerate(free_cash_flows, start=1)
    )
    terminal_value = (
        free_cash_flows[-1]
        * (1 + assumptions.terminal_growth_rate)
        / (discount_rate - assumptions.terminal_growth_rate)
    )
    discounted_terminal_value = terminal_value / ((1 + discount_rate) ** len(free_cash_flows))
    enterprise_value = present_value_fcf + discounted_terminal_value
    equity_value = enterprise_value - assumptions.net_debt
    per_share_value = (
        equity_value / assumptions.shares_outstanding if assumptions.shares_outstanding is not None else None
    )

    payload = {
        "free_cash_flows": free_cash_flows,
        "assumptions": assumptions.model_dump(),
        "enterprise_value": enterprise_value,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    return ValuationResult(
        calculation_id=f"dcf:v1:{digest}",
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        per_share_value=per_share_value,
        terminal_value=terminal_value,
        present_value_fcf=present_value_fcf,
        assumptions=assumptions,
    )
