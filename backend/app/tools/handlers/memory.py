"""Memory tools — agent-initiated memory read/write and cross-session search."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services.session_recall import SessionRecallError, search_session_history
from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool


# -- save_memory ---------------------------------------------------------------


def _coerce_tenant_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


@tool(
    ToolMeta(
        name="save_memory",
        description=(
            "Persist an explicit user-commanded memory so it is available in future conversations. "
            "This writes to the Explicit Memory Overlay immediately; accepted T3 files are updated later "
            "by the T3 Consolidation Batch and Platform Gate. Direct file edits under memory/ are refused.\n\n"
            "Use this tool ONLY when the user explicitly asks you to remember, correct, "
            "never repeat, or follow something in future sessions. Do not infer durable "
            "memory from ordinary conversation or tool output.\n\n"
            "Category routing for explicit user-commanded memory:\n"
            "- User corrections or preferences → feedback\n"
            "- Project decisions or deadlines the user asked you to remember → project\n"
            "- Reusable approaches the user explicitly wants reused → strategy\n"
            "- Proven failure patterns the user explicitly wants avoided → blocked_pattern\n"
            "- Hard future-behavior rules → constraint\n"
            "- External references the user explicitly wants remembered → reference\n"
            "- User role, knowledge, or working style the user explicitly wants remembered → user\n\n"
            "Each fact should be a single, concise statement (under 200 chars is ideal).\n"
            "Do NOT store episodic observations — routine scan/poll results, "
            "'no change' logs, one-off event records, or transient task state. Those "
            "belong in your session log (workspace/T0), not durable memory; save the "
            "durable rule behind them instead (not 'today's scan found no changes' but "
            "'this scan cadence catches changes fastest'). "
            "Do NOT store raw tool output or debugging logs.\n"
            "Do NOT use save_memory to submit skill/workflow/soul/T3 candidates. "
            "Those must be LLM-authored T3 Consolidator artifacts submitted through "
            "submit_t3_consolidation_pitch or submit_t3_revised_patch."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact to remember. Keep concise and durable.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "user",
                        "feedback",
                        "project",
                        "reference",
                        "constraint",
                        "strategy",
                        "blocked_pattern",
                        "general",
                    ],
                    "description": "Memory category for retrieval prioritization.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional topic/subject tag for grouping related facts.",
                },
                "container_candidate": {
                    "type": "string",
                    "enum": [
                        "memory_append",
                        "soul_candidate",
                        "skill_candidate",
                        "workflow_candidate",
                        "artifact_only",
                    ],
                    "description": (
                        "Deprecated compatibility hint; do not use for new skill/workflow/soul/T3 "
                        "candidate routing. New candidates belong in T3 Consolidator pitch/revised_patch "
                        "artifacts, not save_memory. If supplied by legacy callers, this is stored only "
                        "as explicit-overlay metadata and does not command promotion."
                    ),
                },
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional evidence pointers (T2 entry ids, artifact paths, session ids).",
                },
            },
            "required": ["content", "category"],
        },
        category="memory",
        display_name="Save Memory",
        icon="\U0001f9e0",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
        max_result_chars=RESULT_CHARS_UNLIMITED,
    )
)
async def save_memory(agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None, *, execution_context=None) -> str:
    # Closure A3: the third positional parameter makes the agent_args adapter
    # pass request.context.tenant_id so governed memory writes retain tenant
    # context for the optional enhancement adapter boundary.
    from app.config import get_settings
    from app.memory.explicit_overlay import write_explicit_memory_overlay

    content = (arguments.get("content") or "").strip()
    if not content:
        return "[Error] content is required and cannot be empty."

    raw_refs = arguments.get("source_refs")
    source_refs = [str(ref).strip() for ref in raw_refs if str(ref).strip()] if isinstance(raw_refs, list) else []
    source_refs.append("tool:save_memory")

    if execution_context is not None:
        from app.services.explicit_memory_authority import save_scoped_explicit_memory
        from app.tools.runtime import ToolExecutionRequest

        try:
            result = await save_scoped_explicit_memory(
                ToolExecutionRequest("save_memory", arguments, execution_context),
                data_root=Path(get_settings().AGENT_DATA_DIR),
            )
        except Exception as exc:
            return await _record_memory_error("save_memory", agent_id, arguments, execution_context, exc)
    else:
        # Offline/internal callers retain the content helper. Its output has
        # no runtime source receipt and cannot grant prompt access.
        result = await write_explicit_memory_overlay(
            agent_id, category=arguments.get("category", "general"), content=content,
            source_refs=source_refs, tenant_id=_coerce_tenant_uuid(tenant_id),
            data_root=Path(get_settings().AGENT_DATA_DIR),
        )

    if isinstance(result, dict):
        import json

        return await _record_memory_output("save_memory", agent_id, arguments, execution_context, json.dumps(result))
    if result.status == "rejected":
        content = f"[Rejected] {result.sensitivity}: {result.reason}"
        return (await _record_memory_output("save_memory", agent_id, arguments, execution_context, content)
            if execution_context is not None else content)
    if result.status == "held":
        content = f"[Held] {result.sensitivity}: {result.reason}"
        return (await _record_memory_output("save_memory", agent_id, arguments, execution_context, content)
            if execution_context is not None else content)
    if result.status == "duplicate" and execution_context is None:
        return (
            f"[Skipped] Exact explicit memory already exists ({result.entry_id}) "
            f"in {result.target_hint}: {result.content}"
        )

    content = f"Saved to explicit memory overlay [{result.category}] ({result.target_hint}): {result.content}"
    if execution_context is not None:
        return await _record_memory_output("save_memory", agent_id, arguments, execution_context, content,
            entry_id=result.entry_id)
    return content


async def _record_memory_output(tool_name, agent_id, arguments, context, content, *, entry_id=None, entries=(), team_entries=(), unbound=()):
    from app.services.explicit_memory_tool_sources import record_memory_result
    from app.tools.runtime import ToolExecutionRequest

    try:
        if entry_id is not None:
            entries = await _scoped_explicit_entries(agent_id, context, ids=[entry_id])
            if len(entries) != 1:
                from app.services.content_input_receipts import ContentInputReceiptError

                raise ContentInputReceiptError("explicit_memory_source_changed", "The written source needs reconciliation.")
        await record_memory_result(ToolExecutionRequest(tool_name, arguments, context),
            content=content, entries=entries, team_entries=team_entries, unbound=unbound)
        return content
    except Exception as exc:
        return _memory_authority_error(tool_name, exc)


async def _record_memory_error(tool_name, agent_id, arguments, context, error):
    # This response contains only typed mechanical status and an owned opaque
    # recovery reference. It discloses no failed source's content.
    from app.services.explicit_memory_tool_sources import record_memory_result
    from app.tools.runtime import ToolExecutionRequest

    content = _memory_authority_error(tool_name, error)
    try:
        await record_memory_result(ToolExecutionRequest(tool_name, arguments, context), content=content)
    except Exception:
        # A failed observation cannot replace the original operation's recovery
        # reference or grant complete downstream source coverage.
        pass
    return content


def _memory_authority_error(tool_name, error):
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.tools.result_envelope import render_tool_error

    status = error.status_code if isinstance(error, ContentInputReceiptError) else 503
    code = error.code if isinstance(error, ContentInputReceiptError) else "explicit_memory_operation_unknown"
    outcome = ("unknown" if not isinstance(error, ContentInputReceiptError) or code.endswith("unknown")
        else "approval_required" if code == "explicit_memory_approval_required" else "denied" if status == 403 else "unavailable")
    hint = ("Return to the group where this memory was recorded to correct or retire it. Private work cannot disclose a correction to that group."
        if code == "explicit_memory_group_context_mismatch" else
        "Restore current source authority or reconcile the retained write plan before retrying the same command.")
    return render_tool_error(tool_name=tool_name, error_class="auth_or_permission" if status == 403 else "provider_unavailable",
        message="This memory operation could not verify its source or commit state.", provider="explicit_memory",
        http_status=status, retryable=status == 503,
        actionable_hint=hint,
        extra={"reason_code": code, "outcome": outcome, "error_type": type(error).__name__,
            **({"operation_id": error.operation_id,
                "recovery_tool": "reconcile_memory_write"} if getattr(error, "operation_id", None) else {})})


# -- update_memory / retire_memory -------------------------------------------


def _t3_job_artifact_result(path: Path) -> str:
    return f"Submitted T3 job artifact: {path.relative_to(path.parents[4]).as_posix() if len(path.parents) > 4 else path.as_posix()}"


_T3_REVIEW_STALE_MARKER = ".review_stale_after_patch"


def _maybe_apply_t3_platform_gate(*, agent_id: uuid.UUID, data_root: Path, job_id: str) -> str | None:
    from app.memory.md_store import memory_dir
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    job_dir = memory_dir(data_root, agent_id) / ".staging" / "t3_jobs" / job_id
    review_path = job_dir / "review.md"
    patch_path = job_dir / "revised_patch.md"
    if not review_path.exists() or not patch_path.exists():
        return None
    if (job_dir / _T3_REVIEW_STALE_MARKER).exists():
        return f"Platform Gate waiting for fresh Memory Gate review after revised_patch for T3 job {job_id}."
    review_md = review_path.read_text(encoding="utf-8", errors="replace")
    patch_md = patch_path.read_text(encoding="utf-8", errors="replace")
    if "<memory_gate_review" not in review_md or "<t3_consolidation_patch" not in patch_md:
        return None
    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=data_root,
        job_id=job_id,
        revised_patch_md=patch_md,
        review_md=review_md,
    )
    if result.status == "committed":
        return (
            f"Platform Gate committed T3 job {job_id}: "
            f"paths={list(result.committed_paths)} blocks={list(result.committed_blocks)}"
        )
    if result.status == "rebase_required":
        return f"Platform Gate requires rebase for T3 job {job_id}: {', '.join(result.issues)}"
    return f"Platform Gate held T3 job {job_id}: {', '.join(result.issues)}"


@tool(
    ToolMeta(
        name="submit_t3_consolidation_pitch",
        description=(
            "Submit the LLM-authored consolidation_pitch.md for a staged T3 consolidation job. "
            "This writes only to memory/.staging/t3_jobs/<job_id>/consolidation_pitch.md; "
            "it never writes accepted T3 files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "T3 consolidation job id."},
                "content": {"type": "string", "description": "Full Markdown pitch content."},
            },
            "required": ["job_id", "content"],
        },
        category="memory",
        display_name="Submit T3 Consolidation Pitch",
        icon="\U0001f9e0",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
    )
)
def submit_t3_consolidation_pitch(
    agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None
) -> str:
    from app.config import get_settings
    from app.memory.t3_consolidation import write_t3_job_artifact

    del tenant_id
    job_id = (arguments.get("job_id") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not job_id:
        return "[Error] job_id is required."
    if not content:
        return "[Error] content is required."
    try:
        data_root = Path(get_settings().AGENT_DATA_DIR)
        path = write_t3_job_artifact(
            agent_id=agent_id,
            data_root=data_root,
            job_id=job_id,
            filename="consolidation_pitch.md",
            content=content,
        )
    except Exception as exc:
        return f"[Error] submit_t3_consolidation_pitch failed: {exc}"
    return _t3_job_artifact_result(path)


@tool(
    ToolMeta(
        name="submit_t3_memory_gate_review",
        description=(
            "Submit the Memory Gate Agent review.md for a staged T3 consolidation job. "
            "Use this for accept/revise/hold/reject review artifacts and rubric scores. "
            "This must review the latest revised_patch.md before accepted T3 can be committed. "
            "If revised_patch.md is already ready, Platform Gate is attempted immediately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "T3 consolidation job id."},
                "content": {"type": "string", "description": "Full Markdown review artifact."},
            },
            "required": ["job_id", "content"],
        },
        category="memory",
        display_name="Submit T3 Memory Gate Review",
        icon="\U0001f9e0",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
    )
)
def submit_t3_memory_gate_review(agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None) -> str:
    from app.config import get_settings
    from app.memory.t3_consolidation import write_t3_job_artifact

    del tenant_id
    job_id = (arguments.get("job_id") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not job_id:
        return "[Error] job_id is required."
    if not content:
        return "[Error] content is required."
    try:
        data_root = Path(get_settings().AGENT_DATA_DIR)
        path = write_t3_job_artifact(
            agent_id=agent_id,
            data_root=data_root,
            job_id=job_id,
            filename="review.md",
            content=content,
        )
        stale_marker = path.parent / _T3_REVIEW_STALE_MARKER
        if stale_marker.exists():
            stale_marker.unlink()
    except Exception as exc:
        return f"[Error] submit_t3_memory_gate_review failed: {exc}"
    gate_result = _maybe_apply_t3_platform_gate(agent_id=agent_id, data_root=data_root, job_id=job_id)
    if gate_result:
        return f"{_t3_job_artifact_result(path)}\n{gate_result}"
    return _t3_job_artifact_result(path)


@tool(
    ToolMeta(
        name="submit_t3_revised_patch",
        description=(
            "Submit the final LLM-authored revised_patch.md for a staged T3 consolidation job. "
            "Submitting or changing revised_patch.md makes any older Memory Gate review stale; "
            "a fresh review must follow before Platform Gate can atomically apply the exact XML blocks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "T3 consolidation job id."},
                "content": {"type": "string", "description": "Full Markdown revised patch content."},
            },
            "required": ["job_id", "content"],
        },
        category="memory",
        display_name="Submit T3 Revised Patch",
        icon="\U0001f9e0",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
    )
)
def submit_t3_revised_patch(agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None) -> str:
    from app.config import get_settings
    from app.memory.t3_consolidation import write_t3_job_artifact

    del tenant_id
    job_id = (arguments.get("job_id") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not job_id:
        return "[Error] job_id is required."
    if not content:
        return "[Error] content is required."
    try:
        data_root = Path(get_settings().AGENT_DATA_DIR)
        path = write_t3_job_artifact(
            agent_id=agent_id,
            data_root=data_root,
            job_id=job_id,
            filename="revised_patch.md",
            content=content,
        )
        review_path = path.parent / "review.md"
        if review_path.exists():
            (path.parent / _T3_REVIEW_STALE_MARKER).write_text(
                "Memory Gate review predates revised_patch; submit a fresh review before Platform Gate commit.\n",
                encoding="utf-8",
            )
    except Exception as exc:
        return f"[Error] submit_t3_revised_patch failed: {exc}"
    gate_result = _maybe_apply_t3_platform_gate(agent_id=agent_id, data_root=data_root, job_id=job_id)
    if gate_result:
        return f"{_t3_job_artifact_result(path)}\n{gate_result}"
    return _t3_job_artifact_result(path)


async def _sync_memory_enhancement_after_memory_mutation(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    *,
    data_root: Path,
) -> None:
    try:
        from app.memory.enhancement import sync_t3_to_memory_enhancement

        await sync_t3_to_memory_enhancement(agent_id, _coerce_tenant_uuid(tenant_id), data_root=data_root)
    except Exception:
        # T3 markdown + lifecycle sidecar remain the durable source of truth.
        return


def _load_visible_t3_entry(data_root: Path, agent_id: uuid.UUID, entry_id: str):
    from types import SimpleNamespace

    from app.memory.plane_read import load_plane_entries

    entries = load_plane_entries(data_root, agent_id, [entry_id])
    if not entries:
        return None
    entry = entries[0]
    return SimpleNamespace(
        entry_id=entry["id"],
        content=entry["content"],
        category=entry["category"],
        source=entry["source"],
        timestamp=entry.get("timestamp", ""),
        metadata={},
    )


@tool(
    ToolMeta(
        name="update_memory",
        description=(
            "Replace an existing long-term memory entry by ID when the user gives an explicit correction. "
            "This writes the replacement through the Memory Control Plane, then archives the old entry with "
            "a supersedes/superseded_by audit edge. Use search_memory/load_memory first to get the memory_id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Stable id= value returned by search_memory/load_memory for the entry to replace.",
                },
                "content": {
                    "type": "string",
                    "description": "Corrected durable fact. Keep one fact per call.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "user",
                        "feedback",
                        "project",
                        "reference",
                        "constraint",
                        "strategy",
                        "blocked_pattern",
                        "general",
                    ],
                    "description": "Optional replacement category. Defaults to the old entry category.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short evidence-backed reason, e.g. explicit user correction.",
                },
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional evidence pointers supporting the correction.",
                },
            },
            "required": ["memory_id", "content"],
        },
        category="memory",
        display_name="Update Memory",
        icon="\U0001f9e0",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
        max_result_chars=RESULT_CHARS_UNLIMITED,
    )
)
async def update_memory(agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None, *, execution_context=None) -> str:
    from app.config import get_settings
    from app.memory.explicit_overlay import (
        load_explicit_overlay_entries_by_ids,
        update_explicit_overlay_status,
        write_explicit_memory_overlay,
    )
    from app.memory.t3_platform_gate import is_accepted_t3_target

    memory_id = (arguments.get("memory_id") or arguments.get("id") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not memory_id:
        return "[Error] memory_id is required."
    if not content:
        return "[Error] content is required and cannot be empty."

    if execution_context is not None:
        from app.services.explicit_memory_operations import execute_memory_operation
        from app.tools.runtime import ToolExecutionRequest

        try:
            result = await execute_memory_operation(ToolExecutionRequest("update_memory", arguments, execution_context),
                data_root=Path(get_settings().AGENT_DATA_DIR))
            if isinstance(result, dict):
                import json
                return await _record_memory_output("update_memory", agent_id, arguments, execution_context, json.dumps(result))
            if result.status in {"held", "rejected"}:
                return await _record_memory_output("update_memory", agent_id, arguments, execution_context,
                    f"[{result.status.title()}] {result.reason}")
            content = f"Updated explicit memory {memory_id} -> {result.entry_id} [{result.category}]: {result.content}"
            return await _record_memory_output("update_memory", agent_id, arguments, execution_context,
                content, entry_id=result.entry_id)
        except Exception as exc:
            return await _record_memory_error("update_memory", agent_id, arguments, execution_context, exc)

    data_root = Path(get_settings().AGENT_DATA_DIR)
    old_entry = _load_visible_t3_entry(data_root, agent_id, memory_id)
    if old_entry is None:
        explicit_entries = load_explicit_overlay_entries_by_ids(data_root, agent_id, [memory_id])
        explicit_entry = explicit_entries[0] if explicit_entries else None
        if explicit_entry is not None and explicit_entry.status == "active":
            raw_refs = arguments.get("source_refs")
            source_refs = (
                [str(ref).strip() for ref in raw_refs if str(ref).strip()] if isinstance(raw_refs, list) else []
            )
            source_refs.extend([f"memory:{memory_id}", "tool:update_memory"])
            result = await write_explicit_memory_overlay(
                agent_id,
                category=arguments.get("category") or explicit_entry.category or "general",
                content=content,
                source_refs=source_refs,
                tenant_id=_coerce_tenant_uuid(tenant_id),
                data_root=data_root,
                target_hint=explicit_entry.target_hint,
                origin="explicit_user_correction",
            )
            if result.status == "rejected":
                return f"[Rejected] {result.sensitivity}: {result.reason}"
            if result.status == "held":
                return f"[Held] {result.sensitivity}: {result.reason}"
            update_explicit_overlay_status(
                data_root,
                agent_id,
                memory_id,
                status="retired",
                reason=f"superseded_by:{result.entry_id}",
                accepted_blocks=[],
            )
            return f"Updated explicit memory {memory_id} -> {result.entry_id} [{result.category}]: {content}"
    if old_entry is None:
        return f"[Error] Memory entry not found or not visible: {memory_id}"
    if is_accepted_t3_target(old_entry.source):
        return (
            f"[Needs T3 Patch] Accepted T3 memory {memory_id} lives in {old_entry.source}. "
            "Create a T3 Consolidation revised_patch with replace_block/supersede evidence; "
            "Platform Gate must commit accepted T3 changes."
        )
    return (
        f"[Needs T3 Patch] Memory {memory_id} is not an explicit overlay entry. "
        "Use the T3 consolidation lane for accepted memory corrections."
    )


@tool(
    ToolMeta(
        name="retire_memory",
        destructive=True,
        description=(
            "Retire an obsolete or incorrect long-term memory entry by ID. The entry is removed from active "
            "recall and preserved in memory/archive.md plus memory/control/lifecycle.json; it is never physically deleted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Stable id= value returned by search_memory/load_memory for the entry to retire.",
                },
                "reason": {
                    "type": "string",
                    "enum": ["obsolete", "incorrect", "user_requested", "privacy", "superseded", "archived"],
                    "description": "Why the entry should leave active recall.",
                },
            },
            "required": ["memory_id", "reason"],
        },
        category="memory",
        display_name="Retire Memory",
        icon="\U0001f5c4",
        read_only=False,
        parallel_safe=False,
        governance="sensitive",
        adapter="agent_args",
        max_result_chars=RESULT_CHARS_UNLIMITED,
    )
)
async def retire_memory(agent_id: uuid.UUID, arguments: dict, tenant_id: uuid.UUID | str | None = None, *, execution_context=None) -> str:
    from app.config import get_settings
    from app.memory.explicit_overlay import load_explicit_overlay_entries_by_ids, update_explicit_overlay_status
    from app.memory.t3_platform_gate import is_accepted_t3_target

    memory_id = (arguments.get("memory_id") or arguments.get("id") or "").strip()
    reason = (arguments.get("reason") or "").strip().lower()
    if not memory_id:
        return "[Error] memory_id is required."
    if not reason:
        return "[Error] reason is required."

    if execution_context is not None:
        import json
        from app.services.explicit_memory_operations import execute_memory_operation
        from app.tools.runtime import ToolExecutionRequest

        try:
            result = await execute_memory_operation(ToolExecutionRequest("retire_memory", arguments, execution_context),
                data_root=Path(get_settings().AGENT_DATA_DIR))
            return await _record_memory_output("retire_memory", agent_id, arguments, execution_context, json.dumps(result))
        except Exception as exc:
            return await _record_memory_error("retire_memory", agent_id, arguments, execution_context, exc)

    data_root = Path(get_settings().AGENT_DATA_DIR)
    old_entry = _load_visible_t3_entry(data_root, agent_id, memory_id)
    if old_entry is None:
        explicit_entries = load_explicit_overlay_entries_by_ids(data_root, agent_id, [memory_id])
        explicit_entry = explicit_entries[0] if explicit_entries else None
        if explicit_entry is not None and explicit_entry.status == "active":
            update_explicit_overlay_status(data_root, agent_id, memory_id, status="retired", reason=reason)
            return f"Retired explicit memory {memory_id}: {reason}"
    if old_entry is None:
        return f"[Error] Memory entry not found or not visible: {memory_id}"
    if is_accepted_t3_target(old_entry.source):
        return (
            f"[Needs T3 Patch] Accepted T3 memory {memory_id} lives in {old_entry.source}. "
            "Retirement requires a T3 revised_patch with retire_block/supersede evidence and Platform Gate commit."
        )
    return (
        f"[Needs T3 Patch] Memory {memory_id} is not an explicit overlay entry. "
        "Use the T3 consolidation lane for accepted memory retirement."
    )


@tool(ToolMeta(
    name="reconcile_memory_write",
    description=("Inspect, retry the exact retained file/authority commit, or cancel your own unresolved explicit-memory write. "
        "Use the operation_id from its receipt, or omit it with action=status to discover your unfinished writes after a lost response. "
        "Retry never regenerates semantic content. An unresolved semantic review "
        "can only be inspected or cancelled; do not issue a replacement generation without a new explicit request."),
    parameters={"type": "object", "properties": {
        "operation_id": {"type": "string"},
        "action": {"type": "string", "enum": ["status", "retry", "cancel"]}},
        "required": ["action"], "additionalProperties": False},
    category="memory", display_name="Recover Memory Write", icon="\U0001f9e0",
    read_only=False, parallel_safe=False, governance="sensitive", adapter="agent_args",
    max_result_chars=RESULT_CHARS_UNLIMITED,
))
async def reconcile_memory_write(agent_id, arguments, tenant_id=None, *, execution_context=None):
    import json
    from app.config import get_settings
    from app.services.explicit_memory_operations import reconcile_memory_operation
    from app.tools.runtime import ToolExecutionRequest

    if execution_context is None:
        return "[Unavailable] A current authenticated work context is required for memory recovery."
    try:
        result = await reconcile_memory_operation(
            ToolExecutionRequest("reconcile_memory_write", arguments, execution_context),
            operation_id=arguments.get("operation_id"), action=arguments["action"],
            data_root=Path(get_settings().AGENT_DATA_DIR))
        if isinstance(result, dict):
            return await _record_memory_output("reconcile_memory_write", agent_id, arguments, execution_context, json.dumps(result))
        if result.status in {"held", "rejected"}:
            return await _record_memory_output("reconcile_memory_write", agent_id, arguments, execution_context,
                f"[{result.status.title()}] {result.reason}")
        return await _record_memory_output("reconcile_memory_write", agent_id, arguments, execution_context,
            f"Saved to explicit memory overlay [{result.category}] ({result.target_hint}): {result.content}", entry_id=result.entry_id)
    except Exception as exc:
        return await _record_memory_error("reconcile_memory_write", agent_id, arguments, execution_context, exc)


# -- load_memory ---------------------------------------------------------------


@tool(
    ToolMeta(
        name="load_memory",
        description=(
            "Load full long-term memory entries by ID after search_memory or the prompt memory index returns IDs.\n\n"
            "Use this before relying on an indexed/preview-only memory entry. Supports batch IDs, including exact "
            "team_memory: workspace references returned by search. Team reads use current membership and source access."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Memory entry IDs returned by search_memory or the prompt memory index.",
                }
            },
            "required": ["ids"],
        },
        category="memory",
        display_name="Load Memory",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
        max_result_chars=RESULT_CHARS_UNLIMITED,
    )
)
def load_memory(agent_id: uuid.UUID, arguments: dict, *, execution_context=None, _authorized_explicit_entries=None) -> str:
    if execution_context is not None:
        return _load_scoped_memory(agent_id, arguments, execution_context)
    from pathlib import Path

    from app.config import get_settings
    from app.memory.explicit_overlay import load_explicit_overlay_entries_by_ids
    from app.memory.plane_read import load_plane_entries

    raw_ids = arguments.get("ids") or []
    if isinstance(raw_ids, str):
        ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    else:
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if not ids:
        return "[Error] ids is required and cannot be empty."

    settings = get_settings()
    data_root = Path(settings.AGENT_DATA_DIR)
    all_explicit_entries = load_explicit_overlay_entries_by_ids(data_root, agent_id, ids)
    explicit_ids = {entry.entry_id for entry in all_explicit_entries}
    explicit_entries = all_explicit_entries if _authorized_explicit_entries is None else _authorized_explicit_entries
    from types import SimpleNamespace

    entries = [
        SimpleNamespace(
            entry_id=row["id"],
            content=row["content"],
            category=row["category"],
            source=row["source"],
            timestamp=row.get("timestamp", ""),
            metadata={},
        )
        for row in load_plane_entries(
            data_root, agent_id, [entry_id for entry_id in ids if entry_id not in explicit_ids]
        )
    ]
    if not entries and not explicit_entries:
        return f"No memory entries found for ids: {', '.join(ids)}"

    visible_entries = []
    suppressed_ids = []
    for entry in entries:
        if _memory_metadata_visible(entry.metadata):
            visible_entries.append(entry)
        else:
            suppressed_ids.append(entry.entry_id)

    found_ids = {entry.entry_id for entry in visible_entries} | explicit_ids
    if not visible_entries and not explicit_entries:
        lines = ["No visible memory entries found."]
    else:
        lines = ["## Loaded Memory"]
    if explicit_entries:
        lines.append("## Loaded Explicit Memory Overlay")
        for entry in explicit_entries:
            if entry.status != "active":
                continue
            lines.append(
                f"- id={entry.entry_id} source=memory/explicit/entries/{entry.entry_id}.md "
                f"category={entry.category} target_hint={entry.target_hint}"
            )
            lines.append(f"  {entry.content}")
            if entry.source_refs:
                lines.append(f"  source_refs: {', '.join(entry.source_refs)}")
    for entry in visible_entries:
        ts = f" timestamp={entry.timestamp}" if entry.timestamp else ""
        lines.append(f"- id={entry.entry_id} source={entry.source} category={entry.category}{ts}")
        lines.append(f"  {entry.content}")
    missing = [entry_id for entry_id in ids if entry_id not in found_ids and entry_id not in suppressed_ids]
    if missing:
        lines.append("")
        lines.append(f"Missing ids: {', '.join(missing)}")
    if suppressed_ids:
        lines.append("")
        lines.append(f"Suppressed entries: {len(suppressed_ids)}")
    return "\n".join(lines)


async def _scoped_explicit_entries(agent_id, execution_context, *, ids=None, issue_observer=None):
    from app.config import get_settings
    from app.memory.explicit_overlay import load_explicit_overlay_entries, load_explicit_overlay_entries_by_ids
    from app.services.explicit_memory_authority import authorized_explicit_entries
    from app.tools.runtime import ToolExecutionRequest

    root = Path(get_settings().AGENT_DATA_DIR)
    entries = (load_explicit_overlay_entries(root, agent_id) if ids is None
        else load_explicit_overlay_entries_by_ids(root, agent_id, ids))
    return await authorized_explicit_entries(tenant_id=uuid.UUID(str(execution_context.tenant_id)),
        agent_id=agent_id, request=ToolExecutionRequest("load_memory", {}, execution_context), entries=entries,
        issue_observer=issue_observer)


def _memory_availability(content, issues):
    if not issues:
        return content
    import json

    return content + "\n\n## Memory Availability\n" + json.dumps({
        "schema": "hive.explicit_memory_availability.v1", "status": "partial", "issues": issues})


async def _load_scoped_memory(agent_id, arguments, execution_context):
    raw_ids = arguments.get("ids") or []
    ids = [part.strip() for part in raw_ids.split(",") if part.strip()] if isinstance(raw_ids, str) else [str(value) for value in raw_ids]
    try:
        from app.services.team_memory_runtime import read_entries, render_entry
        from app.tools.runtime import ToolExecutionRequest

        team_ids = [value for value in ids if value.startswith("team_memory:")]
        local_ids = [value for value in ids if value not in team_ids]
        issues = []
        entries = await _scoped_explicit_entries(agent_id, execution_context, ids=local_ids, issue_observer=issues)
        content = load_memory(agent_id, {**arguments, "ids": local_ids},
            _authorized_explicit_entries=[entry for entry, _receipt in entries]) if local_ids else ""
        teams = await read_entries(tenant_id=uuid.UUID(str(execution_context.tenant_id)),
            request=ToolExecutionRequest("load_memory", arguments, execution_context), ids=team_ids, issues=issues) if team_ids else []
        if teams:
            content += "\n\n## Team Memory\n" + "\n".join(render_entry(entry) for entry, _receipt in teams)
        content = _memory_availability(content or "No team memory was found for the supplied current load references.", issues)
        from app.config import get_settings
        from app.memory.explicit_overlay import load_explicit_overlay_entries_by_ids
        from app.memory.plane_read import load_plane_entries

        root = Path(get_settings().AGENT_DATA_DIR)
        explicit_ids = {e.entry_id for e in load_explicit_overlay_entries_by_ids(root, agent_id, local_ids)}
        plane = load_plane_entries(root, agent_id, [entry_id for entry_id in local_ids if entry_id not in explicit_ids])
        return await _record_memory_output("load_memory", agent_id, arguments, execution_context, content,
            entries=entries, team_entries=teams, unbound=("legacy_memory_sources",) if plane else ())
    except Exception as exc:
        return await _record_memory_error("load_memory", agent_id, arguments, execution_context, exc)


# -- search_memory -------------------------------------------------------------


@tool(
    ToolMeta(
        name="search_memory",
        description=(
            "Search your long-term memory and past session history.\n\n"
            "Use this tool when you need to recall:\n"
            "- What a user told you in a previous conversation\n"
            "- Decisions, preferences, or constraints from past sessions\n"
            "- Strategies that worked or approaches that failed\n"
            "- Any fact you saved previously with save_memory\n\n"
            "- Current business-team and work-team memory authorized for this work and its recipients\n\n"
            "Returns every authorized matching fact and recalled session by default, with complete semantic content. "
            "Set limit only when you intentionally want the retrieval layer to return fewer candidates. "
            "Retrieval uses indexed terms and phrases, not a cross-language semantic guarantee. "
            "A query miss is not proof that related memory does not exist; use known IDs/source references "
            "or alternative wording when needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or phrase to find in memory.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["facts", "sessions", "all"],
                    "description": "Search scope: 'facts' (semantic memory only), 'sessions' (past conversation recall), 'all' (both). Default: all.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional explicit maximum results to return; omit for every authorized matching candidate.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Only include facts on or after this date (YYYY-MM-DD). Use get_current_time to resolve relative dates like 'last month'.",
                },
                "date_to": {
                    "type": "string",
                    "description": "Only include facts on or before this date (YYYY-MM-DD).",
                },
            },
            "required": ["query"],
        },
        category="memory",
        display_name="Search Memory",
        icon="\U0001f50d",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="agent_args",
        max_result_chars=RESULT_CHARS_UNLIMITED,
    )
)
async def search_memory(agent_id: uuid.UUID, arguments: dict, tenant_id: str | None = None, *, execution_context=None) -> str:
    import json
    from pathlib import Path

    from app.config import get_settings

    if set(arguments) - {"query", "scope", "limit", "date_from", "date_to"}:
        return json.dumps({"schema": "hive.session_recall_error.v1", "status": "invalid_arguments",
            "code": "session_recall_arguments_invalid", "retryable": False,
            "message": "Unsupported fields cannot select the memory reader."})
    query = (arguments.get("query") or "").strip()
    if not query:
        return "[Error] query is required."

    scope = arguments.get("scope", "all")
    raw_limit = arguments.get("limit")
    if raw_limit is None:
        limit = None
    else:
        try:
            limit = max(1, int(raw_limit))
        except (TypeError, ValueError):
            return "[Error] limit must be a positive integer when provided."
    date_from = (arguments.get("date_from") or "").strip() or None
    date_to = (arguments.get("date_to") or "").strip() or None
    results: list[str] = []
    overlay_pairs = []
    overlay_facts = []
    team_pairs = []
    unbound = []
    issues = []

    settings = get_settings()

    # --- Semantic facts search ---
    if scope in ("facts", "all"):
        from app.memory.explicit_overlay import search_explicit_overlay_entries

        authorized_entries = None
        if execution_context is not None:
            try:
                overlay_pairs = await _scoped_explicit_entries(agent_id, execution_context, issue_observer=issues)
                authorized_entries = [entry for entry, _receipt in overlay_pairs]
            except Exception as exc:
                return await _record_memory_error("search_memory", agent_id, arguments, execution_context, exc)
        overlay_facts = search_explicit_overlay_entries(
            Path(settings.AGENT_DATA_DIR),
            agent_id,
            query,
            limit=limit,
            **({"authorized_entries": authorized_entries} if execution_context is not None else {}),
        )
        overlay_facts = [fact for fact in overlay_facts if _memory_fact_visible(fact)]

        from app.memory.plane_read import search_plane_facts

        facts = search_plane_facts(
            Path(settings.AGENT_DATA_DIR),
            agent_id,
            query,
            limit=limit,
        )
        backend_facts = await _search_semantic_backend_facts(
            agent_id,
            tenant_id=tenant_id,
            query=query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            md_facts=facts,
        )
        if backend_facts:
            facts = _dedupe_fact_results([*backend_facts, *facts])
            if limit is not None:
                facts = facts[:limit]
        facts = [fact for fact in facts if _memory_fact_visible(fact)]
        if facts:
            unbound.append("legacy_memory_sources")
            results.append("## Semantic Memory")
            for f in facts:
                entry_id = f.get("id", "")
                cat = f.get("category", "general")
                content = f.get("content") or f.get("preview", "")
                ts = f.get("timestamp", "")
                ts_display = f" ({ts[:10]})" if ts else ""
                source = f.get("source", "")
                source_display = f" source={source}" if source else ""
                id_display = f"id={entry_id} " if entry_id else ""
                load_hint = f' load_memory(ids=["{entry_id}"])' if entry_id else ""
                results.append(f"- {id_display}[{cat}]{ts_display}{source_display} {content}{load_hint}")

    # --- Cross-session recall ---
    if scope in ("sessions", "all"):
        try:
            from app.tools.runtime import ToolExecutionRequest

            tenant_uuid = uuid.UUID(str(tenant_id)) if tenant_id else None
            recalled = await search_session_history(
                agent_id,
                query,
                limit=limit,
                snippet_limit=3,
                tenant_id=tenant_uuid,
                runtime_request=ToolExecutionRequest(tool_name="search_memory", arguments=arguments,
                    context=execution_context) if execution_context is not None else None,
            )
            if recalled:
                unbound.append("session_recall_sources")
                results.append("## Session Recall")
                for hit in recalled:
                    ts = hit.get("started_at", "?")
                    source = hit.get("source", "unknown")
                    headline = hit.get("headline", "past session")
                    results.append(f"- ({ts} [{source}]) {headline}")
                    complete_transcript = (hit.get("transcript") or "").strip()
                    if complete_transcript:
                        results.append("  Complete transcript:")
                        for line in complete_transcript.splitlines():
                            results.append(f"    {line}")
                    else:
                        # Legacy/degraded hits may lack a canonical transcript.
                        # In that case expose every available evidence projection.
                        focused_recap = (hit.get("focused_recap") or "").strip()
                        if focused_recap:
                            results.append(f"  Recap: {focused_recap}")
                        summary = (hit.get("summary") or "").strip()
                        if summary and summary != focused_recap:
                            results.append(f"  Summary: {summary}")
                        evidence_lines = hit.get("evidence_lines") or []
                        if evidence_lines:
                            results.append("  Evidence:")
                            for line in evidence_lines:
                                cleaned = line.strip()
                                if cleaned:
                                    results.append(f"    {cleaned}")
                        transcript_window = (hit.get("transcript_window") or "").strip()
                        if transcript_window:
                            results.append("  Context:")
                            for line in transcript_window.splitlines():
                                cleaned = line.strip()
                                if cleaned:
                                    results.append(f"    {cleaned}")
                        display_snippets = hit.get("context_snippets") or hit.get("snippets", [])
                        for snippet in display_snippets:
                            results.append(f"  - {snippet}")
        except SessionRecallError as exc:
            if scope == "sessions" and execution_context is not None:
                from app.services.content_input_receipts import ContentInputReceiptError

                return await _record_memory_error("search_memory", agent_id, arguments, execution_context,
                    ContentInputReceiptError(exc.code, str(exc), 403 if exc.status == "denied" else 503))
            results.append("## Session Recall\n" + json.dumps({"schema": "hive.session_recall_error.v1",
                "status": exc.status, "code": exc.code, "retryable": exc.retryable, "message": str(exc)}))
        except Exception:
            results.append("## Session Recall\n" + json.dumps({"schema": "hive.session_recall_error.v1",
                "status": "unavailable", "code": "session_recall_authority_unavailable", "retryable": True,
                "message": "Session recall is temporarily unavailable. Retry later."}))

    if execution_context is not None and overlay_facts:
        from app.services.explicit_memory_authority import authorized_explicit_entries
        from app.tools.runtime import ToolExecutionRequest

        ids = {fact["id"] for fact in overlay_facts}
        overlay_pairs = await authorized_explicit_entries(tenant_id=uuid.UUID(str(execution_context.tenant_id)),
            agent_id=agent_id, request=ToolExecutionRequest("search_memory", arguments, execution_context),
            entries=[entry for entry, _receipt in overlay_pairs if entry.entry_id in ids], issue_observer=issues)
        allowed_ids = {entry.entry_id for entry, _receipt in overlay_pairs}
        overlay_facts = [fact for fact in overlay_facts if fact["id"] in allowed_ids]
    if execution_context is not None and scope in {"facts", "all"}:
        from app.services.team_memory_runtime import read_entries, render_entry
        from app.tools.runtime import ToolExecutionRequest

        try:
            team_pairs = await read_entries(tenant_id=uuid.UUID(str(execution_context.tenant_id)), query=query, issues=issues,
                request=ToolExecutionRequest("search_memory", arguments, execution_context))
            if limit is not None:
                team_pairs = team_pairs[:limit]
            if team_pairs:
                results.append("## Team Memory\n" + "\n".join(render_entry(entry) for entry, _receipt in team_pairs))
        except Exception:
            issues.append({"status": "unavailable", "code": "team_memory_source_unavailable"})
    overlay_lines = []
    if overlay_facts:
        overlay_lines.append("## Explicit Memory Overlay")
        for fact in overlay_facts:
            entry_id = fact["id"]
            overlay_lines.append(f"- id={entry_id} [{fact.get('category', 'general')}] source=explicit_overlay "
                f"target={fact.get('target_hint', 'unknown')} {fact.get('content') or fact.get('preview', '')} "
                f'load_memory(ids=["{entry_id}"])')
    results = [*overlay_lines, *results]
    if not results:
        content = (
            f"No memory found for query: {query} (scope={scope}). "
            "This is a query miss, not proof that related memory does not exist. "
            "Try alternative wording or load_memory with previously returned IDs and follow their source_refs."
        )

    else:
        content = "\n".join(results)
    content = _memory_availability(content, issues)
    if execution_context is not None:
        used_ids = {fact["id"] for fact in overlay_facts}
        return await _record_memory_output("search_memory", agent_id, arguments, execution_context, content,
            entries=[pair for pair in overlay_pairs if pair[0].entry_id in used_ids], team_entries=team_pairs, unbound=unbound)
    return content


async def _search_semantic_backend_facts(
    agent_id: uuid.UUID,
    *,
    tenant_id: str | None,
    query: str,
    limit: int | None,
    date_from: str | None,
    date_to: str | None,
    md_facts: list[dict],
) -> list[dict]:
    del agent_id, tenant_id, query, limit, date_from, date_to, md_facts
    return []


def _dedupe_fact_results(facts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for fact in facts:
        key = str(fact.get("id") or _normalize_fact_content(fact.get("content", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


def _normalize_fact_content(content: object) -> str:
    return " ".join(str(content or "").lower().split())


def _memory_metadata_visible(metadata: dict | None) -> bool:
    from app.memory.visibility import can_access_metadata

    return can_access_metadata(metadata)


def _memory_fact_visible(fact: dict) -> bool:
    sensitivity = fact.get("sensitivity")
    if not sensitivity:
        from app.memory.visibility import classify_text_sensitivity

        sensitivity = classify_text_sensitivity(str(fact.get("content") or fact.get("preview") or ""))
    return _memory_metadata_visible({"sensitivity": sensitivity})


def _wiki_hit_visible(data_root: Path, agent_id: uuid.UUID, hit: dict) -> bool:
    from app.memory.visibility import can_access_sensitivity, classify_text_sensitivity

    text = _wiki_hit_source_text(data_root, agent_id, hit) or " ".join(
        str(hit.get(key) or "") for key in ("title", "kind", "preview", "source_ref")
    )
    return can_access_sensitivity(classify_text_sensitivity(text))


def _wiki_hit_source_text(data_root: Path, agent_id: uuid.UUID, hit: dict) -> str:
    source_ref = str(hit.get("source_ref") or "").replace("\\", "/")
    if not source_ref.startswith("memory/"):
        return ""
    rel_path = Path(source_ref.removeprefix("memory/"))
    if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.suffix.lower() != ".md":
        return ""
    base = (Path(data_root) / str(agent_id) / "memory").resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents:
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
