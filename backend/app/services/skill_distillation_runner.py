"""Owns one evidence-to-skill distillation transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.skill_distiller import (
        DirectSkillCandidate,
        FactorSessionProvider,
        Path,
        SessionWorkflowEvidence,
        uuid,
    )


async def run_skill_distillation(
    *,
    agent_id: uuid.UUID,
    workspace: Path,
    tenant_id: uuid.UUID | None,
    runtime_config: Any,
    model: Any | None = None,
    current_session_id: str | None = None,
    factor_session_provider: FactorSessionProvider | None = None,
    support: Any,
) -> dict[str, Any]:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    _build_workflow_signature = support._build_workflow_signature
    _capture_skill_candidate_package_factor = support._capture_skill_candidate_package_factor
    _collect_direct_skill_candidates = support._collect_direct_skill_candidates
    _commit_skill_with_asset_revision = support._commit_skill_with_asset_revision
    _draft_skill_with_llm = support._draft_skill_with_llm
    _infer_skill_name = support._infer_skill_name
    _load_internal_session_evidence = support._load_internal_session_evidence
    _normalize_skill_folder_name = support._normalize_skill_folder_name
    _promotion_gate_metadata = support._promotion_gate_metadata
    _referee_review_payload = support._referee_review_payload
    _resolve_patch_target_skill = support._resolve_patch_target_skill
    _run_skill_artifact_gate = support._run_skill_artifact_gate
    _run_skill_referee_gate = support._run_skill_referee_gate
    _select_skill_candidate_with_llm = support._select_skill_candidate_with_llm
    SkillCandidateSelectionOption = support.SkillCandidateSelectionOption
    advance_distiller_cursor = support.advance_distiller_cursor
    datetime = support.datetime
    infer_static_runtime_tool_group_names = support.infer_static_runtime_tool_group_names
    load_distiller_state = support.load_distiller_state
    load_flywheel_skill_candidate_drafts = support.load_flywheel_skill_candidate_drafts
    load_memory_skill_candidates = support.load_memory_skill_candidates
    load_skill_candidates = support.load_skill_candidates
    logger = support.logger
    rank_skill_candidates = support.rank_skill_candidates
    record_skill_execution = support.record_skill_execution
    record_skill_lifecycle_event = support.record_skill_lifecycle_event
    record_workflow_candidates_from_memory = support.record_workflow_candidates_from_memory
    render_skill_evidence_contrast = support.render_skill_evidence_contrast
    replace = support.replace
    resolve_existing_skill_conflict = support.resolve_existing_skill_conflict
    save_distiller_state = support.save_distiller_state
    timezone = support.timezone
    update_skill_candidate_package_status = support.update_skill_candidate_package_status
    update_skill_candidate_record = support.update_skill_candidate_record
    validate_distilled_skill = support.validate_distilled_skill
    write_skill_candidate_package = support.write_skill_candidate_package
    Path = support.Path

    if not getattr(runtime_config, "skill_candidate_loop_enabled", False):
        return {"status": "disabled", "processed_sessions": 0}

    # Memory candidate lane (spec §12 P4): surface workflow candidates into
    # the evolution ledger and load skill candidates as drafting evidence.
    from app.config import get_settings

    data_root = Path(get_settings().AGENT_DATA_DIR)
    workflow_candidates_recorded = 0
    memory_skill_candidates: list[dict[str, str]] = []
    flywheel_skill_candidate_drafts = load_flywheel_skill_candidate_drafts(workspace)
    try:
        workflow_candidates_recorded = record_workflow_candidates_from_memory(data_root, agent_id, workspace=workspace)
        memory_skill_candidates = load_memory_skill_candidates(data_root, agent_id)
    except Exception as exc:  # noqa: BLE001 — candidate-lane IO must not break distillation
        logger.warning("[skill_distiller] memory candidate lane failed for %s: %s", agent_id, exc)

    state = load_distiller_state(workspace)
    evidence = await _load_internal_session_evidence(
        agent_id=agent_id,
        since_days=None,
        state=state,
        current_session_id=current_session_id,
    )
    direct_candidates: list[DirectSkillCandidate] = _collect_direct_skill_candidates(
        skill_candidate_drafts=flywheel_skill_candidate_drafts,
        memory_skill_candidates=memory_skill_candidates,
        workspace=workspace,
    )
    if not evidence and not direct_candidates:
        return {
            "status": "idle",
            "processed_sessions": 0,
            "workflow_candidates_recorded": workflow_candidates_recorded,
            "memory_skill_candidates": len(memory_skill_candidates),
        }

    processed = 0
    last_cursor = advance_distiller_cursor(
        (state.last_processed_at or "", state.last_processed_session_id or ""),
        evidence,
    )
    grouped: dict[str, list[SessionWorkflowEvidence]] = {}

    for item in evidence:
        processed += 1
        fingerprint = _build_workflow_signature(item.tool_names)
        if fingerprint.workflow_signature is None:
            continue
        if item.status == "noop":
            continue

        skill_record_name = (
            item.loaded_skill_names[0]
            if item.used_skill and item.loaded_skill_names
            else _infer_skill_name(fingerprint.workflow_signature)
        )
        record_skill_execution(
            workspace,
            skill_name=skill_record_name,
            workflow_signature=fingerprint.workflow_signature,
            status=item.status,
            used_skill=item.used_skill,
            note=item.summary,
            blocker="",
            occurred_at=item.occurred_at,
        )
        grouped.setdefault(fingerprint.workflow_signature, []).append(item)

    state.last_processed_at = last_cursor[0] or state.last_processed_at
    state.last_processed_session_id = last_cursor[1] or state.last_processed_session_id
    save_distiller_state(workspace, state)

    candidates = load_skill_candidates(workspace)
    ranked_candidates = rank_skill_candidates(candidates.values())
    reviewable = ranked_candidates.reviewable
    if model is None:
        return {"status": "candidate", "processed_sessions": processed}
    if not direct_candidates and not reviewable:
        return {"status": "candidate", "processed_sessions": processed}

    selection_options: list[SkillCandidateSelectionOption] = []
    for direct in direct_candidates:
        selection_options.append(
            SkillCandidateSelectionOption(
                key=f"direct:{direct.record.workflow_signature}:{direct.candidate_id}",
                candidate_id=direct.candidate_id,
                record=direct.record,
                evidence=direct.evidence,
                direct_candidate=direct,
            )
        )
    direct_workflow_signatures = {
        value for direct in direct_candidates for value in (direct.record.workflow_signature, direct.candidate_id)
    }
    for candidate_record in reviewable:
        if candidate_record.workflow_signature in direct_workflow_signatures:
            continue
        candidate_evidence = grouped.get(candidate_record.workflow_signature, [])
        if not candidate_evidence:
            candidate_evidence = [
                item
                for item in evidence
                if _build_workflow_signature(item.tool_names).workflow_signature == candidate_record.workflow_signature
            ]
        selection_options.append(
            SkillCandidateSelectionOption(
                key=f"workflow:{candidate_record.workflow_signature}",
                candidate_id=candidate_record.workflow_signature,
                record=candidate_record,
                evidence=candidate_evidence,
                direct_candidate=None,
            )
        )

    try:
        selected_option = await _select_skill_candidate_with_llm(
            model=model,
            options=selection_options,
            workspace=workspace,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 - selector failure must hold, never choose mechanically
        logger.warning("[skill_distiller] candidate selector failed for %s: %s", agent_id, exc)
        return {
            "status": "held",
            "processed_sessions": processed,
            "reason": f"skill_candidate_selector_failed:{type(exc).__name__}",
            "candidate_count": len(selection_options),
        }
    if selected_option is None:
        return {
            "status": "held",
            "processed_sessions": processed,
            "reason": "skill_candidate_selector_deferred",
            "candidate_count": len(selection_options),
        }

    distillation_intent = "review"
    record = selected_option.record
    evidence_for_candidate = selected_option.evidence
    direct_candidate = selected_option.direct_candidate
    if not evidence_for_candidate and direct_candidate is None:
        evidence_for_candidate = grouped.get(record.workflow_signature, [])
        if not evidence_for_candidate:
            evidence_for_candidate = [
                item
                for item in evidence
                if _build_workflow_signature(item.tool_names).workflow_signature == record.workflow_signature
            ]

    draft = await _draft_skill_with_llm(
        model=model,
        workflow_signature=record.workflow_signature,
        evidence=evidence_for_candidate,
        declared_packs=infer_static_runtime_tool_group_names(
            list(_build_workflow_signature(evidence_for_candidate[0].tool_names).normalized_tools)
        )
        if evidence_for_candidate
        else (),
        workspace=workspace,
        distillation_intent=distillation_intent,
        target_skill_name=None,
        evidence_contrast=render_skill_evidence_contrast(evidence_for_candidate),
        memory_candidates=memory_skill_candidates,
        skill_candidate_drafts=flywheel_skill_candidate_drafts,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )

    conflict = resolve_existing_skill_conflict(workspace=workspace, draft=draft)
    if draft.decision in {"defer", "reject"}:
        note = draft.reason or "The Skill Distiller model deferred this candidate."
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name or record.skill_name,
            blocker="llm_deferred",
            last_status="defer",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name or record.skill_name,
            status="defer",
            note=note,
        )
        return {"status": "deferred", "processed_sessions": processed}

    if conflict.final_decision in {"defer", "reject"}:
        note = conflict.reason or draft.reason or "The Skill candidate requires another model decision."
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name or record.skill_name,
            blocker="semantic_conflict_deferred",
            last_status="defer",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name or record.skill_name,
            status="defer",
            note=note,
        )
        return {"status": "deferred", "processed_sessions": processed, "reason": note}

    final_decision = conflict.final_decision
    effective_draft = (
        replace(draft, name=conflict.existing_skill_name)
        if final_decision == "patch" and conflict.existing_skill_name
        else draft
    )

    rendered = effective_draft.skill_markdown.strip().rstrip() + "\n" if effective_draft.skill_markdown.strip() else ""
    validation_errors = validate_distilled_skill(workspace=workspace, draft=effective_draft, rendered_markdown=rendered)
    if validation_errors:
        note = "; ".join(validation_errors)
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=effective_draft.name or record.skill_name,
            blocker="validation_failed",
            last_status="defer",
            last_note=note,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=effective_draft.name or record.skill_name,
            status="defer",
            note=note,
        )
        return {"status": "deferred", "processed_sessions": processed, "errors": validation_errors}

    if final_decision == "patch":
        patch_target = _resolve_patch_target_skill(workspace=workspace, draft=effective_draft, conflict=conflict)
        if patch_target is None:
            note = conflict.reason or effective_draft.reason or "Patch decision had no existing skill target."
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name or record.skill_name,
                blocker="patch_target_missing",
                last_status="defer",
                last_note=note,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=effective_draft.name or record.skill_name,
                status="defer",
                note=note,
            )
            return {"status": "deferred", "processed_sessions": processed, "reason": note}

        from app.services.skill_evolution_registry import (
            ORIGIN_USER_SKILL_CREATOR,
            can_self_evolve_skill,
            get_skill_evolution_entry,
        )

        if not can_self_evolve_skill(workspace, patch_target.metadata.name):
            note = "Patch target is not in the self-evolving skill chain."
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name or record.skill_name,
                blocker="non_evolvable_skill",
                last_status="defer",
                last_note=note,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=patch_target.metadata.name,
                status="defer",
                note=note,
            )
            return {"status": "deferred", "processed_sessions": processed, "reason": note}
        patch_registry_entry = get_skill_evolution_entry(workspace, patch_target.metadata.name)
        patch_skill_origin = (
            str(patch_registry_entry.get("skill_origin"))
            if isinstance(patch_registry_entry, dict) and patch_registry_entry.get("skill_origin")
            else ORIGIN_USER_SKILL_CREATOR
        )

        from app.services.evolution_ledger import (
            record_evolution_candidate,
            record_promotion_decision,
        )
        from app.services.evolution_verification import (
            decide_provisional_promotion,
            record_verification_eval,
            run_evolution_verification,
        )

        patch_relative_path = patch_target.relative_path
        candidate = record_evolution_candidate(
            workspace,
            target_type="skill_patch",
            target_id=patch_relative_path,
            diff=rendered,
            source_attempt_ids=[item.session_id for item in evidence_for_candidate],
            baseline_version=patch_relative_path,
            metadata={
                "workflow_signature": record.workflow_signature,
                "confidence": effective_draft.confidence,
                "declared_tools": list(effective_draft.declared_tools),
                "declared_packs": list(effective_draft.declared_packs),
                "existing_skill_name": patch_target.metadata.name,
                "reason": effective_draft.reason or conflict.reason,
                "distillation_intent": distillation_intent,
                "evidence_contrast": render_skill_evidence_contrast(evidence_for_candidate),
            },
        )
        candidate_manifest = write_skill_candidate_package(
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            rendered_markdown=rendered,
            skill_name=effective_draft.name,
            package_type="patch",
            target_path=patch_relative_path,
            skill_origin=patch_skill_origin,
            evolvable=True,
            source_refs=[item.session_id for item in evidence_for_candidate],
            reason=effective_draft.reason or conflict.reason or "Patch existing skill after repeated evidence.",
            declared_tools=effective_draft.declared_tools,
            declared_packs=effective_draft.declared_packs
            or infer_static_runtime_tool_group_names(list(effective_draft.declared_tools)),
            status="candidate",
            extra_metadata={"workflow_signature": record.workflow_signature},
        )
        await _capture_skill_candidate_package_factor(
            tenant_id=tenant_id,
            agent_id=agent_id,
            manifest=candidate_manifest,
            draft=effective_draft,
            evidence=evidence_for_candidate,
            workflow_signature=record.workflow_signature,
            distillation_intent=distillation_intent,
            session_provider=factor_session_provider,
        )
        verification_report = run_evolution_verification(
            workspace=workspace,
            candidate=candidate,
            graders=[
                {
                    "type": "skill_guard",
                    "content": rendered,
                    "path": patch_relative_path,
                }
            ],
        )
        record_verification_eval(
            workspace,
            candidate=candidate,
            verification_report=verification_report,
            dataset="skill_distiller.verified_skill_guard",
        )
        artifact_gate_report = None
        if verification_report.get("passed"):
            package_draft_path = f"evolution/skill_candidates/{candidate['candidate_id']}/SKILL.md.draft"
            artifact_gate_report = await _run_skill_artifact_gate(
                rendered_markdown=rendered,
                candidate_path=package_draft_path,
            )
        promotion_decision = decide_provisional_promotion(
            candidate,
            verification_report=verification_report,
            artifact_gate_report=artifact_gate_report,
        )
        if promotion_decision["decision"] != "provisional":
            record_promotion_decision(
                workspace,
                candidate_id=candidate["candidate_id"],
                decision="held",
                reason=promotion_decision["reason"],
                rollback_ref=patch_relative_path,
                metadata=_promotion_gate_metadata(
                    verification_report=verification_report,
                    artifact_gate_report=artifact_gate_report,
                ),
            )
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name,
                blocker="verification_failed",
                last_status="defer",
                last_note=promotion_decision["reason"],
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=effective_draft.name,
                status="defer",
                note=promotion_decision["reason"],
            )
            update_skill_candidate_package_status(
                workspace=workspace,
                candidate_id=candidate["candidate_id"],
                status="held",
                reason=promotion_decision["reason"],
            )
            return {
                "status": "deferred",
                "processed_sessions": processed,
                "reason": promotion_decision["reason"],
                "verification_report": verification_report,
                "artifact_gate_report": artifact_gate_report,
            }

        referee_review, referee_hold_reason = await _run_skill_referee_gate(
            model=model,
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            draft=effective_draft,
            rendered_markdown=rendered,
            final_decision="patch",
            evidence=evidence_for_candidate,
            verification_report=verification_report,
            artifact_gate_report=artifact_gate_report,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        if referee_hold_reason:
            record_promotion_decision(
                workspace,
                candidate_id=candidate["candidate_id"],
                decision="held",
                reason=referee_hold_reason,
                rollback_ref=patch_relative_path,
                metadata=_promotion_gate_metadata(
                    verification_report=verification_report,
                    artifact_gate_report=artifact_gate_report,
                    referee_review=referee_review,
                ),
            )
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name,
                blocker="referee_failed",
                last_status="defer",
                last_note=referee_hold_reason,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            record_skill_lifecycle_event(
                workspace,
                skill_name=effective_draft.name,
                status="defer",
                note=referee_hold_reason,
            )
            update_skill_candidate_package_status(
                workspace=workspace,
                candidate_id=candidate["candidate_id"],
                status="held",
                reason=referee_hold_reason,
            )
            return {
                "status": "deferred",
                "processed_sessions": processed,
                "reason": referee_hold_reason,
                "verification_report": verification_report,
                "artifact_gate_report": artifact_gate_report,
                "referee_review": _referee_review_payload(referee_review),
                **({"direct_candidate_id": direct_candidate.candidate_id} if direct_candidate else {}),
            }

        save_result = await _commit_skill_with_asset_revision(
            workspace=workspace,
            target_relative_path=patch_relative_path,
            rendered_markdown=rendered,
            skill_name=effective_draft.name,
            overwrite=True,
            status="provisional",
            candidate_id=candidate["candidate_id"],
            skill_origin=patch_skill_origin,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        if "✅" not in save_result:
            record_promotion_decision(
                workspace,
                candidate_id=candidate["candidate_id"],
                decision="held",
                reason="patch save failed after verification",
                rollback_ref=patch_relative_path,
                metadata=_promotion_gate_metadata(
                    verification_report=verification_report,
                    artifact_gate_report=artifact_gate_report,
                    referee_review=referee_review,
                    extra={"save_result": save_result},
                ),
            )
            update_skill_candidate_record(
                workspace,
                workflow_signature=record.workflow_signature,
                skill_name=effective_draft.name,
                blocker="save_failed",
                last_status="defer",
                last_note=save_result,
                last_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            update_skill_candidate_package_status(
                workspace=workspace,
                candidate_id=candidate["candidate_id"],
                status="held",
                reason="patch save failed after verification",
            )
            return {
                "status": "deferred",
                "processed_sessions": processed,
                "save_result": save_result,
                "verification_report": verification_report,
                "artifact_gate_report": artifact_gate_report,
            }

        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="provisional",
            reason=promotion_decision["reason"],
            rollback_ref=patch_relative_path,
            metadata=_promotion_gate_metadata(
                verification_report=verification_report,
                artifact_gate_report=artifact_gate_report,
                referee_review=referee_review,
                extra={"save_result": save_result},
            ),
        )
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            status="provisional",
            reason=promotion_decision["reason"],
            extra_metadata={"target_path": patch_relative_path},
        )
        from app.services.evolution_validation import validate_evolution_ledger

        evolution_validation = validate_evolution_ledger(workspace, write_report=True)
        patched_at = datetime.now(timezone.utc).isoformat()
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=effective_draft.name,
            blocker="provisional",
            last_status="provisional",
            last_note=effective_draft.reason or "Patched existing skill for provisional trial.",
            last_updated_at=patched_at,
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=effective_draft.name,
            status="provisional",
            note=effective_draft.reason or "Patched existing skill for provisional trial.",
        )
        state.last_promotion_at = patched_at
        save_distiller_state(workspace, state)
        return {
            "status": "provisional",
            "processed_sessions": processed,
            "skill_name": effective_draft.name,
            "workflow_signature": record.workflow_signature,
            "evolution_validation_passed": evolution_validation["passed"],
            "evolution_validation": evolution_validation.get("report_artifact"),
            "verification_report": verification_report,
            "artifact_gate_report": artifact_gate_report,
            "workflow_candidates_recorded": workflow_candidates_recorded,
            "referee_review": _referee_review_payload(referee_review),
            **({"direct_candidate_id": direct_candidate.candidate_id} if direct_candidate else {}),
        }

    from app.services.evolution_ledger import (
        record_evolution_candidate,
        record_promotion_decision,
    )
    from app.services.evolution_verification import (
        decide_provisional_promotion,
        record_verification_eval,
        run_evolution_verification,
    )

    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id=draft.name,
        diff=rendered,
        source_attempt_ids=[item.session_id for item in evidence_for_candidate],
        baseline_version="none",
        metadata={
            "workflow_signature": record.workflow_signature,
            "confidence": draft.confidence,
            "declared_tools": list(draft.declared_tools),
            "declared_packs": list(draft.declared_packs),
            "distillation_intent": distillation_intent,
            "evidence_contrast": render_skill_evidence_contrast(evidence_for_candidate),
        },
    )
    rollback_ref = f"skills/{_normalize_skill_folder_name(draft.name)}/SKILL.md"
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED

    candidate_manifest = write_skill_candidate_package(
        workspace=workspace,
        candidate_id=candidate["candidate_id"],
        rendered_markdown=rendered,
        skill_name=draft.name,
        package_type="promote",
        target_path=rollback_ref,
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        evolvable=True,
        source_refs=[item.session_id for item in evidence_for_candidate],
        reason=draft.reason or "Promote repeated workflow into a reusable skill.",
        declared_tools=draft.declared_tools,
        declared_packs=draft.declared_packs or infer_static_runtime_tool_group_names(list(draft.declared_tools)),
        status="candidate",
        extra_metadata={"workflow_signature": record.workflow_signature},
    )
    await _capture_skill_candidate_package_factor(
        tenant_id=tenant_id,
        agent_id=agent_id,
        manifest=candidate_manifest,
        draft=draft,
        evidence=evidence_for_candidate,
        workflow_signature=record.workflow_signature,
        distillation_intent=distillation_intent,
        session_provider=factor_session_provider,
    )
    verification_report = run_evolution_verification(
        workspace=workspace,
        candidate=candidate,
        graders=[
            {
                "type": "skill_guard",
                "content": rendered,
                "path": "SKILL.md",
            }
        ],
    )
    record_verification_eval(
        workspace,
        candidate=candidate,
        verification_report=verification_report,
        dataset="skill_distiller.verified_skill_guard",
    )
    artifact_gate_report = None
    if verification_report.get("passed"):
        package_draft_path = f"evolution/skill_candidates/{candidate['candidate_id']}/SKILL.md.draft"
        artifact_gate_report = await _run_skill_artifact_gate(
            rendered_markdown=rendered,
            candidate_path=package_draft_path,
        )
    promotion_decision = decide_provisional_promotion(
        candidate,
        verification_report=verification_report,
        artifact_gate_report=artifact_gate_report,
    )
    if promotion_decision["decision"] != "provisional":
        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="held",
            reason=promotion_decision["reason"],
            metadata=_promotion_gate_metadata(
                verification_report=verification_report,
                artifact_gate_report=artifact_gate_report,
            ),
        )
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="verification_failed",
            last_status="defer",
            last_note=promotion_decision["reason"],
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name,
            status="defer",
            note=promotion_decision["reason"],
        )
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            status="held",
            reason=promotion_decision["reason"],
        )
        return {
            "status": "deferred",
            "processed_sessions": processed,
            "reason": promotion_decision["reason"],
            "verification_report": verification_report,
            "artifact_gate_report": artifact_gate_report,
        }

    referee_review, referee_hold_reason = await _run_skill_referee_gate(
        model=model,
        workspace=workspace,
        candidate_id=candidate["candidate_id"],
        draft=draft,
        rendered_markdown=rendered,
        final_decision="promote",
        evidence=evidence_for_candidate,
        verification_report=verification_report,
        artifact_gate_report=artifact_gate_report,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )
    if referee_hold_reason:
        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="held",
            reason=referee_hold_reason,
            metadata=_promotion_gate_metadata(
                verification_report=verification_report,
                artifact_gate_report=artifact_gate_report,
                referee_review=referee_review,
            ),
        )
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="referee_failed",
            last_status="defer",
            last_note=referee_hold_reason,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        record_skill_lifecycle_event(
            workspace,
            skill_name=draft.name,
            status="defer",
            note=referee_hold_reason,
        )
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            status="held",
            reason=referee_hold_reason,
        )
        return {
            "status": "deferred",
            "processed_sessions": processed,
            "reason": referee_hold_reason,
            "verification_report": verification_report,
            "artifact_gate_report": artifact_gate_report,
            "referee_review": _referee_review_payload(referee_review),
            **({"direct_candidate_id": direct_candidate.candidate_id} if direct_candidate else {}),
        }

    save_result = await _commit_skill_with_asset_revision(
        workspace=workspace,
        target_relative_path=rollback_ref,
        rendered_markdown=rendered,
        skill_name=draft.name,
        overwrite=False,
        status="provisional",
        candidate_id=candidate["candidate_id"],
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        agent_id=agent_id,
        tenant_id=tenant_id,
    )
    if "✅" not in save_result:
        record_promotion_decision(
            workspace,
            candidate_id=candidate["candidate_id"],
            decision="held",
            reason="save failed after verification",
            metadata=_promotion_gate_metadata(
                verification_report=verification_report,
                artifact_gate_report=artifact_gate_report,
                referee_review=referee_review,
                extra={"save_result": save_result},
            ),
        )
        update_skill_candidate_record(
            workspace,
            workflow_signature=record.workflow_signature,
            skill_name=draft.name,
            blocker="save_failed",
            last_status="defer",
            last_note=save_result,
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate["candidate_id"],
            status="held",
            reason="save failed after verification",
        )
        return {
            "status": "deferred",
            "processed_sessions": processed,
            "save_result": save_result,
            "verification_report": verification_report,
            "artifact_gate_report": artifact_gate_report,
        }

    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="provisional",
        reason=promotion_decision["reason"],
        rollback_ref=rollback_ref,
        metadata=_promotion_gate_metadata(
            verification_report=verification_report,
            artifact_gate_report=artifact_gate_report,
            referee_review=referee_review,
            extra={"save_result": save_result},
        ),
    )
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=candidate["candidate_id"],
        status="provisional",
        reason=promotion_decision["reason"],
        extra_metadata={"target_path": rollback_ref},
    )
    from app.services.evolution_validation import validate_evolution_ledger

    evolution_validation = validate_evolution_ledger(workspace, write_report=True)

    # Spec §12 P4: promoted strategy evidence leaves the candidate pool —
    # the LLM names which memory candidates this skill consumed; we stamp
    # `[promoted_to=skill]` so they stop surfacing as open candidates.
    promoted_memory_ids: list[str] = []
    if draft.consumed_memory_candidate_ids:
        from app.memory.plane_read import mark_profile_entry_promoted

        for candidate_id in draft.consumed_memory_candidate_ids:
            try:
                if mark_profile_entry_promoted(
                    data_root,
                    agent_id,
                    entry_id=candidate_id,
                    promoted_to="skill",
                    target=draft.name,
                ):
                    promoted_memory_ids.append(candidate_id)
            except Exception as exc:  # noqa: BLE001 — marker failure is auditable, not fatal
                logger.warning("[skill_distiller] failed to mark memory candidate %s promoted: %s", candidate_id, exc)

    promoted_at = datetime.now(timezone.utc).isoformat()
    update_skill_candidate_record(
        workspace,
        workflow_signature=record.workflow_signature,
        skill_name=draft.name,
        blocker="provisional",
        last_status="provisional",
        last_note=draft.reason or "Entered provisional trial as a new skill.",
        last_updated_at=promoted_at,
    )
    record_skill_lifecycle_event(
        workspace,
        skill_name=draft.name,
        status="provisional",
        note=draft.reason or "Entered provisional trial as a new skill.",
    )
    state.last_promotion_at = promoted_at
    save_distiller_state(workspace, state)
    return {
        "status": "provisional",
        "processed_sessions": processed,
        "skill_name": draft.name,
        "workflow_signature": record.workflow_signature,
        "evolution_validation_passed": evolution_validation["passed"],
        "evolution_validation": evolution_validation.get("report_artifact"),
        "workflow_candidates_recorded": workflow_candidates_recorded,
        "promoted_memory_candidates": promoted_memory_ids,
        "artifact_gate_report": artifact_gate_report,
        "referee_review": _referee_review_payload(referee_review),
        **({"direct_candidate_id": direct_candidate.candidate_id} if direct_candidate else {}),
    }
