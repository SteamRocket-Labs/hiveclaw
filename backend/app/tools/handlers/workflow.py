"""Workflow tools: preview + start for ephemeral definitions.

The agent submits DEFINITION DATA only (§3.2) — the engine interprets it;
there is no code execution surface. ``preview_workflow`` surfaces deterministic
confirmation notes, but ``start_workflow`` never enters Plan Mode automatically.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.database import tenant_scoped_session
from app.runtime.dynamic_workflow import (
    build_dynamic_workflow_run_metadata,
    dump_json,
    mapping,
    validate_dynamic_workflow_proposal,
)
from app.runtime.context_budget import build_tool_execution_shape_decision, execution_shape_from_round_state
from app.runtime.workflow_admission import (
    AdmissionLimits,
    WorkflowAdmissionError,
    admit_workflow,
    normalize_workflow_args,
)
from app.runtime.workflow_compiler import WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.services.workflow_confirmation_service import (
    WORKFLOW_AGENT_NO_CONFIRMATION_START_SOURCE,
    WorkflowConfirmationConflict,
    WorkflowStartClaim,
    claim_workflow_preview_start,
    create_workflow_preview,
    create_workflow_proposal,
    load_workflow_candidate,
    load_workflow_preview,
    mark_workflow_preview_failed_record,
    mark_workflow_preview_started_record,
    workflow_preview_artifact_payload,
)
from app.services.workflow_launch import inspect_workflow_confirmation_needs, start_ephemeral_workflow_for_agent
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

logger = logging.getLogger(__name__)


_DEFINITION_PARAM = {
    "type": "object",
    "description": (
        "Structured workflow definition (data, not code): name, args_schema, steps "
        "(agent_step/fanout_step/gate_step/wait_until_step). Task strings may reference "
        "{{args.x}} and {{steps.<id>.output}} (pure key substitution)."
    ),
}


def _request_identity(request: ToolExecutionRequest) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    try:
        tenant_id = uuid.UUID(str(request.context.tenant_id or ""))
        session_id = uuid.UUID(str(request.context.session_id or ""))
        user_id = uuid.UUID(str(request.context.user_id))
    except ValueError as exc:
        raise WorkflowConfirmationConflict(
            "missing_workflow_identity",
            "Dynamic Workflow proposal, preview, and start require tenant, user, and current session UUIDs.",
        ) from exc
    return tenant_id, request.context.agent_id, session_id, user_id


async def _persist_dynamic_proposal(request: ToolExecutionRequest, proposal: dict[str, Any]) -> dict[str, Any]:
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="dynamic_workflow_proposal",
    ) as db:
        artifact = await create_workflow_proposal(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            proposal=proposal,
        )
        return dict(artifact.proposal_json or {})


async def _load_dynamic_candidate(
    request: ToolExecutionRequest,
    proposal_id: str | None,
    candidate_id: str | None,
) -> tuple[Any | None, dict[str, Any] | None]:
    if not proposal_id or not candidate_id:
        return None, None
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError as exc:
        raise WorkflowConfirmationConflict("proposal_not_found", "Dynamic Workflow proposal_id is invalid.") from exc
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="dynamic_workflow_candidate",
    ) as db:
        return await load_workflow_candidate(
            db,
            proposal_id=proposal_uuid,
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )


async def _persist_workflow_preview(
    request: ToolExecutionRequest,
    *,
    definition: dict[str, Any],
    args: dict[str, Any],
    definition_hash: str,
    args_hash: str,
    preview_payload: dict[str, Any],
    proposal: Any | None,
    candidate_id: str | None,
) -> dict[str, Any]:
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    proposal_id = getattr(proposal, "id", None)
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="workflow_preview_artifact",
    ) as db:
        attached_proposal = None
        if proposal_id is not None:
            result = await db.get(type(proposal), proposal_id)
            attached_proposal = result
            if attached_proposal is None:
                raise WorkflowConfirmationConflict(
                    "proposal_not_found",
                    "Dynamic Workflow proposal disappeared before preview creation.",
                )
        preview = await create_workflow_preview(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            definition=definition,
            args=args,
            definition_hash=definition_hash,
            args_hash=args_hash,
            preview_payload=preview_payload,
            proposal=attached_proposal,
            candidate_id=candidate_id,
        )
        return workflow_preview_artifact_payload(preview)


async def _claim_workflow_start(request: ToolExecutionRequest, preview_id: uuid.UUID) -> WorkflowStartClaim:
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    evidence_id = str(request.context.turn_id or "").strip()
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="workflow_preview_start_claim",
    ) as db:
        return await claim_workflow_preview_start(
            db,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            confirmation_source=WORKFLOW_AGENT_NO_CONFIRMATION_START_SOURCE,
            confirmation_evidence_id=evidence_id,
        )


async def _finish_workflow_start(
    request: ToolExecutionRequest,
    *,
    preview_id: uuid.UUID,
    claim_token: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="workflow_preview_start_finish") as db:
        preview = await load_workflow_preview(
            db,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            for_update=True,
        )
        mark_workflow_preview_started_record(preview, run_id=run_id, claim_token=claim_token)
        await db.flush()


async def _fail_workflow_start(
    request: ToolExecutionRequest,
    *,
    preview_id: uuid.UUID,
    claim_token: uuid.UUID,
    code: str,
    message: str,
) -> None:
    tenant_id, agent_id, session_id, user_id = _request_identity(request)
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="workflow_preview_start_failure") as db:
        preview = await load_workflow_preview(
            db,
            preview_id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            for_update=True,
        )
        mark_workflow_preview_failed_record(
            preview,
            code=code,
            message=message,
            claim_token=claim_token,
        )
        await db.flush()


def _dynamic_candidate_binding_error(
    *,
    proposal_id: str | None,
    candidate_id: str | None,
    dynamic_candidate: dict[str, Any] | None,
    definition_hash: str,
    args_hash: str,
) -> str | None:
    if not proposal_id and not candidate_id:
        return None
    if not proposal_id or not candidate_id:
        return "Dynamic Workflow preview requires both proposal_id and candidate_id"
    if not dynamic_candidate:
        return "Dynamic Workflow candidate was not found or expired; call propose_dynamic_workflow again"
    if dynamic_candidate.get("definition_hash") != definition_hash:
        return "Dynamic Workflow candidate lowered_definition differs from the preview_workflow definition"
    if dynamic_candidate.get("args_hash") != args_hash:
        return "Dynamic Workflow candidate preview_args differ from the preview_workflow args"
    return None


@tool(
    ToolMeta(
        name="propose_dynamic_workflow",
        description=(
            "Draft and validate Dynamic Workflow candidates WITHOUT previewing or starting execution.\n\n"
            "Use when the task needs many isolated workers, repeatable orchestration, adversarial review, "
            "or long-running state outside the main context. Provide one to three candidates with success criteria, "
            "pattern mix, budget, failure policy, and a lowered governed WorkflowDefinition. "
            "This tool never runs workflow steps. After choosing a candidate, call preview_workflow with the returned "
            "lowered_definition and preview_args; start_workflow only after the user approves that exact preview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "why_workflow": {"type": "string"},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "args": {"type": "object", "description": "Preview args shared by candidates unless overridden."},
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "name": {"type": "string"},
                            "pattern_mix": {"type": "array", "items": {"type": "string"}},
                            "risk_level": {"type": "string"},
                            "budget": {"type": "object"},
                            "failure_policy": {"type": "object"},
                            "args": {"type": "object"},
                            "lowered_definition": _DEFINITION_PARAM,
                        },
                        "required": ["lowered_definition"],
                    },
                },
                "recommended_candidate_id": {"type": "string"},
            },
            "required": ["goal", "why_workflow", "success_criteria", "candidates"],
        },
        category="workflow",
        display_name="Propose Dynamic Workflow",
        # This writes only the canonical confirmation evidence ledger. It does
        # not execute a step or mutate a user/business asset, so it has the same
        # domain-read-only semantics as HR blueprint preview persistence.
        read_only=True,
        parallel_safe=True,
        risk_class="controlled_write",
        idempotency_scope="session",
        governance="safe",
        adapter="request",
    )
)
async def propose_dynamic_workflow(request: ToolExecutionRequest) -> str:
    proposal = validate_dynamic_workflow_proposal(request.arguments)
    if proposal.get("ok") is not True:
        return dump_json(proposal)
    try:
        proposal = await _persist_dynamic_proposal(request, proposal)
    except WorkflowConfirmationConflict as exc:
        return dump_json({"ok": False, "error_code": exc.code, "error": exc.message})
    return dump_json(proposal)


@tool(
    ToolMeta(
        name="preview_workflow",
        description=(
            "Compile and preflight an ephemeral workflow definition WITHOUT running it.\n\n"
            "Usage:\n"
            "- Always preview before start_workflow: returns preview_id, definition_hash, args_hash, confirmation notes, "
            "planned leaf calls and budget.\n"
            "- Confirmation notes are informational; they do not force Plan Mode.\n"
            "- Show the user the preview and get their go-ahead before starting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "definition": _DEFINITION_PARAM,
                "args": {"type": "object", "description": "Arguments matching the definition's args_schema."},
                "proposal_id": {
                    "type": "string",
                    "description": "Dynamic Workflow proposal_id, if previewing a candidate.",
                },
                "candidate_id": {
                    "type": "string",
                    "description": "Dynamic Workflow candidate_id, if previewing a candidate.",
                },
            },
            "required": ["definition"],
        },
        category="workflow",
        display_name="Preview Workflow",
        # Persisting an immutable preview is evidence bookkeeping, not workflow
        # execution. start_workflow remains the governed side-effect boundary.
        read_only=True,
        parallel_safe=True,
        risk_class="controlled_write",
        idempotency_scope="session",
        governance="safe",
        adapter="request",
    )
)
async def preview_workflow(request: ToolExecutionRequest) -> str:
    arguments = request.arguments
    definition = arguments.get("definition") or {}
    args = arguments.get("args") or {}
    proposal_id = str(arguments.get("proposal_id") or "").strip() or None
    candidate_id = str(arguments.get("candidate_id") or "").strip() or None
    try:
        proposal, dynamic_candidate = await _load_dynamic_candidate(request, proposal_id, candidate_id)
        compiled = compile_workflow(definition)
        args = normalize_workflow_args(compiled, args)
        from app.config import get_settings

        admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))
        confirmation = inspect_workflow_confirmation_needs(compiled, args=args)
    except (WorkflowCompileError, WorkflowAdmissionError, WorkflowConfirmationConflict) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    args_hash = compute_definition_hash(args)
    dynamic_error = _dynamic_candidate_binding_error(
        proposal_id=proposal_id,
        candidate_id=candidate_id,
        dynamic_candidate=dynamic_candidate,
        definition_hash=compiled.definition_hash,
        args_hash=args_hash,
    )
    if dynamic_error:
        return json.dumps({"ok": False, "error": dynamic_error}, ensure_ascii=False)

    preview_payload = {
        "ok": True,
        "proposal_id": proposal_id,
        "candidate_id": candidate_id,
        "definition_hash": compiled.definition_hash,
        "args_hash": args_hash,
        "confirmation_required": confirmation.requires_confirmation,
        "confirmation_reasons": confirmation.reasons,
        "planned_leaf_calls": admission.planned_leaf_calls,
        "budget_tokens": admission.budget_tokens,
        "dynamic_candidate": dynamic_candidate,
    }
    try:
        persisted = await _persist_workflow_preview(
            request,
            definition=compiled.definition.canonical_dict(),
            args=args,
            definition_hash=compiled.definition_hash,
            args_hash=args_hash,
            preview_payload=preview_payload,
            proposal=proposal,
            candidate_id=candidate_id,
        )
    except WorkflowConfirmationConflict as exc:
        return json.dumps({"ok": False, "error_code": exc.code, "error": exc.message}, ensure_ascii=False)
    return json.dumps(
        {
            **persisted,
            "dynamic_candidate": None,
        },
        ensure_ascii=False,
    )


@tool(
    ToolMeta(
        name="start_workflow",
        timeout_seconds=180.0,
        idempotency_scope="runtime_task",
        description=(
            "Start an ephemeral workflow run from a structured definition.\n\n"
            "Use a workflow ONLY when the step order itself is a requirement — a fixed sequence "
            "that must not drift, mandatory mid-run approval gates, or large fan-out under a hard "
            "budget. For one-off parallelism or isolation, spawn_subagent is enough; for handing "
            "work to another digital employee, use delegate_to_agent.\n\n"
            "Usage:\n"
            "- preview_workflow FIRST and show the user what will run.\n"
            "- If the preview requires confirmation, this tool cannot start it. Stop and wait for the "
            "authenticated user to select Confirm and run on that exact preview; do not infer confirmation "
            "from chat text or enter Plan Mode unless the user explicitly asks for it.\n"
            "- If the preview does not require confirmation, start_workflow may start that exact preview.\n"
            "- Pass ledger_todo_id to mirror the run onto your work-ledger todo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "preview_id": {
                    "type": "string",
                    "description": "The durable preview_id returned by preview_workflow. Definition and args are loaded from that immutable snapshot.",
                },
                "ledger_todo_id": {
                    "type": "string",
                    "description": "Optional work-ledger todo this run serves (observation mirror only).",
                },
            },
            "required": ["preview_id"],
            "additionalProperties": False,
        },
        category="workflow",
        display_name="Start Workflow",
        governance="sensitive",
        work_amplifying=True,
        plan_gate_action_kind="bridge:self",
    )
)
async def start_workflow(request: ToolExecutionRequest) -> str:
    agent_id = request.context.agent_id
    session_id = request.context.session_id
    execution_shape_decision = build_tool_execution_shape_decision(
        "start_workflow",
        execution_shape_from_round_state(request.context.round_state),
    )
    if not session_id:
        return json.dumps(
            {
                "ok": False,
                "error_code": "missing_workflow_session",
                "error": (
                    "start_workflow must run inside the current chat session so the Dynamic Workflow run "
                    "can be resumed and shown in the session workbench."
                ),
            },
            ensure_ascii=False,
        )
    arguments = request.arguments
    unexpected_arguments = sorted(set(arguments) - {"preview_id", "ledger_todo_id"})
    if unexpected_arguments:
        return json.dumps(
            {
                "ok": False,
                "error_code": "invalid_start_arguments",
                "error": (
                    "start_workflow accepts only preview_id and ledger_todo_id; canonical definition and args "
                    f"come from the durable preview (unexpected: {', '.join(unexpected_arguments)})"
                ),
            },
            ensure_ascii=False,
        )
    ledger_todo_id = arguments.get("ledger_todo_id") or None
    try:
        preview_id = uuid.UUID(str(arguments.get("preview_id") or ""))
    except ValueError:
        return json.dumps(
            {"ok": False, "error_code": "preview_id_required", "error": "start_workflow requires a valid preview_id"},
            ensure_ascii=False,
        )

    try:
        claim = await _claim_workflow_start(request, preview_id)
    except WorkflowConfirmationConflict as exc:
        if exc.code == "explicit_user_confirmation_required":
            return json.dumps(
                {
                    "ok": False,
                    "status": "requires_confirmation",
                    "requires_confirmation": True,
                    "error_code": exc.code,
                    "error": exc.message,
                    "preview_id": str(exc.details.get("preview_id") or preview_id),
                    "confirmation_reasons": list(exc.details.get("confirmation_reasons") or []),
                    "next_action": "Wait for the user to select Confirm and run on this exact workflow preview.",
                },
                ensure_ascii=False,
            )
        return json.dumps({"ok": False, "error_code": exc.code, "error": exc.message}, ensure_ascii=False)

    preview = claim.preview
    if claim.outcome == "replay":
        return json.dumps(
            {
                "ok": True,
                "run_id": str(preview.run_id),
                "status": "replayed",
                "reason": "workflow_preview_already_started",
                "execution_shape_decision": execution_shape_decision,
            },
            ensure_ascii=False,
        )

    claim_token = preview.claim_token
    run_id = preview.run_id
    if claim_token is None or run_id is None:
        return json.dumps(
            {"ok": False, "error_code": "invalid_start_claim", "error": "Workflow start claim is incomplete."},
            ensure_ascii=False,
        )

    definition = dict(preview.definition_json or {})
    args = dict(preview.args_json or {})
    proposal_id = str(preview.proposal_id) if preview.proposal_id else None
    candidate_id = preview.candidate_id
    dynamic_candidate = mapping((preview.preview_json or {}).get("dynamic_candidate"))
    try:
        run_metadata = build_dynamic_workflow_run_metadata(
            proposal_id=proposal_id,
            candidate_id=candidate_id,
            preview_id=str(preview_id),
            definition_hash=preview.definition_hash,
            args_hash=preview.args_hash,
            candidate=dynamic_candidate,
        )
        if run_metadata is None:
            run_metadata = {}
        run_metadata["execution_shape_decision"] = execution_shape_decision
        run_metadata["workflow_confirmation"] = {
            "preview_id": str(preview.id),
            "artifact_version": preview.artifact_version,
            "artifact_hash": preview.artifact_hash,
            "confirmed_by_user_id": (
                str(preview.confirmed_by_user_id) if preview.confirmed_by_user_id is not None else None
            ),
            "confirmation_source": preview.confirmation_source,
            "confirmation_evidence_id": preview.confirmation_evidence_id,
        }
        handle = await start_ephemeral_workflow_for_agent(
            agent_id=agent_id,
            definition=definition,
            args=args,
            user_id=request.context.user_id,
            ledger_todo_id=ledger_todo_id,
            parent_session_id=session_id,
            root_session_id=session_id,
            definition_source="dynamic_workflow" if proposal_id or candidate_id else "ephemeral",
            run_metadata=run_metadata,
            run_id=run_id,
            enqueue_only=True,
            budget_run_id=request.context.budget_run_id,
        )
    except Exception as exc:
        logger.exception("Workflow launch failed for durable preview %s", preview_id)
        try:
            await _fail_workflow_start(
                request,
                preview_id=preview_id,
                claim_token=claim_token,
                code="workflow_launch_failed",
                message=str(exc),
            )
        except Exception:
            logger.exception("Could not persist Workflow launch failure for preview %s", preview_id)
        return json.dumps(
            {"ok": False, "error_code": "workflow_launch_failed", "error": str(exc)},
            ensure_ascii=False,
        )

    reconciliation_pending = False
    try:
        await _finish_workflow_start(
            request,
            preview_id=preview_id,
            claim_token=claim_token,
            run_id=handle.run_id,
        )
    except Exception:
        reconciliation_pending = True
        logger.exception("Workflow run %s started but preview finalization failed", handle.run_id)

    return json.dumps(
        {
            "ok": True,
            "run_id": str(handle.run_id),
            "status": handle.outcome.status,
            "reason": handle.outcome.reason,
            "preview_id": str(preview_id),
            "reconciliation_pending": reconciliation_pending,
            "execution_shape_decision": execution_shape_decision,
        },
        ensure_ascii=False,
    )
