from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.services.deep_research.orchestrator import run_deep_research
from app.services.deep_research.schemas import ResearchRequest, ResearchRun, to_jsonable
from app.services.long_task_runtime import record_long_task_plan, record_long_task_progress
from app.services.runtime_task_service import (
    create_runtime_task_record,
    get_runtime_task_record,
    update_runtime_task_record,
)
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_REQUEST_PROPERTIES: dict[str, Any] = {
    "question": {"type": "string", "description": "The research question to answer."},
    "mode": {
        "type": "string",
        "enum": ["topic_deep_dive", "industry_research", "source_ledger_audit"],
        "description": "Research mode.",
    },
    "scope": {"type": "string", "description": "Optional geography, company, product, or excluded scope."},
    "depth": {"type": "string", "enum": ["quick", "standard", "full", "flagship"]},
    "source_policy": {"type": "string", "enum": ["primary_only", "primary_preferred", "mixed"]},
    "time_window": {"type": "string"},
    "max_rounds": {"type": "integer", "minimum": 1, "maximum": 8},
    "max_sources": {"type": "integer", "minimum": 1, "maximum": 50},
    "concurrency": {"type": "integer", "minimum": 1, "maximum": 12},
    "token_budget": {"type": "integer", "minimum": 1},
    "deadline_seconds": {"type": "integer", "minimum": 10},
    "output_format": {"type": "string", "enum": ["markdown", "json", "html"]},
}


@tool(
    ToolMeta(
        name="deep_research_run",
        description=(
            "Run a source-ledger-backed deep research workflow synchronously for quick or standard scopes. "
            "Produces report.md, sources.jsonl, claims.jsonl, steps.jsonl, and final.json artifacts."
        ),
        parameters=_schema(_REQUEST_PROPERTIES, ["question"]),
        category="research",
        display_name="Deep Research Run",
        icon="🔎",
        is_default=False,
        governance="sensitive",
        pack="deep_research_pack",
        adapter="request",
    )
)
async def deep_research_run(request: ToolExecutionRequest) -> str:
    try:
        research_request = ResearchRequest.from_arguments(request.arguments)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})

    run = await run_deep_research(
        request=research_request,
        agent_id=request.context.agent_id,
        user_id=request.context.user_id,
        workspace=request.context.workspace,
    )
    return _json({"ok": run.status == "completed", **_run_payload(run, request.context.workspace)})


@tool(
    ToolMeta(
        name="deep_research_start",
        description=(
            "Start a long-running source-ledger-backed deep research workflow. "
            "Creates a RuntimeTask and writes resumable artifacts under runtime_artifacts/long_tasks."
        ),
        parameters=_schema(_REQUEST_PROPERTIES, ["question"]),
        category="research",
        display_name="Deep Research Start",
        icon="🔎",
        is_default=False,
        governance="sensitive",
        pack="deep_research_pack",
        adapter="request",
    )
)
async def deep_research_start(request: ToolExecutionRequest) -> str:
    try:
        research_request = ResearchRequest.from_arguments(request.arguments)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})

    task_id = uuid.uuid4()
    await create_runtime_task_record(
        task_id=task_id.hex,
        task_type="deep_research",
        status="running",
        parent_agent_id=request.context.agent_id,
        prompt=research_request.question,
        metadata_json={
            "deep_research": {
                "question": research_request.question,
                "mode": research_request.mode,
                "scope": research_request.scope,
            }
        },
    )
    await record_long_task_plan(
        agent_id=request.context.agent_id,
        runtime_task_id=task_id,
        objective_id=None,
        spec=research_request.question,
        acceptance_criteria=[
            "Deep research artifacts include report.md, sources.jsonl, claims.jsonl, steps.jsonl, and final.json.",
            "Every material claim is source-bound or explicitly marked unsupported.",
            "Terminal status and progress are recorded honestly.",
        ],
        verification_commands=[
            "deep_research_check({ task_id })",
        ],
        risk_gates=[
            "Do not use search snippets as final evidence.",
            "Do not mark unsupported claims as verified.",
        ],
    )
    await record_long_task_progress(
        agent_id=request.context.agent_id,
        runtime_task_id=task_id,
        status="running",
        delta="Deep research task created and background execution scheduled.",
        output_paths=[_relative(request.context.workspace, _deep_research_dir(request.context.workspace, task_id))],
    )
    _schedule_deep_research_background(request, research_request, task_id)
    return _json(
        {
            "ok": True,
            "task_id": task_id.hex,
            "status": "running",
            "artifact_dir": _relative(request.context.workspace, _deep_research_dir(request.context.workspace, task_id)),
            "next_action": f"Use deep_research_check with task_id {task_id.hex} to inspect progress.",
        }
    )


