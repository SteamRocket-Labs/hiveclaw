from __future__ import annotations

import json
from pathlib import Path

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import EvaluationResult, ResearchPlan, ResearchRequest, ResearchRun, to_jsonable, utc_now


class ResearchArtifactWriter:
    def __init__(self, artifact_dir: str | Path):
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.request_path = self.artifact_dir / "request.json"
        self.plan_path = self.artifact_dir / "plan.json"
        self.steps_path = self.artifact_dir / "steps.jsonl"
        self.evaluation_path = self.artifact_dir / "evaluation.jsonl"
        self.report_path = self.artifact_dir / "report.md"
        self.final_path = self.artifact_dir / "final.json"

    def write_request(self, request: ResearchRequest) -> None:
        self.request_path.write_text(json.dumps(to_jsonable(request), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_plan(self, plan: ResearchPlan) -> None:
        self.plan_path.write_text(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2), encoding="utf-8")

    def append_step(self, step) -> None:
        with self.steps_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(step), ensure_ascii=False) + "\n")

    def append_evaluation(self, evaluation: EvaluationResult) -> None:
        with self.evaluation_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(evaluation), ensure_ascii=False) + "\n")

    def finalize(
        self,
        *,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger: EvidenceLedger,
        evaluation: EvaluationResult,
        status: str,
        report_markdown: str | None = None,
    ) -> ResearchRun:
        summary = _summary(status=status, ledger=ledger, gaps=evaluation.gaps)
        self.report_path.write_text(
            report_markdown
            or _render_report(request=request, plan=plan, ledger=ledger, evaluation=evaluation, summary=summary),
            encoding="utf-8",
        )
        final_payload = {
            "schema": "deep_research_final.v1",
            "status": status,
            "summary": summary,
            "question": request.question,
            "mode": request.mode,
            "source_count": len(ledger.sources),
            "claim_count": len(ledger.claims),
            "quality_gates": evaluation.quality_gates,
            "gaps": evaluation.gaps,
            "sources": [to_jsonable(item) for item in ledger.sources.values()],
            "claims": [to_jsonable(item) for item in ledger.claims],
            "report_path": self.report_path.as_posix(),
            "created_at": utc_now(),
        }
        self.final_path.write_text(json.dumps(final_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ResearchRun(
            run_id=self.artifact_dir.name,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            artifact_dir=self.artifact_dir.as_posix(),
            report_path=self.report_path.as_posix(),
            sources_path=ledger.sources_path.as_posix(),
            claims_path=ledger.claims_path.as_posix(),
            steps_path=self.steps_path.as_posix(),
            final_path=self.final_path.as_posix(),
            source_count=len(ledger.sources),
            claim_count=len(ledger.claims),
            quality_gates=evaluation.quality_gates,
            gaps=evaluation.gaps,
            completed_at=utc_now(),
        )


def _summary(*, status: str, ledger: EvidenceLedger, gaps: list[str]) -> str:
    if status == "completed":
        return f"Deep research completed with {len(ledger.sources)} fetched source(s) and {len(ledger.claims)} claim(s)."
    return f"Deep research produced a partial report with {len(gaps)} gap(s)."


def _render_report(
    *,
    request: ResearchRequest,
    plan: ResearchPlan,
    ledger: EvidenceLedger,
    evaluation: EvaluationResult,
    summary: str,
) -> str:
    lines = [
        "# Deep Research Report",
        "",
        "## Executive Answer",
        "",
        summary,
        "",
        "## Scope and Method",
        "",
        f"- Question: {request.question}",
        f"- Mode: {request.mode}",
        f"- Scope: {request.scope or 'narrowest useful scope'}",
        f"- Source policy: {request.source_policy}",
        f"- Lanes: {', '.join(lane.label for lane in plan.lanes)}",
        "",
        "## Key Findings",
        "",
    ]
    if ledger.claims:
        for claim in ledger.claims:
            sources = ", ".join(claim.source_ids) or "no fetched source"
            lines.append(f"- [{claim.status.value}] {_clip_report_text(claim.text)} (sources: {sources})")
    else:
        lines.append("- No material claim reached the evidence threshold.")

    lines.extend(["", "## Source Ledger", ""])
    if ledger.sources:
        for source in ledger.sources.values():
            lines.append(f"- `{source.source_id}` {source.title} — {source.publisher} — {source.url}")
    else:
        lines.append("- No fetched source was accepted into the ledger.")

    lines.extend(["", "## Quality Gates", ""])
    for gate, state in evaluation.quality_gates.items():
        lines.append(f"- {gate}: {state}")

    lines.extend(["", "## Contradictions and Gaps", ""])
    if evaluation.gaps:
        lines.extend(f"- {gap}" for gap in evaluation.gaps)
    else:
        lines.append("- No blocking evidence gap recorded.")

    lines.extend(["", "## Next Checks", ""])
    if evaluation.next_queries:
        lines.extend(f"- Search: {query}" for query in evaluation.next_queries)
    else:
        lines.append("- Re-run with higher depth or narrower scope if a stronger source standard is required.")
    lines.append("")
    return "\n".join(lines)


def _clip_report_text(value: str, *, limit: int = 500) -> str:
    normalized = " ".join((value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip(" ,;，；") + "..."
