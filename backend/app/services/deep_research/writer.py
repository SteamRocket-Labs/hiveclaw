from __future__ import annotations

import json
from pathlib import Path

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import (
    EvaluationResult,
    ResearchPlan,
    ResearchRequest,
    ResearchRun,
    WorkerResult,
    to_jsonable,
    utc_now,
)


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
        self.source_notes_path = self.artifact_dir / "source_notes.jsonl"
        self.lane_summaries_path = self.artifact_dir / "lane_summaries.jsonl"
        self.reflection_path = self.artifact_dir / "reflection.jsonl"
        self.worker_reports_path = self.artifact_dir / "worker_reports.jsonl"

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

    def append_source_note(self, note: dict) -> None:
        with self.source_notes_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(note), ensure_ascii=False) + "\n")

    def append_lane_summary(self, summary: dict) -> None:
        with self.lane_summaries_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(summary), ensure_ascii=False) + "\n")

    def append_reflection(self, decision: dict) -> None:
        with self.reflection_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(decision), ensure_ascii=False) + "\n")

    def append_worker_report(self, result: WorkerResult) -> None:
        with self.worker_reports_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(result), ensure_ascii=False) + "\n")

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
        if status == "completed" and report_markdown:
            self.report_path.write_text(report_markdown, encoding="utf-8")
        else:
            self.report_path.write_text(
                _failure_notice(request=request, ledger=ledger, evaluation=evaluation),
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
            "source_notes_path": self.source_notes_path.as_posix() if self.source_notes_path.exists() else None,
            "lane_summaries_path": self.lane_summaries_path.as_posix() if self.lane_summaries_path.exists() else None,
            "reflection_path": self.reflection_path.as_posix() if self.reflection_path.exists() else None,
            "worker_reports_path": self.worker_reports_path.as_posix() if self.worker_reports_path.exists() else None,
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


def _failure_notice(
    *,
    request: ResearchRequest,
    ledger: EvidenceLedger,
    evaluation: EvaluationResult,
) -> str:
    """Short, honest failure notice. Tier 1-2: never pasted evidence-list dump."""
    lines = [
        "# Deep Research — Synthesis Failed",
        "",
        "**This is not a completed Deep Research report.**",
        "",
        f"Question: {request.question}",
        "",
        "## What happened",
        "",
        (
            "The Deep Research workflow ran but the analyst-grade synthesis step did not "
            "produce a user-deliverable report. The evidence ledger and structured notes "
            "have been preserved so the run can be diagnosed and re-attempted."
        ),
        "",
        "## Diagnostics",
        "",
        f"- Sources fetched: {len(ledger.sources)}",
        f"- Claims extracted: {len(ledger.claims)}",
        f"- Quality gates: {', '.join(f'{name}={state}' for name, state in evaluation.quality_gates.items()) or 'none recorded'}",
        "",
        "## Gaps",
        "",
    ]
    if evaluation.gaps:
        lines.extend(f"- {gap}" for gap in evaluation.gaps)
    else:
        lines.append("- No specific gap was recorded by the evaluator.")
    lines.extend(
        [
            "",
            "## Re-run guidance",
            "",
            (
                "Inspect `worker_reports.jsonl`, `sources.jsonl`, `claims.jsonl`, "
                "`source_notes.jsonl`, and `lane_summaries.jsonl` in the artifact directory. "
                "Re-run with an adjusted scope, depth, or model configuration once the failure "
                "cause is understood."
            ),
            "",
        ]
    )
    return "\n".join(lines)
