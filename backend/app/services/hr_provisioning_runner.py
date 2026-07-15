"""Owns resumable HR provisioning after canonical blueprint claim."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.handlers.hr import (
        Skill,
        ToolExecutionRequest,
    )


def _normalize_blueprint_trigger_config(trigger: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Normalize explicit trigger structure without inventing schedule semantics.

    The HR blueprint model owns the schedule it proposes.  This adapter may
    accept the legacy shorthand of a cron expression string, but it must never
    infer a schedule from a human-readable trigger name or another unrelated
    value.  Structurally incomplete triggers are retained in a disabled,
    reviewable state by the caller.
    """

    trigger_type = str(trigger.get("type", "cron") or "cron").strip().lower()
    raw_config = trigger.get("config", {})
    if isinstance(raw_config, dict):
        config = dict(raw_config)
    elif trigger_type == "cron" and isinstance(raw_config, str) and raw_config.strip():
        config = {"expr": raw_config.strip()}
    else:
        config = {}

    required_fields = {
        "cron": ("expr", "missing_cron_expr"),
        "interval": ("minutes", "missing_interval_minutes"),
        "once": ("at", "missing_once_at"),
        "poll": ("url", "missing_poll_url"),
    }
    required = required_fields.get(trigger_type)
    if required is None:
        return config, None

    field, hold_reason = required
    value = config.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return config, hold_reason
    return config, None


