from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator
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
        category="deep_research_pack",
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

    if research_request.depth in {"full", "flagship"}:
        return _json(
            {
                "ok": False,
                "status": "route_to_async",
                "error": "async_required",
                "summary": (
                    "Full or flagship Deep Research is a long-running workflow. "
                    "Use deep_research_start instead of deep_research_run."
                ),
                "recommended_tool": "deep_research_start",
                "next_action": (
                    "Call deep_research_start with the same arguments. "
                    "Do not create triggers to poll this task; use deep_research_check "
                    "or the RuntimeTask/artifact UI."
                ),
            }
        )

    run = await run_deep_research(
        request=research_request,
        agent_id=request.context.agent_id,
        user_id=request.context.user_id,
        workspace=request.context.workspace,
    )
    _publish_workspace_packet(request.context.workspace, run.run_id, Path(run.artifact_dir))
    return _json({"ok": run.status == "completed", **_run_payload(run, request.context.workspace)})


@tool(
    ToolMeta(
        name="deep_research_start",
        description=(
            "Start a long-running source-ledger-backed deep research workflow. "
            "Creates a RuntimeTask and writes resumable artifacts under runtime_artifacts/long_tasks."
        ),
        parameters=_schema(_REQUEST_PROPERTIES, ["question"]),
        category="deep_research_pack",
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
    workspace_artifact_dir = _workspace_export_dir(request.context.workspace, task_id.hex)
    return _json(
        {
            "ok": True,
            "task_id": task_id.hex,
            "status": "running",
            "artifact_dir": _relative(request.context.workspace, _deep_research_dir(request.context.workspace, task_id)),
            "workspace_artifact_dir": _relative(request.context.workspace, workspace_artifact_dir),
            "next_action": (
                f"Use deep_research_check with task_id {task_id.hex} to inspect progress. "
                "Do not create triggers to poll this task; the RuntimeTask/artifact UI tracks progress."
            ),
        }
    )


@tool(
    ToolMeta(
        name="deep_research_check",
        description="Check a deep research RuntimeTask, including progress, partial report, source count, claim count, and gaps.",
        parameters=_schema({"task_id": {"type": "string"}}, ["task_id"]),
        category="deep_research_pack",
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
        category="deep_research_pack",
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
        category="deep_research_pack",
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
        artifact_target = final_path
        file_name = "final.json"
    elif export_format == "html":
        artifact_target = artifact_dir / "report.html"
        markdown = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        artifact_target.write_text(f"<html><body><pre>{_escape_html(markdown)}</pre></body></html>", encoding="utf-8")
        file_name = "report.html"
    else:
        artifact_target = report_path
        file_name = "report.md"
    if not artifact_target.exists():
        return _json({"ok": False, "error": "artifact_not_found", "task_id": task_id})

    workspace_dir = _publish_workspace_packet(request.context.workspace, task_id, artifact_dir)
    target = workspace_dir / file_name
    return _json(
        {
            "ok": True,
            "task_id": task_id,
            "format": export_format,
            "path": _relative(request.context.workspace, target),
            "workspace_artifact_dir": _relative(request.context.workspace, workspace_dir),
            "artifact_path": _relative(request.context.workspace, artifact_target),
        }
    )


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
            _publish_workspace_packet(request.context.workspace, task_id.hex, Path(run.artifact_dir))
            run_payload = _run_payload(run, request.context.workspace)
            await update_runtime_task_record(
                task_id.hex,
                status=run.status,
                result_summary=run.summary,
                metadata_json={"deep_research_result": run_payload},
            )
            await record_long_task_progress(
                agent_id=request.context.agent_id,
                runtime_task_id=task_id,
                status=run.status,
                delta=run.summary,
                output_paths=[
                    run_payload["report_path"],
                    run_payload["sources_path"],
                    run_payload["claims_path"],
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
    artifact_dir = Path(run.artifact_dir) if run.artifact_dir else Path()
    workspace_dir = _workspace_export_dir(workspace, run.run_id)
    workspace_report_path = workspace_dir / "report.md"
    workspace_sources_path = workspace_dir / "sources.jsonl"
    workspace_claims_path = workspace_dir / "claims.jsonl"
    workspace_steps_path = workspace_dir / "steps.jsonl"
    workspace_final_path = workspace_dir / "final.json"
    return {
        "status": run.status,
        "summary": run.summary,
        "artifact_dir": _relative(workspace, artifact_dir),
        "workspace_artifact_dir": _relative(workspace, workspace_dir) if workspace_dir.exists() else None,
        "report_path": _relative(workspace, workspace_report_path if workspace_report_path.exists() else Path(run.report_path)),
        "sources_path": _relative(workspace, workspace_sources_path if workspace_sources_path.exists() else Path(run.sources_path)),
        "claims_path": _relative(workspace, workspace_claims_path if workspace_claims_path.exists() else Path(run.claims_path)),
        "steps_path": _relative(workspace, workspace_steps_path if workspace_steps_path.exists() else Path(run.steps_path)),
        "final_path": _relative(workspace, workspace_final_path if workspace_final_path.exists() else Path(run.final_path)),
        "artifact_report_path": _relative(workspace, Path(run.report_path)),
        "artifact_sources_path": _relative(workspace, Path(run.sources_path)),
        "artifact_claims_path": _relative(workspace, Path(run.claims_path)),
        "artifact_steps_path": _relative(workspace, Path(run.steps_path)),
        "artifact_final_path": _relative(workspace, Path(run.final_path)),
        "source_count": run.source_count,
        "claim_count": run.claim_count,
        "quality_gates": run.quality_gates,
        "gaps": run.gaps,
    }


def _read_deep_research_artifact(workspace: Path, task_id: str) -> dict[str, Any]:
    artifact_dir = _deep_research_dir(workspace, task_id)
    if (artifact_dir / "report.md").exists() or (artifact_dir / "final.json").exists():
        _publish_workspace_packet(workspace, task_id, artifact_dir)
    workspace_dir = _workspace_export_dir(workspace, task_id)
    workspace_report_path = workspace_dir / "report.md"
    workspace_sources_path = workspace_dir / "sources.jsonl"
    workspace_claims_path = workspace_dir / "claims.jsonl"
    workspace_final_path = workspace_dir / "final.json"
    workspace_source_notes_path = workspace_dir / "source_notes.jsonl"
    workspace_lane_summaries_path = workspace_dir / "lane_summaries.jsonl"
    final = _load_json(artifact_dir / "final.json") or {}
    return {
        "artifact_dir": _relative(workspace, artifact_dir),
        "workspace_artifact_dir": _relative(workspace, workspace_dir) if workspace_dir.exists() else None,
        "status": final.get("status"),
        "summary": final.get("summary"),
        "report_path": _relative(workspace, workspace_report_path)
        if workspace_report_path.exists()
        else (_relative(workspace, artifact_dir / "report.md") if (artifact_dir / "report.md").exists() else None),
        "sources_path": _relative(workspace, workspace_sources_path)
        if workspace_sources_path.exists()
        else (_relative(workspace, artifact_dir / "sources.jsonl") if (artifact_dir / "sources.jsonl").exists() else None),
        "claims_path": _relative(workspace, workspace_claims_path)
        if workspace_claims_path.exists()
        else (_relative(workspace, artifact_dir / "claims.jsonl") if (artifact_dir / "claims.jsonl").exists() else None),
        "source_notes_path": _relative(workspace, workspace_source_notes_path)
        if workspace_source_notes_path.exists()
        else (
            _relative(workspace, artifact_dir / "source_notes.jsonl")
            if (artifact_dir / "source_notes.jsonl").exists()
            else None
        ),
        "lane_summaries_path": _relative(workspace, workspace_lane_summaries_path)
        if workspace_lane_summaries_path.exists()
        else (
            _relative(workspace, artifact_dir / "lane_summaries.jsonl")
            if (artifact_dir / "lane_summaries.jsonl").exists()
            else None
        ),
        "final_path": _relative(workspace, workspace_final_path)
        if workspace_final_path.exists()
        else (_relative(workspace, artifact_dir / "final.json") if (artifact_dir / "final.json").exists() else None),
        "artifact_report_path": _relative(workspace, artifact_dir / "report.md")
        if (artifact_dir / "report.md").exists()
        else None,
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


def _workspace_export_dir(workspace: Path, run_id: uuid.UUID | str) -> Path:
    safe_id = str(run_id).strip() or "latest"
    return workspace / "workspace" / "deep_research_reports" / safe_id


def _publish_workspace_packet(workspace: Path, run_id: uuid.UUID | str, artifact_dir: Path) -> Path:
    workspace_dir = _workspace_export_dir(workspace, run_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    for file_name in (
        "request.json",
        "plan.json",
        "steps.jsonl",
        "sources.jsonl",
        "claims.jsonl",
        "evaluation.jsonl",
        "source_notes.jsonl",
        "lane_summaries.jsonl",
        "report.md",
        "report.html",
        "final.json",
    ):
        source = artifact_dir / file_name
        if source.exists() and source.is_file():
            shutil.copyfile(source, workspace_dir / file_name)
    return workspace_dir


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


# ──────────────────────────────────────────────────────────────────────────
# Tier 3-4: streaming generator for SSE-style consumption.
#
# Yields incremental events as new lines appear in steps.jsonl /
# claims.jsonl / source_notes.jsonl / lane_summaries.jsonl / reflection.jsonl
# and finally a `report` event with the partial markdown plus a `final`
# event when final.json appears. Idempotent across reconnects: callers can
# pass `after_step_index` / `after_claim_index` cursors to resume.
#
# Designed to be wrapped by an SSE FastAPI route (deferred to API layer);
# pure async-generator contract here so the kernel and tests can drive it.
# ──────────────────────────────────────────────────────────────────────────

_STREAM_FILES: tuple[tuple[str, str], ...] = (
    ("step", "steps.jsonl"),
    ("claim", "claims.jsonl"),
    ("source_note", "source_notes.jsonl"),
    ("lane_summary", "lane_summaries.jsonl"),
    ("reflection", "reflection.jsonl"),
    ("controller_trace", "controller_trace.jsonl"),
)


async def stream_deep_research_artifacts(
    workspace: Path,
    task_id: str,
    *,
    poll_interval_seconds: float = 0.5,
    cursors: dict[str, int] | None = None,
    deadline_seconds: float | None = None,
    _now: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async-generator that streams Deep Research artifact deltas as discrete events.

    Each yielded event is a JSON-serialisable dict with at minimum:
      - `event`: one of step/claim/source_note/lane_summary/reflection/
        controller_trace/report/final/heartbeat
      - `task_id`
      - `timestamp` (ISO8601)
      - `payload`: the decoded record (for line-oriented files) or text body

    The generator terminates when:
      - `final.json` has been emitted, OR
      - `deadline_seconds` is exceeded since the first call, OR
      - the caller closes / cancels the iterator.

    `cursors` lets a reconnecting caller resume past previously-seen records.
    """
    import time as _time_mod
    from datetime import datetime, timezone

    artifact_dir = _deep_research_dir(workspace, task_id)
    cursors = dict(cursors or {})
    started_at = (_now or _time_mod.monotonic)()
    final_emitted = False
    last_report_text = ""

    def _ts() -> str:
        return datetime.now(timezone.utc).isoformat()

    while True:
        emitted_any = False

        for event_name, filename in _STREAM_FILES:
            path = artifact_dir / filename
            if not path.exists():
                continue
            try:
                lines = [line for line in path.read_text("utf-8").splitlines() if line.strip()]
            except OSError:
                continue
            cursor = cursors.get(event_name, 0)
            if cursor >= len(lines):
                continue
            for line in lines[cursor:]:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {"raw": line}
                yield {
                    "event": event_name,
                    "task_id": task_id,
                    "timestamp": _ts(),
                    "payload": payload,
                }
                emitted_any = True
            cursors[event_name] = len(lines)

        report_path = artifact_dir / "report.md"
        if report_path.exists():
            try:
                text = report_path.read_text("utf-8")
            except OSError:
                text = ""
            if text != last_report_text:
                yield {
                    "event": "report",
                    "task_id": task_id,
                    "timestamp": _ts(),
                    "payload": {"markdown": text, "chars": len(text)},
                }
                last_report_text = text
                emitted_any = True

        final_path = artifact_dir / "final.json"
        if final_path.exists() and not final_emitted:
            try:
                final_payload = json.loads(final_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                final_payload = {}
            yield {
                "event": "final",
                "task_id": task_id,
                "timestamp": _ts(),
                "payload": final_payload,
            }
            final_emitted = True
            return

        if not emitted_any:
            yield {
                "event": "heartbeat",
                "task_id": task_id,
                "timestamp": _ts(),
                "payload": {"cursors": dict(cursors)},
            }

        if deadline_seconds is not None:
            elapsed = (_now or _time_mod.monotonic)() - started_at
            if elapsed >= deadline_seconds:
                return

        await asyncio.sleep(poll_interval_seconds)
