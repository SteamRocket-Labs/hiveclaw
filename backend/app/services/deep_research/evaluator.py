from __future__ import annotations

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import ClaimStatus, EvaluationResult, ResearchRequest


class ResearchEvaluator:
    def evaluate(self, *, request: ResearchRequest, ledger: EvidenceLedger, round_index: int) -> EvaluationResult:
        summary = ledger.summary()
        gates = {
            "attribution": "passed" if summary["claim_count"] and summary["unsupported_claims"] == 0 else "failed",
            "plurality": "passed" if summary["source_count"] >= min(2, request.max_sources) else "failed",
            "freshness": "passed",
            "completeness": "passed" if summary["source_count"] >= min(2, request.max_sources) else "failed",
            "contradiction": "warning" if summary["contradicted_claims"] else "passed",
        }
        gaps: list[str] = []
        if summary["source_count"] == 0:
            gaps.append("No candidate URL was successfully fetched; search snippets were not used as evidence.")
        if summary["unsupported_claims"]:
            gaps.append(f"{summary['unsupported_claims']} claim(s) remain unsupported by fetched sources.")
        if gates["plurality"] == "failed":
            gaps.append("Fewer than two independent fetched sources are available.")
        if gates["completeness"] == "failed" and round_index < request.max_rounds:
            gaps.append("Additional source lanes should be searched before final confidence.")

        unsupported = [
            claim.text
            for claim in ledger.claims
            if claim.status == ClaimStatus.UNSUPPORTED
        ]
        next_queries = []
        if gates["completeness"] == "failed" and round_index < request.max_rounds:
            next_queries.append(f"{request.question} additional independent sources")

        return EvaluationResult(
            quality_gates=gates,
            gaps=gaps,
            next_queries=next_queries,
            missing_sources=[] if summary["source_count"] else ["fetched_source"],
            unsupported_claims=unsupported,
        )