@tool(
    ToolMeta(
        name="deep_research_check",
        description="Check a deep research RuntimeTask, including progress, partial report, source count, claim count, and gaps.",
        parameters=_schema({"task_id": {"type": "string"}}, ["task_id"]),
        category="research",
        display_name="Deep Research Check",
        icon="🔎",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="deep_research_pack",
        adapter="request",
    )
)
async def deep_research_check(request: ToolExecutionRequest) -> str:
    task_id = str(request.arguments.get("task_id") or "").strip()
    if not task_id:
        return _json({"ok": False, "error": "task_id_required"})
    record = await get_runtime_task_record(task_id)
    if record and record.get("parent_agent_id") not in {None, str(request.context.agent_id)}:
        return _json({"ok": False, "error": "forbidden"})
    payload = _read_deep_research_artifact(request.context.workspace, task_id)
    return _json({"ok": True, "task": record, **payload})


@tool(
    ToolMeta(
        name="deep_research_cancel",
        description="Cancel a running deep research RuntimeTask that belongs to the current agent.",
        parameters=_schema({"task_id": {"type": "string"}, "reason": {"type": "string"}}, ["task_id"]),
        category="research",
        display_name="Deep Research Cancel",
        icon="🔎",
        is_default=False,
        governance="sensitive",
        pack="deep_research_pack",
        adapter="request",
    )
)
async def deep_research_cancel(request: ToolExecutionRequest) -> str:
    task_id = str(request.arguments.get("task_id") or "").strip()
    if not task_id:
        return _json({"ok": False, "error": "task_id_required"})
    record = await get_runtime_task_record(task_id)
    if record and record.get("parent_agent_id") not in {None, str(request.context.agent_id)}:
        return _json({"ok": False, "error": "forbidden"})
    await update_runtime_task_record(
        task_id,
        status="killed",
        result_summary=str(request.arguments.get("reason") or "Deep research cancelled."),
        metadata_json={"cancel_reason": str(request.arguments.get("reason") or "cancelled")},
    )
    parsed = _parse_uuid(task_id)
    if parsed:
        await record_long_task_progress(
            agent_id=request.context.agent_id,
            runtime_task_id=parsed,
            status="killed",
            delta="Deep research task was cancelled.",
            blocked_reason=str(request.arguments.get("reason") or "cancelled"),
        )
    return _json({"ok": True, "task_id": task_id, "status": "killed"})


@tool(
    ToolMeta(
        name="deep_research_export",
        description="Export a completed or partial deep research artifact as markdown, json, or html.",
        parameters=_schema(
            {"task_id": {"type": "string"}, "format": {"type": "string", "enum": ["markdown", "json", "html"]}},
            ["task_id"],
        ),
        category="research",
        display_name="Deep Research Export",
        icon="🔎",
        is_default=False,
        governance="sensitive",
        pack="deep_research_pack",
        adapter="request",
    )
)
async def deep_research_export(request: ToolExecutionRequest) -> str:
    task_id = str(request.arguments.get("task_id") or "").strip()
    export_format = str(request.arguments.get("format") or "markdown").strip()
    record = await get_runtime_task_record(task_id)
    if record and record.get("parent_agent_id") not in {None, str(request.context.agent_id)}:
        return _json({"ok": False, "error": "forbidden"})
    artifact_dir = _deep_research_dir(request.context.workspace, task_id)
    final_path = artifact_dir / "final.json"
    report_path = artifact_dir / "report.md"
    if export_format == "json":
        target = final_path
    elif export_format == "html":
        target = artifact_dir / "report.html"
        markdown = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        target.write_text(f"<html><body><pre>{_escape_html(markdown)}</pre></body></html>", encoding="utf-8")
    else:
        target = report_path
    if not target.exists():
        return _json({"ok": False, "error": "artifact_not_found", "task_id": task_id})
    return _json({"ok": True, "task_id": task_id, "format": export_format, "path": _relative(request.context.workspace, target)})


