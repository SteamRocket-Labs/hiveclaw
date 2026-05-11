from __future__ import annotations

from urllib.parse import urlparse

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import ClaimStatus, EvaluationResult, ResearchRequest


class ResearchEvaluator:
    def evaluate(self, *, request: ResearchRequest, ledger: EvidenceLedger, round_index: int) -> EvaluationResult:
        summary = ledger.summary()
        independent_hosts = _independent_hosts(ledger)
        plurality_required = min(2, request.max_sources)
        coverage_required = _required_source_count(request)
        gates = {
            "attribution": "passed" if summary["claim_count"] and summary["unsupported_claims"] == 0 else "failed",
            "plurality": "passed" if len(independent_hosts) >= plurality_required else "failed",
            "freshness": "passed",
            "completeness": "passed" if summary["source_count"] >= coverage_required else "failed",
            "contradiction": "warning" if summary["contradicted_claims"] else "passed",
        }
        gaps: list[str] = []
        if summary["source_count"] == 0:
            gaps.append("No candidate URL was successfully fetched; search snippets were not used as evidence.")
        if summary["unsupported_claims"]:
            gaps.append(f"{summary['unsupported_claims']} claim(s) remain unsupported by fetched sources.")
        if gates["plurality"] == "failed":
            gaps.append(f"Fewer than {plurality_required} independent fetched source host(s) are available.")
        if gates["completeness"] == "failed" and round_index < request.max_rounds:
            gaps.append(
                f"Coverage below {request.depth} depth requirement: "
                f"{summary['source_count']} fetched source(s), need at least {coverage_required}."
            )

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


def _required_source_count(request: ResearchRequest) -> int:
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return min(request.max_sources, 4)
    if depth in {"quick", "light"}:
        return min(request.max_sources, 2)
    return min(request.max_sources, 2)


def _independent_hosts(ledger: EvidenceLedger) -> set[str]:
    hosts: set[str] = set()
    for source in ledger.sources.values():
        host = urlparse(source.url).netloc.casefold().removeprefix("www.")
        if host:
            hosts.add(host)
    return hosts
