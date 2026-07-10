"""Shared Workflow preview binding store.

Both the AgentTool path and the REST/UI path must prove that a workflow start
matches a prior preview of the same definition and args. REST starts require a
fresh preview_id; AgentTool starts may keep the historical hash fallback for
resume/replay robustness.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.runtime.workflow_admission import normalize_workflow_args
from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_definition import compute_definition_hash

WORKFLOW_PREVIEW_TTL_SECONDS = 60 * 60
_WORKFLOW_PREVIEW_CACHE: dict[str, dict[str, Any]] = {}


def prune_workflow_preview_cache(now: float | None = None) -> None:
    now = now or time.monotonic()
    expired = [
        preview_id
        for preview_id, record in _WORKFLOW_PREVIEW_CACHE.items()
        if now - float(record.get("created_at", 0.0)) > WORKFLOW_PREVIEW_TTL_SECONDS
    ]
    for preview_id in expired:
        _WORKFLOW_PREVIEW_CACHE.pop(preview_id, None)


def record_workflow_preview(
    *,
    agent_id: uuid.UUID,
    definition_hash: str,
    args_hash: str,
    confirmation_required: bool,
    proposal_id: str | None = None,
    candidate_id: str | None = None,
    dynamic_candidate: dict[str, Any] | None = None,
) -> str:
    prune_workflow_preview_cache()
    preview_id = str(uuid.uuid4())
    _WORKFLOW_PREVIEW_CACHE[preview_id] = {
        "agent_id": str(agent_id),
        "definition_hash": definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation_required,
        "proposal_id": proposal_id,
        "candidate_id": candidate_id,
        "dynamic_candidate": dynamic_candidate,
        "created_at": time.monotonic(),
    }
    return preview_id


def get_workflow_preview(preview_id: str | None) -> dict[str, Any] | None:
    if not preview_id:
        return None
    prune_workflow_preview_cache()
    record = _WORKFLOW_PREVIEW_CACHE.get(preview_id)
    return dict(record) if record else None


def validate_workflow_preview_binding(
    *,
    agent_id: uuid.UUID,
    definition: dict,
    args: dict,
    preview_id: str | None,
    expected_definition_hash: str | None = None,
    expected_args_hash: str | None = None,
    allow_hash_fallback: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    compiled = compile_workflow(definition)
    actual_definition_hash = compiled.definition_hash
    args = normalize_workflow_args(compiled, args)
    actual_args_hash = compute_definition_hash(args)

    if preview_id:
        record = get_workflow_preview(preview_id)
        if record is None:
            return (
                False,
                "start_workflow requires a fresh preview_workflow result; preview_id is unknown or expired",
                None,
            )
        if record.get("agent_id") != str(agent_id):
            return False, "start_workflow preview_id belongs to another agent", None
        if record.get("definition_hash") != actual_definition_hash or record.get("args_hash") != actual_args_hash:
            return False, "start_workflow definition/args differ from the preview_workflow artifact", None
        return True, "", record

    if not allow_hash_fallback:
        return False, "start_workflow requires preview_workflow first; pass preview_id from preview_workflow", None

    if expected_definition_hash or expected_args_hash:
        if not expected_definition_hash or not expected_args_hash:
            return False, "start_workflow requires both definition_hash and args_hash when preview_id is omitted", None
        if expected_definition_hash != actual_definition_hash:
            return False, "start_workflow definition_hash does not match the supplied definition", None
        if expected_args_hash != actual_args_hash:
            return False, "start_workflow args_hash does not match the supplied args", None
        return True, "", None

    return False, "start_workflow requires preview_workflow first; pass preview_id from preview_workflow", None
