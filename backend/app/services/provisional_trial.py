"""Provisional self-evolution trial outcome helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.evolution_ledger import record_rollback_event


DEFAULT_NEGATIVE_SIGNAL_THRESHOLD = 2


def evaluate_provisional_trial_signal(
    *,
    workspace: Path,
    candidate_id: str,
    rollback_ref: str,
    signal: dict[str, Any],
    negative_signal_count: int,
    negative_signal_threshold: int = DEFAULT_NEGATIVE_SIGNAL_THRESHOLD,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Evaluate a provisional-trial signal and roll back once the threshold is met."""

    threshold = max(1, int(negative_signal_threshold))
    count = max(0, int(negative_signal_count))
    if count < threshold:
        return {
            "decision": "continue_trial",
            "candidate_id": candidate_id,
            "negative_signal_count": count,
            "negative_signal_threshold": threshold,
        }

    reason = str(signal.get("reason") or "provisional trial negative signal threshold reached")
    rollback = record_rollback_event(
        workspace,
        candidate_id=candidate_id,
        restored_ref=rollback_ref,
        reason=reason,
        operator="system",
        metadata={
            "source": "provisional_trial",
            "signal": signal,
            "negative_signal_count": count,
            "negative_signal_threshold": threshold,
        },
        transaction=transaction,
    )
    return {
        "decision": "rolled_back",
        "candidate_id": candidate_id,
        "rollback_event": rollback,
        "negative_signal_count": count,
        "negative_signal_threshold": threshold,
    }