async def run_hr_provisioning(request: ToolExecutionRequest, *, support: Any) -> str:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    _append_hr_creation_t0_event = support._append_hr_creation_t0_event
    _build_blueprint_preview_payload = support._build_blueprint_preview_payload
    _build_create_employee_result = support._build_create_employee_result
    _claim_canonical_hr_blueprint = support._claim_canonical_hr_blueprint
    _dedupe_strings = support._dedupe_strings
    _install_external_skill_ref = support._install_external_skill_ref
    _parse_list = support._parse_list
    _refine_soul_inputs = support._refine_soul_inputs
    _resolve_employee_creation_model = support._resolve_employee_creation_model
    _resolve_employee_refinement_model = support._resolve_employee_refinement_model
    _stamp_hr_blueprint_trigger_exemption = support._stamp_hr_blueprint_trigger_exemption
    _trim_role_description_for_prompt_guard = support._trim_role_description_for_prompt_guard
    json = support.json
    logger = support.logger
    stage_external_skill_package_review_for_tenant = support.stage_external_skill_package_review_for_tenant
    uuid = support.uuid

    from app.services.hr_creation_service import HrCreationConflict

    try:
        draft_id, scope_tenant_id, user_id, session_id, args, claim = await _claim_canonical_hr_blueprint(request)
    except (HrCreationConflict, ValueError) as exc:
        code = getattr(exc, "code", "invalid_request")
        if request.arguments.get("_runtime_authority") is not None and code in {
            "blueprint_integrity_mismatch",
            "missing_confirmation_evidence",
            "runtime_authority_mismatch",
        }:
            raise
        message = getattr(exc, "message", str(exc))
        return json.dumps({"status": "error", "error": code, "message": message}, ensure_ascii=False)

    tenant_id = str(scope_tenant_id)

    name = (args.get("name") or "").strip()
    if not name or len(name) < 2:
        return "Error: name is required and must be at least 2 characters."
    if len(name) > 100:
        return "Error: name must be 100 characters or less."

    role_description = _trim_role_description_for_prompt_guard(args.get("role_description", ""))
    personality = args.get("personality", "")
    boundaries = args.get("boundaries", "")

    skill_names = _dedupe_strings([s for s in _parse_list(args.get("skill_names")) if isinstance(s, str)])
    mcp_server_ids = _dedupe_strings([s for s in _parse_list(args.get("mcp_server_ids")) if isinstance(s, str)])
    clawhub_slugs = _dedupe_strings([s for s in _parse_list(args.get("clawhub_slugs")) if isinstance(s, str)])
    permission_scope = args.get("permission_scope", "company")

    from app.services.heartbeat_policy import MANAGED_HEARTBEAT_ACTIVE_HOURS, managed_heartbeat_interval_minutes

    heartbeat_enabled = True
    heartbeat_interval = managed_heartbeat_interval_minutes()
    heartbeat_active_hours = MANAGED_HEARTBEAT_ACTIVE_HOURS
    # Triggers (scheduled tasks) — LLM may pass string or malformed data
    raw_triggers = args.get("triggers") or []
    if isinstance(raw_triggers, str):
        try:
            import json as _json

            raw_triggers = _json.loads(raw_triggers)
        except (ValueError, TypeError) as _trig_err:
            logger.warning("[HR] Failed to parse triggers JSON: %s — raw: %s", _trig_err, str(raw_triggers)[:100])
            raw_triggers = []
    triggers = [t for t in raw_triggers if isinstance(t, dict)]
    if raw_triggers and not triggers:
        logger.warning("[HR] All %d triggers dropped (not dict): %s", len(raw_triggers), str(raw_triggers)[:200])
    # New customization params
    welcome_message = args.get("welcome_message", "")
    preview_payload = _build_blueprint_preview_payload(args)
    skill_names = list(preview_payload["blueprint"]["effective_skill_names"])
    external_skill_refs = list(preview_payload["blueprint"]["external_skill_refs"])
    manual_steps = list(preview_payload["manual_steps"])
    warnings = list(preview_payload["warnings"])
    install_plan = []

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent, AgentPermission
    from app.models.skill import Skill
    from app.models.user import User
    from app.services.agent_identity_lifecycle import ensure_agent_identity
    from app.services.agent_manager import agent_manager
    from app.services.capability_install_service import (
        build_capability_install_plan,
        list_capability_installs,
        record_capability_install,
        record_capability_install_plan,
    )
    from app.services.capability_reuse_service import (
        reuse_existing_mcp_server_for_agent,
        reuse_existing_skill_for_agent,
    )
    from app.services.hr_creation_service import (
        derive_hr_provisioning_readiness,
        ensure_hr_provisioning_steps,
        load_hr_creation_draft,
        mark_hr_creation_completed_record,
        mark_hr_creation_failed_record,
        release_hr_creation_claim_record,
        renew_hr_creation_claim_record,
        transition_hr_provisioning_step_record,
    )

    install_plan = build_capability_install_plan(
        skill_names=skill_names,
        mcp_server_ids=mcp_server_ids,
        clawhub_slugs=clawhub_slugs,
        external_skill_refs=external_skill_refs,
    )

    try:
        # RLS 阶段1: agent creation reads/writes many policy-bearing tables
        # (users, agents, skills, llm_models, tenant_settings, tenants). Scope
        # the whole flow to the requesting tenant. The context tenant_id is the
        # primary source; when absent, fall back to the calling HR agent's
        # tenant via the audited single-row bypass (same tenant as the user for
        # self-service creation), so the User read below is still scoped.
        async with tenant_scoped_session(scope_tenant_id) as db:
            # Look up the calling user
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                raise HrCreationConflict("requester_missing", "Could not identify the requesting user.")

            effective_tenant_id = scope_tenant_id
            draft = await load_hr_creation_draft(
                db,
                draft_id=draft_id,
                tenant_id=scope_tenant_id,
                hr_agent_id=uuid.UUID(str(request.context.agent_id)),
                requested_by_user_id=user_id,
                session_id=session_id,
                for_update=True,
            )
            if claim.state == "completed":
                completed_agent_result = await db.execute(select(Agent).where(Agent.id == draft.created_agent_id))
                completed_agent = completed_agent_result.scalar_one_or_none()
                if completed_agent is None:
                    return json.dumps(
                        {
                            "status": "error",
                            "error": "completed_asset_missing",
                            "message": "The completed HR draft no longer references an existing agent.",
                        },
                        ensure_ascii=False,
                    )
                return _build_create_employee_result(
                    agent_id=str(completed_agent.id),
                    agent_name=completed_agent.name,
                    features=["idempotent_replay=true"],
                    skills_dir=str(agent_manager._agent_dir(completed_agent.id) / "skills"),
                    creation_state="ready",
                )

            steps = await ensure_hr_provisioning_steps(
                db,
                draft=draft,
                claim=claim,
                install_plan=install_plan,
            )
            steps_by_key = {step.step_key: step for step in steps}
            capability_steps = {
                (step.step_kind, step.source_key): step for step in steps if step.source_key is not None
            }
            validate_step = steps_by_key["validate"]
            if validate_step.status != "completed":
                transition_hr_provisioning_step_record(
                    validate_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={"blueprint_hash": draft.blueprint_hash, "validated_before_claim": True},
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()

            existing_agent_result = await db.execute(select(Agent).where(Agent.id == draft.created_agent_id))
            existing_agent = existing_agent_result.scalar_one_or_none()

            # Resolve a model only while creating the core row or producing the
            # one canonical refinement receipt. Recovery reuses that receipt;
            # it never asks the model to regenerate a previously committed soul.
            creation_model = await _resolve_employee_creation_model(db, effective_tenant_id)
            if not creation_model and existing_agent is None:
                model_step = steps_by_key["model"]
                transition_hr_provisioning_step_record(
                    model_step,
                    draft=draft,
                    claim=claim,
                    status="failed",
                    error_code="missing_creation_model",
                    error_message="No enabled LLM model is configured for this tenant.",
                )
                mark_hr_creation_failed_record(
                    draft,
                    claim=claim,
                    code="missing_creation_model",
                    message="No enabled LLM model is configured for this tenant.",
                )
                await db.commit()
                return (
                    f"❌ Cannot create agent '{name}': no LLM model configured for this tenant. "
                    "Please add at least one enabled LLM model in Enterprise Settings → LLM Pool."
                )
            primary_model_id = existing_agent.primary_model_id if existing_agent is not None else creation_model.id

            model_step = steps_by_key["model"]
            saved_refinement = dict((draft.provisioning_json or {}).get("refined_inputs") or {})
            if model_step.status == "completed" and saved_refinement:
                _refined = saved_refinement
            elif existing_agent is not None:
                # Legacy in-flight drafts predate the refinement receipt. The
                # persisted Agent row and confirmed blueprint are stronger
                # evidence than a fresh, potentially drifting model rewrite.
                _refined = {
                    "role_description": existing_agent.role_description or role_description,
                    "personality": personality,
                    "boundaries": boundaries,
                    "primary_users": list(preview_payload["blueprint"].get("primary_users", [])),
                    "core_outputs": list(preview_payload["blueprint"].get("core_outputs", [])),
                    "quality_standards": [],
                    "first_tasks": [],
                }
                transition_hr_provisioning_step_record(
                    model_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={"source": "legacy_agent_row", "primary_model_id": str(primary_model_id)},
                )
                draft.provisioning_json = {
                    **dict(draft.provisioning_json or {}),
                    "refined_inputs": _refined,
                }
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()
            else:
                transition_hr_provisioning_step_record(
                    model_step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"primary_model_id": str(primary_model_id)},
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()

                _hr_agent_r = await db.execute(select(Agent).where(Agent.id == request.context.agent_id))
                _hr_agent = _hr_agent_r.scalar_one_or_none()
                _llm_obj, _refine_model_source = await _resolve_employee_refinement_model(
                    db,
                    effective_tenant_id,
                    preferred_model_id=_hr_agent.primary_model_id if _hr_agent else None,
                    creation_model=creation_model,
                )
                _model_cfg = (
                    {
                        "provider": _llm_obj.provider,
                        "model": _llm_obj.model,
                        "api_key": _llm_obj.api_key,
                        "base_url": _llm_obj.base_url,
                    }
                    if _llm_obj
                    else {}
                )
                logger.info(
                    "[HR] Soul refinement using model: %s/%s (from %s)",
                    _model_cfg.get("provider", "?"),
                    _model_cfg.get("model", "?"),
                    _refine_model_source,
                )
                _refined = await _refine_soul_inputs(
                    name=name,
                    role_description=role_description,
                    personality=personality,
                    boundaries=boundaries,
                    primary_users=_parse_list(args.get("primary_users")),
                    core_outputs=_parse_list(args.get("core_outputs")),
                    model_config=_model_cfg,
                    usage_agent_id=_hr_agent.id if _hr_agent else None,
                    usage_tenant_id=tenant_id,
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                transition_hr_provisioning_step_record(
                    model_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={
                        "primary_model_id": str(primary_model_id),
                        "refinement_model_source": _refine_model_source,
                    },
                )
                draft.provisioning_json = {
                    **dict(draft.provisioning_json or {}),
                    "refined_inputs": _refined,
                }
                await db.commit()

            role_description = str(_refined.get("role_description") or role_description)
            personality = str(_refined.get("personality") or personality)
            boundaries = str(_refined.get("boundaries") or boundaries)

            resolved_extra_skills: list[Skill] = []
            if skill_names:
                from sqlalchemy import or_
                from sqlalchemy.orm import selectinload

                missing_skill_names: list[str] = []
                for sname in skill_names:
                    sr = await db.execute(
                        select(Skill)
                        .where(
                            Skill.folder_name == sname,
                            or_(
                                Skill.tenant_id == effective_tenant_id,
                                Skill.tenant_id.is_(None),
                            ),
                        )
                        .options(selectinload(Skill.files))
                    )
                    skill = sr.scalar_one_or_none()
                    if skill is None:
                        missing_skill_names.append(sname)
                    else:
                        resolved_extra_skills.append(skill)
                if missing_skill_names:
                    logger.warning("[HR] Skipping unavailable extra skills: %s", missing_skill_names)
                    warnings.append(
                        f"Skipped {len(missing_skill_names)} unavailable skill(s): "
                        + ", ".join(missing_skill_names)
                        + ". Use clawhub_slugs or external_skill_refs for marketplace skills."
                    )

            # Resolve tenant defaults
            default_max_triggers = 20
            default_min_poll = 5
            default_webhook_rate = 5
            tenant_obj = None
            if effective_tenant_id:
                from app.models.tenant import Tenant

                tenant_result = await db.execute(select(Tenant).where(Tenant.id == effective_tenant_id))
                tenant_obj = tenant_result.scalar_one_or_none()
                if tenant_obj:
                    default_max_triggers = tenant_obj.default_max_triggers or 20
                    default_min_poll = tenant_obj.min_poll_interval_floor or 5
                    default_webhook_rate = tenant_obj.max_webhook_rate_ceiling or 5

            # Create the agent — set last_heartbeat_at to now so the first
            # heartbeat fires after a full interval, giving MCP/workspace init time.
            from datetime import datetime as _dt, timezone as _tz

            core_step = steps_by_key["core"]
            if existing_agent is None:
                transition_hr_provisioning_step_record(
                    core_step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"deterministic_agent_id": True},
                )
                deterministic_agent_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hive://hr-creation/{draft.id}")
                agent = Agent(
                    id=deterministic_agent_id,
                    name=name,
                    role_description=role_description,
                    welcome_message=welcome_message or None,
                    creator_id=user.id,
                    owner_user_id=user.id,
                    sponsor_user_id=user.id,
                    tenant_id=effective_tenant_id,
                    agent_type="native",
                    agent_class="internal_tenant",
                    security_zone="standard",
                    primary_model_id=primary_model_id,
                    status="creating",
                    max_triggers=default_max_triggers,
                    min_poll_interval_min=default_min_poll,
                    webhook_rate_limit=default_webhook_rate,
                    heartbeat_enabled=heartbeat_enabled,
                    heartbeat_interval_minutes=heartbeat_interval,
                    heartbeat_active_hours=heartbeat_active_hours,
                    last_heartbeat_at=_dt.now(_tz.utc),
                )
                db.add(agent)
                await ensure_agent_identity(
                    db,
                    agent,
                    display_name=agent.name,
                    avatar_url=None,
                    rls_bypass_reason=f"HR digital employee identity bootstrap for tenant {effective_tenant_id}",
                    rls_bypass_actor_id=str(user.id),
                )

                if permission_scope == "self":
                    db.add(
                        AgentPermission(
                            agent_id=agent.id,
                            tenant_id=agent.tenant_id,
                            scope_type="user",
                            scope_id=user.id,
                            access_level="manage",
                        )
                    )
                else:
                    db.add(
                        AgentPermission(
                            agent_id=agent.id,
                            tenant_id=agent.tenant_id,
                            scope_type="company",
                            access_level="use",
                        )
                    )
                await db.flush()

                from app.services.tool_seeder import assign_default_tools_to_agent

                await assign_default_tools_to_agent(db, agent.id)
                await db.flush()
            else:
                agent = existing_agent

            if core_step.status != "completed":
                transition_hr_provisioning_step_record(
                    core_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={
                        "agent_id": str(agent.id),
                        "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                        "recovered_existing_row": existing_agent is not None,
                    },
                )
            draft.status = "provisioning"
            draft.created_agent_id = agent.id
            draft.provisioning_json = {
                **dict(draft.provisioning_json or {}),
                "core": "completed",
                "workspace": "completed" if steps_by_key["workspace"].status == "completed" else "pending",
                "default_skills": "completed" if steps_by_key["defaults"].status == "completed" else "pending",
                "t0_evidence": "completed" if steps_by_key["t0_evidence"].status == "completed" else "pending",
            }
            renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
            await db.commit()

            # Initialize agent file system (standard template)
            # Override blueprint with LLM-refined values for soul rendering and first-work setup.
            _bp = {
                **preview_payload["blueprint"],
                "primary_users": _refined.get("primary_users", preview_payload["blueprint"].get("primary_users", [])),
                "core_outputs": _refined.get("core_outputs", preview_payload["blueprint"].get("core_outputs", [])),
                "quality_standards": _refined.get("quality_standards", []),
                "first_tasks": _refined.get("first_tasks", []),
                "ready_now": list(preview_payload["ready_now"]),
                "deferred_capabilities": list(preview_payload["blueprint"].get("deferred_capabilities", [])),
                "manual_steps": manual_steps,
                "company_id": str(effective_tenant_id) if effective_tenant_id else "",
                "company_name": getattr(tenant_obj, "name", None) or "the company",
                "owner_id": str(user.id),
                "owner_name": user.display_name or user.username or str(user.id),
                "company_charter": preview_payload["blueprint"].get("company_charter", {}),
                "owner_agency_charter": preview_payload["blueprint"].get("owner_agency_charter", {}),
            }
            agent_dir = agent_manager._agent_dir(agent.id)
            workspace_step = steps_by_key["workspace"]
            if workspace_step.status != "completed":
                transition_hr_provisioning_step_record(
                    workspace_step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"workspace": str(agent_dir)},
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()
                await agent_manager.initialize_agent_files(
                    db,
                    agent,
                    personality=personality,
                    boundaries=boundaries,
                    blueprint=_bp,
                    repair_existing=True,
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                transition_hr_provisioning_step_record(
                    workspace_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={
                        "workspace": str(agent_dir),
                        "soul_path": str(agent_dir / "soul.md"),
                        "repairable": True,
                    },
                )
                await db.commit()

            defaults_step = steps_by_key["defaults"]
            defaults_already_completed = defaults_step.status == "completed"
            if not defaults_already_completed:
                transition_hr_provisioning_step_record(
                    defaults_step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"agent_id": str(agent.id)},
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()

            # Create triggers (scheduled tasks)
            if triggers and not defaults_already_completed:
                from app.models.trigger import AgentTrigger

                for trig in triggers:
                    trig_type = str(trig.get("type", "cron") or "cron").strip().lower()
                    raw_config, _config_hold_reason = _normalize_blueprint_trigger_config(trig)
                    _trigger_name = str(trig.get("name", "task") or "task").strip()
                    _trigger_reason = str(trig.get("reason", "") or "").strip()
                    raw_config = dict(raw_config)
                    if _config_hold_reason:
                        metadata = dict(raw_config.get("metadata") or {})
                        metadata["configuration_hold_reason"] = _config_hold_reason
                        raw_config["metadata"] = metadata
                        logger.warning(
                            "Holding structurally incomplete HR blueprint trigger '%s': %s",
                            _trigger_name,
                            _config_hold_reason,
                        )
                    raw_config.setdefault(
                        "trigger_class",
                        "event_wait" if trig_type in {"poll", "on_message", "webhook"} else "scheduled_job",
                    )
                    raw_config = _stamp_hr_blueprint_trigger_exemption(raw_config)
                    existing_trigger = await db.scalar(
                        select(AgentTrigger.id).where(
                            AgentTrigger.agent_id == agent.id,
                            AgentTrigger.name == _trigger_name,
                            AgentTrigger.type == trig_type,
                        )
                    )
                    if existing_trigger is not None:
                        continue
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            tenant_id=agent.tenant_id,
                            name=_trigger_name,
                            type=trig_type,
                            config=raw_config,
                            reason=_trigger_reason,
                            is_enabled=not bool(_config_hold_reason),
                        )
                    )
                await db.flush()

            # Kick-start: create ONE 'once' boot trigger so the new agent wakes
            # shortly after creation and starts its first task.
            _first_tasks = _refined.get("first_tasks", [])
            _boot_task = next((str(t).strip() for t in _first_tasks if str(t).strip()), "")
            if not _boot_task:
                _boot_task = str(args.get("focus_content", "")).strip()
            if _boot_task and not defaults_already_completed:
                from app.models.trigger import AgentTrigger

                _fire_at = (_dt.now(_tz.utc) + __import__("datetime").timedelta(seconds=30)).isoformat()
                existing_boot = await db.scalar(
                    select(AgentTrigger.id).where(
                        AgentTrigger.agent_id == agent.id,
                        AgentTrigger.name == "first_task_boot",
                        AgentTrigger.type == "once",
                    )
                )
                if existing_boot is None:
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            tenant_id=agent.tenant_id,
                            name="first_task_boot",
                            type="once",
                            config=_stamp_hr_blueprint_trigger_exemption(
                                {"at": _fire_at, "trigger_class": "scheduled_job"}
                            ),
                            reason=(
                                f"Read soul.md for your full mission. Start with this first task: {_boot_task}\n\n"
                                "Record progress and evidence in your work ledger as you go."
                            ),
                        )
                    )
                    await db.flush()
                    logger.info("[HR] Created boot trigger for agent %s: %s", agent.id, _boot_task[:80])

            # Copy default skills + requested skills
            from sqlalchemy.orm import selectinload

            default_skill_result = await db.execute(
                select(Skill).where(Skill.is_default).options(selectinload(Skill.files))
            )
            all_skills_to_copy: list[Skill] = list(default_skill_result.scalars().all())

            for skill in resolved_extra_skills:
                if skill not in all_skills_to_copy:
                    all_skills_to_copy.append(skill)

            from app.services.skill_installation import install_active_skill_package

            if not defaults_already_completed:
                for skill in all_skills_to_copy:
                    install_active_skill_package(
                        workspace=agent_dir,
                        folder_name=skill.folder_name,
                        files=[{"path": sf.path, "content": sf.content} for sf in skill.files],
                        source=f"hr_registry_skill:{skill.id}",
                        overwrite=True,
                    )

            # Audit the asset creation once; replaying an already-completed
            # defaults step must not fabricate another creation event.
            if not defaults_already_completed:
                try:
                    from app.core.policy import write_audit_event

                    await write_audit_event(
                        db,
                        event_type="agent.created",
                        severity="info",
                        actor_type="user",
                        actor_id=user.id,
                        tenant_id=effective_tenant_id or user.tenant_id or uuid.UUID(int=0),
                        action="create_agent",
                        resource_type="agent",
                        resource_id=agent.id,
                        details={"name": agent.name, "created_via": "hr_agent"},
                    )
                except Exception as _audit_exc:
                    logger.warning("Audit write failed for hr agent.created: %s", _audit_exc)

            if not defaults_already_completed:
                from app.services.ai_assets import register_agent_asset

                await register_agent_asset(
                    db,
                    agent,
                    change_source="create",
                    actor_user_id=user.id,
                    change_message="Digital employee created by HR Agent",
                )
            renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
            if defaults_step.status != "completed":
                transition_hr_provisioning_step_record(
                    defaults_step,
                    draft=draft,
                    claim=claim,
                    status="completed",
                    receipt={
                        "trigger_count": len(triggers),
                        "default_skill_count": len(all_skills_to_copy),
                    },
                )
            draft.status = "provisioning"
            draft.provisioning_json = {
                **dict(draft.provisioning_json or {}),
                "core": "completed",
                "workspace": "completed",
                "default_skills": "completed",
                "optional_capabilities": "running",
                "t0_evidence": "completed" if steps_by_key["t0_evidence"].status == "completed" else "pending",
            }
            await db.commit()

            t0_step = steps_by_key["t0_evidence"]
            if session_id and t0_step.status != "completed":
                transition_hr_provisioning_step_record(
                    t0_step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"session_id": str(session_id)},
                )
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()
                try:
                    t0_result = _append_hr_creation_t0_event(
                        hr_agent_id=request.context.agent_id,
                        created_agent_id=agent.id,
                        created_agent_name=agent.name,
                        session_id=session_id,
                        tenant_id=effective_tenant_id or user.tenant_id,
                        user_id=user.id,
                        blueprint_hash=preview_payload["blueprint_hash"],
                        preview_payload=preview_payload,
                        installed_skill_names=skill_names,
                        trigger_count=len(triggers),
                        creation_draft_id=draft.id,
                    )
                except Exception as t0_exc:
                    warnings.append(
                        "HR creation evidence projection failed; the agent exists and the draft remains auditable."
                    )
                    renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                    transition_hr_provisioning_step_record(
                        t0_step,
                        draft=draft,
                        claim=claim,
                        status="failed",
                        error_code="t0_projection_failed",
                        error_message=str(t0_exc),
                    )
                    draft.provisioning_json = {**dict(draft.provisioning_json or {}), "t0_evidence": "failed"}
                    logger.warning(
                        "[HR] Failed to append hr_agent_created T0 event for agent %s: %s",
                        agent.id,
                        t0_exc,
                    )
                else:
                    renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                    transition_hr_provisioning_step_record(
                        t0_step,
                        draft=draft,
                        claim=claim,
                        status="completed",
                        receipt={
                            "event_id": t0_result.event_id,
                            "segment_id": t0_result.segment_id,
                            "sequence": t0_result.sequence,
                        },
                    )
                    draft.provisioning_json = {**dict(draft.provisioning_json or {}), "t0_evidence": "completed"}
                await db.commit()

            pending_install_plan = []
            for item in install_plan:
                step = capability_steps[(str(item["kind"]), str(item["source_key"]))]
                if step.status in {"completed", "waiting_review"}:
                    continue
                transition_hr_provisioning_step_record(
                    step,
                    draft=draft,
                    claim=claim,
                    status="running",
                    receipt={"source_key": str(item["source_key"])},
                )
                pending_install_plan.append(item)
            if pending_install_plan:
                renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                await db.commit()
                try:
                    await record_capability_install_plan(
                        agent_id=agent.id,
                        plan=pending_install_plan,
                        installed_via="hr_agent",
                    )
                except Exception as install_plan_err:
                    logger.warning("[HR] Failed to persist capability install plan: %s", install_plan_err)

            for skill in resolved_extra_skills:
                try:
                    await record_capability_install(
                        agent_id=agent.id,
                        kind="platform_skill",
                        source_key=skill.folder_name,
                        status="installed",
                        installed_via="hr_agent",
                        display_name=skill.name,
                        metadata_json={"phase": "copied_to_agent"},
                    )
                except Exception as skill_record_err:
                    logger.warning("[HR] Failed to record installed skill %s: %s", skill.folder_name, skill_record_err)

            pending_mcp_server_ids = [
                str(item["source_key"]) for item in pending_install_plan if item["kind"] == "mcp_server"
            ]
            pending_clawhub_slugs = [
                str(item["source_key"]) for item in pending_install_plan if item["kind"] == "clawhub_skill"
            ]
            pending_external_skill_refs = [
                str(item["source_key"]) for item in pending_install_plan if item["kind"] == "external_skill_url"
            ]

            # Install MCP servers (after commit, so agent exists in DB)
            logger.info(
                "[HR] Post-commit install phase: mcp=%s, clawhub=%s",
                pending_mcp_server_ids,
                pending_clawhub_slugs,
            )
            mcp_results = []
            if pending_mcp_server_ids:
                from app.services.resource_discovery import import_mcp_from_smithery, _get_smithery_api_key

                # Pre-fetch API key from global config (not from the new agent which has empty config)
                _smithery_key = await _get_smithery_api_key(None)
                for server_id in pending_mcp_server_ids:
                    renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                    await db.commit()
                    try:
                        reused = await reuse_existing_mcp_server_for_agent(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            server_id=server_id,
                            config={"smithery_api_key": _smithery_key} if _smithery_key else None,
                        )
                        if reused is not None:
                            mcp_results.append(f"⏭️ {server_id}: reused existing tenant MCP tools")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={
                                    "phase": "reused_existing_tenant_tools",
                                    "tool_count": reused["tool_count"],
                                },
                            )
                            logger.info(f"[HR] Reused existing MCP {server_id} for agent {agent.id}")
                            continue
                        _mcp_config = {"smithery_api_key": _smithery_key} if _smithery_key else None
                        result = await import_mcp_from_smithery(server_id, agent.id, config=_mcp_config)
                        if isinstance(result, str) and "❌" in result:
                            mcp_results.append(f"⚠️ {server_id}: {result}")
                            warnings.append(f"MCP install not ready: {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="install_rejected",
                                error_message=result,
                            )
                            logger.warning(f"[HR] MCP install rejected for {server_id}: {result}")
                        elif isinstance(result, dict) and result.get("error"):
                            mcp_results.append(f"⚠️ {server_id}: {result['error']}")
                            warnings.append(f"MCP install not ready: {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="install_error",
                                error_message=str(result["error"]),
                            )
                            logger.warning(f"[HR] MCP install error for {server_id}: {result['error']}")
                        else:
                            mcp_results.append(f"✅ {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={"phase": "post_commit"},
                            )
                            logger.info(f"[HR] Installed MCP {server_id} for agent {agent.id}")
                    except Exception as mcp_err:
                        mcp_results.append(f"⚠️ {server_id}: {mcp_err}")
                        warnings.append(f"MCP install failed: {server_id}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(mcp_err),
                            )
                        except Exception as record_err:
                            logger.warning(
                                "[HR] Failed to record MCP install failure for %s: %s", server_id, record_err
                            )
                        logger.warning(f"[HR] MCP install failed for {server_id}: {mcp_err}")

            # Install ClawHub skills (after commit, so agent exists on disk)
            logger.info(
                "[HR] ClawHub install phase: %d slugs to install: %s",
                len(pending_clawhub_slugs),
                pending_clawhub_slugs,
            )
            clawhub_results = []
            if pending_clawhub_slugs:
                import httpx
                from app.api.skills import CLAWHUB_BASE, _fetch_github_directory, _get_github_token

                ch_tenant = str(effective_tenant_id) if effective_tenant_id else None
                ch_token = await _get_github_token(ch_tenant)
                for slug in pending_clawhub_slugs:
                    renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                    await db.commit()
                    try:
                        reused_skill = await reuse_existing_skill_for_agent(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            folder_name=slug,
                        )
                        if reused_skill is not None:
                            clawhub_results.append(f"⏭️ {slug}: reused existing platform skill")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={"phase": "reused_existing_registry_skill"},
                            )
                            logger.info(f"[HR] Reused existing skill {slug} for agent {agent.id}")
                            continue
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(f"{CLAWHUB_BASE}/v1/skills/{slug}")
                            if resp.status_code == 429:
                                import asyncio as _asyncio

                                await _asyncio.sleep(2)
                                resp = await client.get(f"{CLAWHUB_BASE}/v1/skills/{slug}")
                            if resp.status_code != 200:
                                clawhub_results.append(f"⚠️ {slug}: ClawHub HTTP {resp.status_code}")
                                warnings.append(f"ClawHub install not ready: {slug}")
                                await record_capability_install(
                                    agent_id=agent.id,
                                    kind="clawhub_skill",
                                    source_key=slug,
                                    status="failed",
                                    installed_via="hr_agent",
                                    error_code=f"http_{resp.status_code}",
                                    error_message=f"ClawHub HTTP {resp.status_code}",
                                )
                                logger.warning(f"[HR] ClawHub API returned {resp.status_code} for {slug}")
                                continue
                            try:
                                meta = resp.json()
                            except Exception as _json_err:
                                logger.warning("[HR] ClawHub JSON parse failed for %s: %s", slug, _json_err)
                                clawhub_results.append(f"⚠️ {slug}: invalid ClawHub response")
                                warnings.append(f"ClawHub install not ready: {slug}")
                                await record_capability_install(
                                    agent_id=agent.id,
                                    kind="clawhub_skill",
                                    source_key=slug,
                                    status="failed",
                                    installed_via="hr_agent",
                                    error_code="invalid_response",
                                    error_message=str(_json_err),
                                )
                                continue
                        handle = meta.get("owner", {}).get("handle", "").lower()
                        if not handle:
                            clawhub_results.append(f"⚠️ {slug}: no owner handle")
                            warnings.append(f"ClawHub install not ready: {slug}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="missing_owner_handle",
                                error_message="ClawHub metadata missing owner handle",
                            )
                            continue
                        github_path = f"skills/{handle}/{slug}"
                        files = await _fetch_github_directory("openclaw", "skills", github_path, "main", ch_token)
                        review_result = await stage_external_skill_package_review_for_tenant(
                            tenant_id=effective_tenant_id,
                            created_by_user_id=None,
                            source_uri=f"clawhub:{slug}",
                            folder_name=slug,
                            files=files,
                            source_format="clawhub_skill",
                        )
                        clawhub_results.append(f"{slug}: {review_result['status']}")
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="clawhub_skill",
                            source_key=slug,
                            status=review_result["status"],
                            installed_via="hr_agent",
                            metadata_json={
                                "phase": "trust_gate_review",
                                "review_id": review_result.get("review_id"),
                                "skill_guard": review_result.get("skill_guard"),
                                "files_written": 0,
                            },
                        )
                        logger.info("[HR] Staged ClawHub skill %s for Trust Gate review on agent %s", slug, agent.id)
                    except Exception as ch_err:
                        clawhub_results.append(f"⚠️ {slug}: {ch_err}")
                        warnings.append(f"ClawHub install failed: {slug}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(ch_err),
                            )
                        except Exception as record_err:
                            logger.warning("[HR] Failed to record ClawHub install failure for %s: %s", slug, record_err)
                        logger.warning(f"[HR] ClawHub install failed for {slug}: {ch_err}")

            external_skill_results = []
            if pending_external_skill_refs:
                for ref in pending_external_skill_refs:
                    renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
                    await db.commit()
                    try:
                        result = await _install_external_skill_ref(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            ref=ref,
                        )
                        external_skill_results.append(f"{result['folder_name']}: {result['status']}")
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="external_skill_url",
                            source_key=ref,
                            status=result["status"],
                            installed_via="hr_agent",
                            display_name=result["folder_name"],
                            metadata_json={
                                "phase": "trust_gate_review",
                                "files_written": result["files_written"],
                                "review_id": result.get("review_id"),
                            },
                        )
                    except Exception as ext_err:
                        external_skill_results.append(f"⚠️ {ref}: {ext_err}")
                        warnings.append(f"External skill install failed: {ref}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="external_skill_url",
                                source_key=ref,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(ext_err),
                            )
                        except Exception as record_err:
                            logger.warning("[HR] Failed to record external skill failure for %s: %s", ref, record_err)

            # Reconcile the per-capability journal from the canonical install
            # registry. The worker never guesses completion from display text.
            install_records = await list_capability_installs(agent_id=agent.id)
            install_records_by_key = {
                (str(record.get("kind")), str(record.get("source_key"))): record for record in install_records
            }
            renew_hr_creation_claim_record(draft, claim=claim, lease_seconds=600)
            for item in install_plan:
                key = (str(item["kind"]), str(item["source_key"]))
                step = capability_steps[key]
                if step.status == "completed":
                    continue
                record = install_records_by_key.get(key)
                record_status = str((record or {}).get("status") or "missing")
                if record_status in {"installed", "active", "enabled"}:
                    transition_hr_provisioning_step_record(
                        step,
                        draft=draft,
                        claim=claim,
                        status="completed",
                        receipt={"install_record": record},
                    )
                elif record_status in {"review_required", "pending_review", "quarantined"}:
                    transition_hr_provisioning_step_record(
                        step,
                        draft=draft,
                        claim=claim,
                        status="waiting_review",
                        receipt={"install_record": record},
                    )
                    warnings.append(f"Capability awaits Trust Gate review: {item['source_key']}")
                else:
                    error_message = str((record or {}).get("error_message") or f"install status: {record_status}")
                    transition_hr_provisioning_step_record(
                        step,
                        draft=draft,
                        claim=claim,
                        status="failed",
                        receipt={"install_record": record or {}},
                        error_code=str((record or {}).get("error_code") or "install_incomplete"),
                        error_message=error_message,
                    )
                    warnings.append(f"Capability provisioning failed: {item['source_key']} ({error_message})")
            await db.commit()

            # Build response
            features = [f"name='{agent.name}'"]
            if triggers:
                trigger_names = [t.get("name", "?") for t in triggers]
                features.append(f"triggers={trigger_names}")
            if skill_names:
                features.append(f"extra_skills={skill_names}")
            if mcp_results:
                features.append(f"mcp={mcp_results}")
            if clawhub_results:
                features.append(f"clawhub={clawhub_results}")
            if external_skill_results:
                features.append(f"external_skills={external_skill_results}")

            readiness = derive_hr_provisioning_readiness(steps, ignore_finalize=True)
            if not readiness.ready:
                warnings.append("Required provisioning incomplete: " + ", ".join(readiness.blocking_step_keys))
                release_hr_creation_claim_record(
                    draft,
                    claim=claim,
                    status="failed" if readiness.creation_state == "provisioning_failed" else "provisioning",
                )
                draft.provisioning_json = {
                    **dict(draft.provisioning_json or {}),
                    "required_blockers": list(readiness.blocking_step_keys),
                    "optional_capabilities": "completed_with_warnings" if warnings else "completed",
                }
                await db.commit()
                return _build_create_employee_result(
                    agent_id=str(agent.id),
                    agent_name=agent.name,
                    features=features,
                    skills_dir=str(agent_dir / "skills"),
                    creation_state=readiness.creation_state,
                    warnings=_dedupe_strings(warnings),
                    manual_steps=manual_steps,
                )

            await agent_manager.start_container(db, agent)
            mark_hr_creation_completed_record(
                draft,
                claim=claim,
                steps=steps,
                agent_id=agent.id,
                provisioning={
                    **dict(draft.provisioning_json or {}),
                    "required_blockers": [],
                    "optional_capabilities": "completed_with_warnings" if warnings else "completed",
                },
            )
            await db.commit()
            final_readiness = derive_hr_provisioning_readiness(steps)
            return _build_create_employee_result(
                agent_id=str(agent.id),
                agent_name=agent.name,
                features=features,
                skills_dir=str(agent_dir / "skills"),
                creation_state=(
                    "ready_with_warnings" if warnings or manual_steps or final_readiness.warning_step_keys else "ready"
                ),
                warnings=_dedupe_strings(warnings),
                manual_steps=manual_steps,
            )

    except Exception as e:
        logger.error(f"[HR] create_digital_employee failed: {e}", exc_info=True)
        try:
            async with tenant_scoped_session(scope_tenant_id) as failure_db:
                failed_draft = await load_hr_creation_draft(
                    failure_db,
                    draft_id=draft_id,
                    tenant_id=scope_tenant_id,
                    hr_agent_id=uuid.UUID(str(request.context.agent_id)),
                    requested_by_user_id=user_id,
                    session_id=session_id,
                    for_update=True,
                )
                if failed_draft.status != "completed":
                    mark_hr_creation_failed_record(
                        failed_draft,
                        claim=claim,
                        code="provisioning_failed",
                        message=str(e),
                    )
                    await failure_db.commit()
        except Exception as state_exc:
            logger.error("[HR] Failed to persist HR creation failure state: %s", state_exc, exc_info=True)
        return "Error: failed to create the digital employee. Please try again or contact support."
