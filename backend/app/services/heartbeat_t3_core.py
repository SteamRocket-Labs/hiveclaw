"""Direct LLM core for heartbeat-owned T3 consolidation.

Heartbeat is platform infrastructure, not a full agent session. This module
keeps the intelligent part of heartbeat as direct model calls over staged T3
artifacts, then hands physical writes to the existing Platform Gate.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.metrics import record_autonomous_llm_call
from app.memory.t3_consolidation import (
    discover_pending_t3_sources,
    stage_pending_t3_consolidation_job,
    write_t3_job_artifact,
)
from app.memory.t3_platform_gate import apply_t3_consolidation_patch
from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context


_T3_CONSOLIDATOR_TEMPLATE = Path(__file__).parent.parent / "templates" / "T3_CONSOLIDATOR.md"
_MAX_INPUT_CHARS = 120_000
_CONSOLIDATOR_OUTPUT_TOKENS = 16_384
_REVIEW_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class HeartbeatT3CoreResult:
    status: str
    outcome_type: str
    score: int
    summary: str
    job_id: str | None = None
    skip_reason: str | None = None
    platform_gate_status: str | None = None
    issues: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()


async def run_heartbeat_t3_core(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    data_root: Path | str,
    model: Any,
) -> HeartbeatT3CoreResult:
    """Run one direct T3 consolidation tick.

    No tools are registered, no full-agent invocation object is constructed,
    and no agent session is resumed. The only LLM authority here is semantic drafting;
    accepted memory still goes through Platform Gate.
    """

    root = Path(data_root)
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    pending = discover_pending_t3_sources(agent_id=resolved_agent_id, data_root=root)
    if not pending.has_sources:
        record_autonomous_llm_call(source="heartbeat", outcome="skipped")
        return HeartbeatT3CoreResult(
            status="skipped",
            outcome_type="noop",
            score=0,
            summary="No pending T3 sources.",
            skip_reason="no_pending_t3_sources",
        )

    staged = stage_pending_t3_consolidation_job(agent_id=resolved_agent_id, data_root=root)
    if staged.status != "staged":
        record_autonomous_llm_call(source="heartbeat", outcome="skipped")
        _record_job_direct_core_summary(
            job_dir=staged.job_dir,
            status="held",
            summary="T3 job held before LLM consolidation.",
            issues=staged.issues,
        )
        return HeartbeatT3CoreResult(
            status="held",
            outcome_type="failure",
            score=2,
            summary="T3 job held before LLM consolidation.",
            job_id=staged.job_id,
            issues=tuple(staged.issues),
        )

    try:
        consolidation_payload = await _call_consolidator(
            model=model,
            agent_id=resolved_agent_id,
            tenant_id=tenant_id,
            job_dir=staged.job_dir,
            job_id=staged.job_id,
        )
        pitch_md = _required_artifact(consolidation_payload, "consolidation_pitch_md")
        patch_md = _required_artifact(consolidation_payload, "revised_patch_md")
        summary = str(consolidation_payload.get("summary") or "T3 consolidation artifacts drafted.").strip()

        pitch_path = write_t3_job_artifact(
            agent_id=resolved_agent_id,
            data_root=root,
            job_id=staged.job_id,
            filename="consolidation_pitch.md",
            content=patch_heading(pitch_md, "T3 Consolidation Pitch"),
        )
        patch_path = write_t3_job_artifact(
            agent_id=resolved_agent_id,
            data_root=root,
            job_id=staged.job_id,
            filename="revised_patch.md",
            content=patch_heading(patch_md, "T3 Revised Patch"),
        )

        review_payload = await _call_memory_gate_review(
            model=model,
            agent_id=resolved_agent_id,
            tenant_id=tenant_id,
            job_dir=staged.job_dir,
            job_id=staged.job_id,
            consolidation_pitch_md=pitch_path.read_text(encoding="utf-8", errors="replace"),
            revised_patch_md=patch_path.read_text(encoding="utf-8", errors="replace"),
        )
        review_md = _required_artifact(review_payload, "review_md")
        review_path = write_t3_job_artifact(
            agent_id=resolved_agent_id,
            data_root=root,
            job_id=staged.job_id,
            filename="review.md",
            content=patch_heading(review_md, "T3 Memory Gate Review"),
        )

        gate_result = apply_t3_consolidation_patch(
            agent_id=resolved_agent_id,
            data_root=root,
            job_id=staged.job_id,
            revised_patch_md=patch_path.read_text(encoding="utf-8", errors="replace"),
            review_md=review_path.read_text(encoding="utf-8", errors="replace"),
        )
        status = str(getattr(gate_result, "status", "held") or "held")
        issues = tuple(str(issue) for issue in getattr(gate_result, "issues", ()) or ())
        artifact_paths = tuple(
            path.relative_to(root / str(resolved_agent_id)).as_posix()
            for path in (pitch_path, patch_path, review_path)
        )
        committed_paths = tuple(str(path) for path in getattr(gate_result, "committed_paths", ()) or ())
        committed_blocks = tuple(str(block) for block in getattr(gate_result, "committed_blocks", ()) or ())
        final_summary = _gate_summary(summary, status, committed_paths=committed_paths, issues=issues)
        _record_job_direct_core_summary(
            job_dir=staged.job_dir,
            status=status,
            summary=final_summary,
            issues=issues,
            committed_paths=committed_paths,
            committed_blocks=committed_blocks,
            artifact_paths=artifact_paths,
        )
        record_autonomous_llm_call(source="heartbeat", outcome="success")
        return HeartbeatT3CoreResult(
            status=status,
            outcome_type="curated" if status == "committed" else "failure",
            score=7 if status == "committed" else 3,
            summary=final_summary,
            job_id=staged.job_id,
            platform_gate_status=status,
            issues=issues,
            artifact_paths=artifact_paths,
        )
    except Exception as exc:
        record_autonomous_llm_call(source="heartbeat", outcome="failure")
        _record_job_direct_core_summary(
            job_dir=staged.job_dir,
            status="failed",
            summary=f"Heartbeat T3 direct core failed: {type(exc).__name__}",
            issues=(str(exc),),
        )
        raise


def patch_heading(markdown: str, title: str) -> str:
    content = (markdown or "").strip()
    if content.startswith("# "):
        return content
    return f"# {title}\n\n{content}".strip()


async def _call_consolidator(
    *,
    model: Any,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    job_dir: Path,
    job_id: str,
) -> dict[str, Any]:
    system_prompt = _T3_CONSOLIDATOR_TEMPLATE.read_text(encoding="utf-8").strip()
    user_prompt = _truncate(
        "\n\n".join(
            [
                f"job_id: {job_id}",
                "source_bundle.json:",
                _read_job_file(job_dir, "source_bundle.json"),
                "t3_neighborhood.md:",
                _read_job_file(job_dir, "t3_neighborhood.md"),
                "Return JSON only. Do not include Markdown fences outside JSON.",
            ]
        )
    )
    return await _complete_json(
        model=model,
        agent_id=agent_id,
        tenant_id=tenant_id,
        phase="consolidator",
        messages=[
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ],
        max_tokens=_output_tokens(model, _CONSOLIDATOR_OUTPUT_TOKENS),
        temperature=0.2,
    )


async def _call_memory_gate_review(
    *,
    model: Any,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    job_dir: Path,
    job_id: str,
    consolidation_pitch_md: str,
    revised_patch_md: str,
) -> dict[str, Any]:
    system_prompt = (
        "You are the Memory Gate reviewer for a T3 consolidation job. "
        "Review the exact revised_patch.md against source_bundle.json and t3_neighborhood.md. "
        "Return JSON only with key \"review_md\". The review_md must contain "
        "<memory_gate_review schema_version=\"t3.memory_gate_review.v1\"> and a decision."
    )
    user_prompt = _truncate(
        "\n\n".join(
            [
                f"job_id: {job_id}",
                "source_bundle.json:",
                _read_job_file(job_dir, "source_bundle.json"),
                "t3_neighborhood.md:",
                _read_job_file(job_dir, "t3_neighborhood.md"),
                "consolidation_pitch.md:",
                consolidation_pitch_md,
                "revised_patch.md:",
                revised_patch_md,
            ]
        )
    )
    return await _complete_json(
        model=model,
        agent_id=agent_id,
        tenant_id=tenant_id,
        phase="memory_gate_review",
        messages=[
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ],
        max_tokens=_output_tokens(model, _REVIEW_OUTPUT_TOKENS),
        temperature=0.1,
    )


async def _complete_json(
    *,
    model: Any,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    phase: str,
    messages: list[LLMMessage],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    client = create_llm_client_from_config(
        with_llm_usage_context(
            {
                "provider": getattr(model, "provider"),
                "model": getattr(model, "model"),
                "api_key": getattr(model, "api_key"),
                "base_url": getattr(model, "base_url", None),
            },
            source="heartbeat",
            agent_id=agent_id,
            tenant_id=tenant_id,
            metadata={"phase": phase, "runtime": "direct_t3_core"},
        )
    )
    try:
        response = await client.complete(
            messages=messages,
            tools=None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_json_object(response.content or "")
    finally:
        await client.close()


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("heartbeat T3 core expected a JSON object")
    return payload


def _required_artifact(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"heartbeat T3 core response missing {key}")
    return value.strip()


def _read_job_file(job_dir: Path, filename: str) -> str:
    return (job_dir / filename).read_text(encoding="utf-8", errors="replace")


def _truncate(text: str, max_chars: int = _MAX_INPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[... heartbeat direct T3 core input truncated; omitted {len(text) - max_chars:,} chars ...]\n\n"
    head = max_chars // 2
    tail = max_chars - head - len(marker)
    return text[:head] + marker + text[-max(0, tail) :]


def _output_tokens(model: Any, default: int) -> int:
    configured = getattr(model, "max_output_tokens", None)
    if not configured:
        return default
    try:
        return max(1024, min(int(configured), default))
    except (TypeError, ValueError):
        return default


def _gate_summary(
    draft_summary: str,
    status: str,
    *,
    committed_paths: tuple[str, ...],
    issues: tuple[str, ...],
) -> str:
    if status == "committed":
        return f"{draft_summary} Platform Gate committed {len(committed_paths)} path(s)."
    if issues:
        return f"{draft_summary} Platform Gate {status}: {', '.join(issues[:3])}."
    return f"{draft_summary} Platform Gate {status}."


def _record_job_direct_core_summary(
    *,
    job_dir: Path,
    status: str,
    summary: str,
    issues: tuple[str, ...] = (),
    committed_paths: tuple[str, ...] = (),
    committed_blocks: tuple[str, ...] = (),
    artifact_paths: tuple[str, ...] = (),
) -> None:
    manifest_path = job_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    manifest["heartbeat_direct_core"] = {
        "schema_version": "heartbeat.direct_t3_core.v1",
        "status": status,
        "summary": summary,
        "issues": list(issues),
        "committed_paths": list(committed_paths),
        "committed_blocks": list(committed_blocks),
        "artifact_paths": list(artifact_paths),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