def _schedule_deep_research_background(
    request: ToolExecutionRequest,
    research_request: ResearchRequest,
    task_id: uuid.UUID,
) -> None:
    async def runner() -> None:
        try:
            run = await run_deep_research(
                request=research_request,
                agent_id=request.context.agent_id,
                user_id=request.context.user_id,
                workspace=request.context.workspace,
                runtime_task_id=task_id,
            )
            await update_runtime_task_record(
                task_id.hex,
                status=run.status,
                result_summary=run.summary,
                metadata_json={"deep_research_result": _run_payload(run, request.context.workspace)},
            )
            await record_long_task_progress(
                agent_id=request.context.agent_id,
                runtime_task_id=task_id,
                status=run.status,
                delta=run.summary,
                output_paths=[
                    _relative(request.context.workspace, Path(run.report_path)),
                    _relative(request.context.workspace, Path(run.sources_path)),
                    _relative(request.context.workspace, Path(run.claims_path)),
                ],
                blocked_reason="; ".join(run.gaps) if run.status != "completed" else None,
            )
        except Exception as exc:
            await update_runtime_task_record(task_id.hex, status="failed", result_summary=f"Deep research failed: {type(exc).__name__}")
            await record_long_task_progress(
                agent_id=request.context.agent_id,
                runtime_task_id=task_id,
                status="failed",
                delta="Deep research failed before completion.",
                blocked_reason=f"{type(exc).__name__}: {exc}",
            )

    asyncio.create_task(runner())


def _run_payload(run: ResearchRun, workspace: Path) -> dict[str, Any]:
    return {
        "status": run.status,
        "summary": run.summary,
        "artifact_dir": _relative(workspace, Path(run.artifact_dir)),
        "report_path": _relative(workspace, Path(run.report_path)),
        "sources_path": _relative(workspace, Path(run.sources_path)),
        "claims_path": _relative(workspace, Path(run.claims_path)),
        "steps_path": _relative(workspace, Path(run.steps_path)),
        "final_path": _relative(workspace, Path(run.final_path)),
        "source_count": run.source_count,
        "claim_count": run.claim_count,
        "quality_gates": run.quality_gates,
        "gaps": run.gaps,
    }


def _read_deep_research_artifact(workspace: Path, task_id: str) -> dict[str, Any]:
    artifact_dir = _deep_research_dir(workspace, task_id)
    final = _load_json(artifact_dir / "final.json") or {}
    return {
        "artifact_dir": _relative(workspace, artifact_dir),
        "status": final.get("status"),
        "summary": final.get("summary"),
        "report_path": _relative(workspace, artifact_dir / "report.md") if (artifact_dir / "report.md").exists() else None,
        "source_count": final.get("source_count") or _jsonl_count(artifact_dir / "sources.jsonl"),
        "claim_count": final.get("claim_count") or _jsonl_count(artifact_dir / "claims.jsonl"),
        "quality_gates": final.get("quality_gates") or {},
        "gaps": final.get("gaps") or [],
        "partial_report": (artifact_dir / "report.md").read_text(encoding="utf-8")[:4000]
        if (artifact_dir / "report.md").exists()
        else "",
    }


def _deep_research_dir(workspace: Path, task_id: uuid.UUID | str) -> Path:
    return workspace / "runtime_artifacts" / "long_tasks" / str(task_id).replace("-", "") / "deep_research"


def _relative(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, default=str)
