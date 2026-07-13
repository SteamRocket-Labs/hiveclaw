"""Heartbeat service — platform-managed memory maintenance loop.

Periodically runs deterministic memory maintenance and direct T3 consolidation.
User-facing autonomous patrols belong to triggers and wake policies; heartbeat
itself is always-on platform infrastructure and does not run a full agent loop.

Runs as a background task inside the FastAPI process.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daemon_concurrency import run_bounded
from app.core.events import get_redis
from app.database import enter_rls_bypass, tenant_scoped_session
from app.memory.t2.read_model import load_t2_package_snapshots, render_t2_package_snapshots
from app.services.heartbeat_t3_core import run_heartbeat_t3_core
from app.services.heartbeat_policy import managed_heartbeat_interval_minutes
from app.services.runtime_task_service import (
    build_restart_reconciliation_metadata,
    build_restart_replay_contract,
    build_restart_replay_journal_entry,
    create_runtime_task_record,
    list_active_runtime_task_records,
    merge_restart_replay_journal,
    update_runtime_task_record,
)
from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetRunCreate, RuntimeBudgetService
from app.services.runtime_tenant_admission import admit_agent_runtime_tenant
from app.services.tenant_resolver import resolve_tenant_for_agent

# Legacy human-readable protocol note. Runtime consolidation uses
# app/templates/T3_CONSOLIDATOR.md via app.services.heartbeat_t3_core.
_HEARTBEAT_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
_HEARTBEAT_LEASE_TTL_SECONDS = 600
_heartbeat_leases: dict[uuid.UUID, datetime] = {}

_HEARTBEAT_SCORE_RUBRIC_SUFFIX = """

<heartbeat_score_rubric>
Use [SCORE:0-10] as a calibrated action-quality score, not a feeling:
- 0-1: noop, no eligible input, or no material change.
- 2-3: failure or bootstrap/recovery attempt; useful diagnostics may exist but
  no durable progress was completed.
- 4-6: useful small action with evidence, bounded scope, and no external side
  effects requiring approval.
- 7-8: high-value evidence-backed action, such as accepted curation pitch,
  verified workspace artifact, or reusable candidate with source refs.
- 9-10: exceptional, verified, reusable impact with clear source refs,
  rollback/audit path, and no unresolved risk.
</heartbeat_score_rubric>
"""


_heartbeat_tick_counts: dict[uuid.UUID, int] = {}
_t2_mtimes: dict[uuid.UUID, dict[str, float]] = {}

_HEARTBEAT_T2_FULL_MAX_CHARS = 24_000
_HEARTBEAT_T2_INCREMENTAL_MAX_CHARS = 16_000
_HEARTBEAT_T3_MAX_CHARS = 8_000
_HEARTBEAT_EVOLUTION_CONTEXT_MAX_CHARS = 16_000


def _format_heartbeat_exception(exc: BaseException) -> str:
    message = str(exc).strip()
    exc_type = type(exc).__name__
    return f"{exc_type}: {message}" if message else exc_type


def _log_heartbeat_error(agent_id: uuid.UUID, error_text: str) -> None:
    logger.opt(exception=True).error("Heartbeat error for agent {}: {}", agent_id, error_text)


def _truncate_heartbeat_text(text: str, max_chars: int, label: str) -> str:
    """Trim a single heartbeat section while preserving its opening and latest tail."""
    if len(text) <= max_chars:
        return text

    marker = (
        f"\n\n[... {label} truncated to fit heartbeat context budget; omitted {len(text) - max_chars:,} chars ...]\n\n"
    )
    if max_chars <= len(marker) + 200:
        return text[: max(0, max_chars - len(marker))] + marker[:max_chars]

    head_chars = max(100, int((max_chars - len(marker)) * 0.6))
    tail_chars = max_chars - len(marker) - head_chars
    return text[:head_chars] + marker + text[-tail_chars:]


async def _create_heartbeat_runtime_task(agent_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None) -> str | None:
    try:
        task_id = uuid.uuid4().hex
        trace_id = f"heartbeat:{task_id}"
        side_effect_risk = "internal_governed"
        budget_run_id = None
        try:
            service = RuntimeBudgetService()
            policy = await service.resolve_policy(
                RuntimeBudgetPolicyLookup(
                    tenant_id=tenant_id,
                    source="heartbeat",
                    profile="heartbeat",
                    agent_id=agent_id,
                )
            )
            budget_run = await service.create_run(
                RuntimeBudgetRunCreate(
                    tenant_id=tenant_id,
                    root_run_kind="heartbeat_tick",
                    root_run_key=task_id,
                    source="heartbeat",
                    profile="heartbeat",
                    policy_id=getattr(policy, "id", None),
                    root_runtime_task_id=uuid.UUID(task_id),
                    root_agent_id=agent_id,
                    enforcement_mode=str(getattr(policy, "enforcement_mode", None) or "enforce"),
                    fail_mode=str(getattr(policy, "fail_mode", None) or "fail_closed"),
                    max_tokens=getattr(policy, "max_tokens", None),
                    max_cache_miss_tokens=getattr(policy, "max_cache_miss_tokens", None),
                    max_subagents=getattr(policy, "max_subagents", None),
                    max_team_sessions=getattr(policy, "max_team_sessions", None),
                    max_delegations=getattr(policy, "max_delegations", None),
                    max_background_tasks=getattr(policy, "max_background_tasks", None),
                    max_continuation_wakes=getattr(policy, "max_continuation_wakes", None),
                    max_provider_calls=getattr(policy, "max_provider_calls", None),
                    max_failures=getattr(policy, "max_failures", None),
                    max_needs_reconciliation=getattr(policy, "max_needs_reconciliation", None),
                    max_child_failure_ratio=getattr(policy, "max_child_failure_ratio", None),
                    max_parent_invocations=getattr(policy, "max_parent_invocations", None),
                    policy_snapshot={
                        "policy_id": str(getattr(policy, "id", "")),
                        "scope_type": getattr(policy, "scope_type", None),
                        "source": getattr(policy, "source", None),
                        "profile": getattr(policy, "profile", None),
                        "max_team_sessions": getattr(policy, "max_team_sessions", None),
                        "default_child_token_reservation": getattr(policy, "default_child_token_reservation", None),
                        "default_llm_call_token_reservation": getattr(
                            policy, "default_llm_call_token_reservation", None
                        ),
                        "policy_json": getattr(policy, "policy_json", None),
                    },
                )
            )
            budget_run_id = budget_run.id
        except Exception as budget_exc:
            logger.warning("[Heartbeat] Runtime budget root creation failed for {}: {}", agent_id, budget_exc)
        metadata = {
            "source": "heartbeat",
            "agent_id": str(agent_id),
            "tenant_id": str(tenant_id) if tenant_id else None,
            "runtime_task_id": task_id,
            "request_id": str(uuid.UUID(task_id)),
            "trace_id": trace_id,
            "budget_run_id": str(budget_run_id) if budget_run_id else None,
            "resumable_heartbeat": True,
            "resume_after_restart": True,
            "side_effect_risk": side_effect_risk,
            "restart_replay_contract": build_restart_replay_contract(
                task_type="heartbeat",
                task_id=task_id,
                side_effect_risk=side_effect_risk,
                trace_id=trace_id,
            ),
        }
        metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type="heartbeat",
                task_id=task_id,
                side_effect_risk=side_effect_risk,
                phase="spawn_intent_recorded",
                trace_id=trace_id,
            ),
        )
        return await create_runtime_task_record(
            task_id=task_id,
            task_type="heartbeat",
            status="running",
            parent_agent_id=agent_id,
            prompt="Heartbeat self-evolution tick",
            trace_id=trace_id,
            metadata_json=metadata,
            budget_run_id=budget_run_id,
            budget_admission_status="root" if budget_run_id else None,
        )
    except Exception as exc:
        logger.warning("[Heartbeat] Failed to create RuntimeTask for {}: {}", agent_id, exc)
        return None


async def _update_heartbeat_runtime_task(
    runtime_task_id: str | None,
    *,
    status: str,
    result_summary: str,
    session_id: str | None = None,
    metadata_json: dict | None = None,
) -> None:
    if not runtime_task_id:
        return
    fields = {
        "status": status,
        "result_summary": result_summary[:2000],
        "metadata_json": metadata_json or {},
    }
    if session_id:
        fields["child_session_id"] = session_id
    try:
        await update_runtime_task_record(runtime_task_id, **fields)
    except Exception as exc:
        logger.warning("[Heartbeat] Failed to update RuntimeTask {}: {}", runtime_task_id, exc)


async def _skip_heartbeat_runtime_task(
    runtime_task_id: str | None,
    *,
    skip_reason: str,
    result_summary: str,
    metadata_json: dict | None = None,
) -> None:
    metadata = {"skip_reason": skip_reason}
    metadata.update(metadata_json or {})
    await _update_heartbeat_runtime_task(
        runtime_task_id,
        status="skipped",
        result_summary=result_summary,
        metadata_json=metadata,
    )


async def _mark_heartbeat_runtime_task_needs_reconciliation(
    runtime_task_id: str,
    *,
    metadata: dict | None,
    blocker: str,
    summary: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> None:
    await update_runtime_task_record(
        runtime_task_id,
        status="needs_reconciliation",
        result_summary=summary,
        metadata_json=build_restart_reconciliation_metadata(
            metadata,
            task_type="heartbeat",
            task_id=runtime_task_id,
            blocker=blocker,
            summary=summary,
            trace_id=trace_id,
            session_id=session_id,
        ),
    )


async def resume_persisted_heartbeat_runs(*, limit: int = 50) -> list[str]:
    """Resume heartbeat runs that were still queued before session binding."""

    resumed: list[str] = []
    records = await list_active_runtime_task_records(limit=limit, statuses=("pending", "running"))
    for record in records:
        if record.get("task_type") != "heartbeat":
            continue
        run_id = str(record.get("task_id") or "").strip()
        if not run_id:
            continue
        metadata = dict(record.get("metadata") or {})
        if not metadata.get("resume_after_restart") or not metadata.get("resumable_heartbeat"):
            continue
        trace_id = str(record.get("trace_id") or metadata.get("trace_id") or "")
        session_id = str(record.get("child_session_id") or metadata.get("session_id") or "").strip()
        if session_id:
            await _mark_heartbeat_runtime_task_needs_reconciliation(
                run_id,
                metadata=metadata,
                blocker="direct_core_audit_session_bound",
                summary=(
                    "Heartbeat was interrupted after binding an audit session; replay could duplicate T3 artifact writes. "
                    "Reconciliation is required before retry."
                ),
                trace_id=trace_id,
                session_id=session_id,
            )
            continue
        try:
            agent_id = uuid.UUID(str(record.get("parent_agent_id") or metadata.get("agent_id") or ""))
        except (TypeError, ValueError, AttributeError):
            await _mark_heartbeat_runtime_task_needs_reconciliation(
                run_id,
                metadata=metadata,
                blocker="missing_heartbeat_parent_agent",
                summary="Heartbeat could not be resumed after restart because parent agent id is unavailable.",
                trace_id=trace_id,
            )
            continue
        tenant_id = None
        raw_tenant_id = str(metadata.get("tenant_id") or "").strip()
        if raw_tenant_id:
            try:
                tenant_id = uuid.UUID(raw_tenant_id)
            except ValueError:
                tenant_id = None
        if tenant_id is None:
            tenant_id = await resolve_tenant_for_agent(agent_id)
        side_effect_risk = str(metadata.get("side_effect_risk") or "internal_governed")
        resume_metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type="heartbeat",
                task_id=run_id,
                side_effect_risk=side_effect_risk,
                phase="resume_intent_recorded",
                trace_id=trace_id,
            ),
        )
        await update_runtime_task_record(
            run_id,
            status="running",
            metadata_json={
                "resumed_after_restart": True,
                "restart_replay_contract": metadata.get("restart_replay_contract"),
                "restart_replay_journal": resume_metadata.get("restart_replay_journal"),
            },
        )
        asyncio.create_task(
            run_bounded("heartbeat", _execute_heartbeat(agent_id, tenant_id=tenant_id, runtime_task_id=run_id))
        )
        resumed.append(run_id)
    return resumed


def _reset_heartbeat_session(agent_id: uuid.UUID) -> None:
    """Reset heartbeat maintenance caches after dream, day change, or process restart."""
    _heartbeat_tick_counts.pop(agent_id, None)
    _t2_mtimes.pop(agent_id, None)
    logger.info("[Heartbeat] Maintenance cache reset for {}", agent_id)


def _read_t2_full(agent_id: uuid.UUID) -> str:
    """Read canonical T2 Segment Packages for first tick initialization."""
    from app.config import get_settings

    snapshots, current_mtimes = load_t2_package_snapshots(Path(get_settings().AGENT_DATA_DIR), agent_id, limit=12)
    _t2_mtimes[agent_id] = current_mtimes
    snapshot = render_t2_package_snapshots(snapshots)
    return _truncate_heartbeat_text(
        snapshot or "(no canonical T2 segment packages yet)",
        _HEARTBEAT_T2_FULL_MAX_CHARS,
        "T2 full snapshot",
    )


def _read_t3_summary(agent_id: uuid.UUID) -> str:
    """Two-plane accepted-memory summary (reference for dedup during curation)."""
    from app.config import get_settings

    memory_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory"
    parts: list[str] = []
    for rel in ("self/self.md", "profiles/owner.md", "profiles/collaborators.md", "profiles/domain.md"):
        fpath = memory_dir / rel
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    parts.append(f"### {rel}\n{content[:500]}")
            except Exception as exc:
                logger.debug("[Heartbeat] Failed to read profile plane {}: {}", fpath, exc)
    for subdir, label in (("knowledge", "knowledge pages"), ("milestones", "milestone pages")):
        directory = memory_dir / subdir
        if directory.exists():
            slugs = sorted(path.stem for path in directory.glob("*.md"))
            if slugs:
                parts.append(f"### {label} ({len(slugs)})\n" + ", ".join(slugs[:40]))
    return _truncate_heartbeat_text(
        "\n\n".join(parts) if parts else "(no accepted two-plane memory yet)",
        _HEARTBEAT_T3_MAX_CHARS,
        "T3 summary",
    )


def _read_pending_t3_intake(agent_id: uuid.UUID) -> str:
    """Stage/read canonical pending T3 intake from Segment Packages and explicit overlay."""
    from app.config import get_settings
    from app.memory.t3_consolidation import discover_pending_t3_sources, stage_pending_t3_consolidation_job

    data_root = Path(get_settings().AGENT_DATA_DIR)
    pending_t3 = discover_pending_t3_sources(agent_id=agent_id, data_root=data_root)
    if not pending_t3.has_sources:
        return ""
    t3_job = stage_pending_t3_consolidation_job(agent_id=agent_id, data_root=data_root)
    try:
        rel_job_dir = t3_job.job_dir.relative_to(data_root / str(agent_id)).as_posix()
    except ValueError:
        rel_job_dir = t3_job.job_dir.as_posix()
    lines = [
        "## T3 Consolidation Job Ready",
        f"- job_id: `{t3_job.job_id}`",
        f"- job_status: `{t3_job.status}`",
        f"- job_dir: `{rel_job_dir}`",
        f"- reviewed_t2_packages: {len(pending_t3.package_dirs)}",
        f"- active_explicit_overlay_entries: {len(pending_t3.explicit_entry_ids)}",
        "- direct core reads `source_bundle.json` and `t3_neighborhood.md`",
        "- direct core writes `consolidation_pitch.md` and `revised_patch.md` artifacts",
        "- direct Memory Gate review writes `review.md` for the latest `revised_patch.md`",
        "- Platform Gate commits accepted T3 and marks sources absorbed only after fresh review validation",
    ]
    if t3_job.issues:
        lines.append("- issues:")
        lines.extend(f"  - {issue}" for issue in t3_job.issues)
    return "\n".join(lines)


def _read_incremental_t2(agent_id: uuid.UUID) -> str:
    """Read only changed canonical T2 Segment Packages since last tick."""
    from app.config import get_settings

    snapshots, current_mtimes = load_t2_package_snapshots(
        Path(get_settings().AGENT_DATA_DIR),
        agent_id,
        known_mtimes=_t2_mtimes.get(agent_id, {}),
        limit=8,
    )
    _t2_mtimes[agent_id] = current_mtimes
    return _truncate_heartbeat_text(
        render_t2_package_snapshots(snapshots),
        _HEARTBEAT_T2_INCREMENTAL_MAX_CHARS,
        "incremental T2 snapshot",
    )


def _get_default_heartbeat_instruction() -> str:
    """Read default heartbeat instruction from templates/HEARTBEAT.md (single source of truth)."""
    try:
        return _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8").strip()
    except Exception as exc:
        # Template-read failure is a packaging bug — the whole T2→T3 curation SOP
        # silently degrades to a one-liner stub while the distiller keeps running.
        logger.error("[Heartbeat] HEARTBEAT.md template read failed, using stub SOP: {}", exc)
        return (
            "[Heartbeat] Review your recent work and memory, do one evidence-backed useful thing, "
            "reply HEARTBEAT_OK if nothing needed."
        )


def _compose_heartbeat_instruction(base_instruction: str) -> str:
    return base_instruction + _HEARTBEAT_SCORE_RUBRIC_SUFFIX


def _try_acquire_heartbeat_lease(
    agent_id: uuid.UUID,
    *,
    now: datetime | None = None,
    ttl_seconds: int = _HEARTBEAT_LEASE_TTL_SECONDS,
) -> bool:
    """Acquire a per-agent heartbeat lease, expiring stale entries automatically."""
    current = now or datetime.now(timezone.utc)
    lease_started_at = _heartbeat_leases.get(agent_id)
    if lease_started_at is not None and (current - lease_started_at).total_seconds() < ttl_seconds:
        return False
    _heartbeat_leases[agent_id] = current
    return True


def _release_heartbeat_lease(agent_id: uuid.UUID) -> None:
    _heartbeat_leases.pop(agent_id, None)


async def _try_acquire_heartbeat_lease_async(
    agent_id: uuid.UUID,
    *,
    now: datetime | None = None,
    ttl_seconds: int = _HEARTBEAT_LEASE_TTL_SECONDS,
) -> bool:
    lease_key = f"heartbeat_lease:{agent_id}"
    try:
        redis = await get_redis()
        acquired = await redis.set(lease_key, (now or datetime.now(timezone.utc)).isoformat(), ex=ttl_seconds, nx=True)
        if acquired:
            _heartbeat_leases[agent_id] = now or datetime.now(timezone.utc)
        return bool(acquired)
    except Exception as exc:
        logger.warning("[Heartbeat] Redis lease unavailable; heartbeat lease fails closed for {}: {}", agent_id, exc)
        return False


async def _release_heartbeat_lease_async(agent_id: uuid.UUID) -> None:
    lease_key = f"heartbeat_lease:{agent_id}"
    try:
        redis = await get_redis()
        await redis.delete(lease_key)
    except Exception as exc:
        logger.debug("[Heartbeat] Redis lease release skipped: {}", exc)
    finally:
        _release_heartbeat_lease(agent_id)


def _is_in_active_hours(active_hours: str, tz_name: str = "UTC") -> bool:
    """Check if current time is within the agent's active hours.

    Format: "HH:MM-HH:MM" (e.g., "09:00-18:00")
    Uses agent's configured timezone (defaults to UTC).
    """
    try:
        from zoneinfo import ZoneInfo

        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., "22:00-06:00")
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True  # Default to active if parsing fails


def _load_heartbeat_instruction(agent_id: uuid.UUID) -> str:
    """Read agent's HEARTBEAT.md, fallback to templates/HEARTBEAT.md (single source of truth)."""
    from app.config import get_settings

    settings = get_settings()

    for ws_root in [
        Path("/tmp/hive_workspaces") / str(agent_id),
        Path(settings.AGENT_DATA_DIR) / str(agent_id),
    ]:
        hb_file = ws_root / "HEARTBEAT.md"
        if not hb_file.exists():
            continue
        try:
            custom = hb_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            logger.debug(f"Failed to read HEARTBEAT.md from {hb_file}: {e}")
            custom = ""
        if not custom:
            break
        return _compose_heartbeat_instruction(custom)

    return _compose_heartbeat_instruction(_get_default_heartbeat_instruction())


def _parse_heartbeat_outcome(reply: str | None) -> tuple[str, int | None]:
    """Parse structured outcome from heartbeat reply.

    Expects LLM to output [OUTCOME:noop|action_taken|curated|failure] [SCORE:0-10].
    Falls back to heuristics if structured tags are missing.

    Returns (outcome_type, score).
    """
    if not reply:
        return "noop", None

    # Try structured tag first: [OUTCOME:action_taken]
    outcome_match = re.search(r"\[OUTCOME:\s*(noop|action_taken|curated|failure)\s*\]", reply, re.IGNORECASE)
    score_match = re.search(r"\[SCORE:\s*(\d+)\s*\]", reply)

    if outcome_match:
        outcome = outcome_match.group(1).lower()
    else:
        # Fallback heuristics — only when structured tags are absent
        # Default to noop (not action_taken) to avoid inflating success rate
        upper_reply = reply.upper()
        if "CURATED" in upper_reply:
            outcome = "curated"
        elif any(kw in upper_reply for kw in ("WROTE", "CREATED", "UPDATED", "POSTED", "SENT", "FIXED")):
            outcome = "action_taken"
        else:
            outcome = "noop"

    if score_match:
        score = min(int(score_match.group(1)), 10)
    else:
        # Fallback score based on outcome type — prevents silent None in
        # telemetry while keeping semantic learning on governed memory paths.
        _OUTCOME_FALLBACK_SCORES = {"action_taken": 5, "failure": 2, "noop": 0}
        score = _OUTCOME_FALLBACK_SCORES.get(outcome, 0)

    return outcome, score


def _heartbeat_outcome_lane(outcome_type: str) -> str:
    normalized = (outcome_type or "").strip().lower()
    return {
        "curated": "memory_curation",
        "action_taken": "agent_action",
        "noop": "idle",
        "failure": "failure",
        "crash": "failure",
    }.get(normalized, "unknown")


def _heartbeat_counts_as_useful(outcome_type: str, score: int | None) -> bool:
    return (outcome_type or "").strip().lower() in {"action_taken", "curated"} and (score is None or score >= 5)


def _heartbeat_action_label(outcome_type: str, summary: str) -> str:
    lane = _heartbeat_outcome_lane(outcome_type)
    if lane in {"agent_action", "memory_curation"}:
        return summary[:100]
    return "none"


_HEARTBEAT_REFLECTION_MAX_REPLY_CHARS = 48_000
_HEARTBEAT_REFLECTION_MAX_CONTEXT_MESSAGES = 24


def _truncate_heartbeat_reflection_text(text: str, max_chars: int = _HEARTBEAT_REFLECTION_MAX_REPLY_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    return (
        text[:head_chars]
        + "\n...[truncated middle of heartbeat reflection; preserved head and final conclusion]...\n"
        + text[-tail_chars:]
    )


def _should_route_heartbeat_reflection(outcome_type: str, reply: str | None) -> tuple[bool, str | None]:
    normalized = (outcome_type or "").strip().lower()
    text = (reply or "").strip()
    if not text:
        return False, "empty_reply"
    if normalized == "noop":
        return False, "low_signal_noop"
    if normalized not in {"action_taken", "curated", "failure", "crash"}:
        return False, "unsupported_outcome"
    return True, None


def _build_heartbeat_reflection_messages(
    *,
    runtime_messages: list[dict] | None,
    reply: str | None,
    metadata: dict | None = None,
) -> list[dict]:
    """Build full-enough reflection input for Learning Brain / Extractor.

    `metadata` is intentionally not copied into message content. Source refs and
    governance context travel in hook metadata, while the LLM receives the
    model-authored heartbeat reflection and recent tool/chat context.
    """

    del metadata
    messages: list[dict] = []
    for raw in (runtime_messages or [])[-_HEARTBEAT_REFLECTION_MAX_CONTEXT_MESSAGES:]:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "unknown")
        content = raw.get("content")
        if isinstance(content, str):
            content = _truncate_heartbeat_reflection_text(content)
        messages.append(
            {
                key: value
                for key, value in {
                    "role": role,
                    "content": content,
                    "tool_calls": raw.get("tool_calls"),
                    "tool_call_id": raw.get("tool_call_id"),
                }.items()
                if value is not None
            }
        )

    final_reply = _truncate_heartbeat_reflection_text(reply or "")
    if final_reply and not (
        messages and messages[-1].get("role") == "assistant" and str(messages[-1].get("content") or "") == final_reply
    ):
        messages.append({"role": "assistant", "content": final_reply})
    return messages


async def _route_heartbeat_reflection_learning(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    agent_name: str,
    session_id: uuid.UUID | str,
    runtime_task_id: str | None,
    assistant_message_id: str | None,
    runtime_messages: list[dict] | None,
    reply: str | None,
    outcome_type: str,
    outcome_lane: str,
    score: int | None,
    await_hook: bool = False,
) -> dict[str, object]:
    """Route non-noop heartbeat reflection into the LLM-primary learning lanes."""

    from app.memory.metrics import record_heartbeat_reflection

    should_route, skip_reason = _should_route_heartbeat_reflection(outcome_type, reply)
    if not should_route:
        record_heartbeat_reflection("skipped_low_signal")
        return {"status": "skipped", "reason": skip_reason or "low_signal"}

    source_refs = [f"heartbeat_session:{session_id}"]
    if runtime_task_id:
        source_refs.append(f"runtime_task:{runtime_task_id}")
    if assistant_message_id:
        source_refs.append(f"chat_message:{assistant_message_id}")

    messages = _build_heartbeat_reflection_messages(
        runtime_messages=runtime_messages,
        reply=reply,
        metadata={"source_refs": source_refs},
    )
    if not messages:
        record_heartbeat_reflection("skipped_low_signal")
        return {"status": "skipped", "reason": "empty_messages"}

    record_heartbeat_reflection("processed")
    from app.runtime import hooks as runtime_hooks

    hook_kwargs = {
        "agent_id": agent_id,
        "session_id": str(session_id),
        "messages": messages,
        "source": "heartbeat_reflection",
        "metadata": {
            "tenant_id": str(tenant_id) if tenant_id else None,
            "agent_name": agent_name or "Agent",
            "heartbeat_outcome": outcome_type,
            "heartbeat_outcome_lane": outcome_lane,
            "heartbeat_score": score,
            "source_refs": source_refs,
            "runtime_task_id": runtime_task_id,
            "assistant_message_id": assistant_message_id,
            "source": "heartbeat_reflection",
            "final_response": (reply or "")[:2000],
            "learning_boundary": "llm_judges_platform_governs",
        },
    }

    async def _emit() -> None:
        await runtime_hooks.emit_hook(runtime_hooks.HookEvent.RESPONSE_COMPLETE, **hook_kwargs)

    if await_hook:
        await _emit()
        return {"status": "emitted", "source_refs": source_refs}

    task = asyncio.create_task(_emit())

    def _on_done(done_task: asyncio.Task[None]) -> None:
        try:
            done_task.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Heartbeat] Background reflection learning hook failed for {}: {}", agent_id, exc)

    task.add_done_callback(_on_done)
    return {"status": "scheduled", "source_refs": source_refs}


_SKILL_OPPORTUNITY_COOLDOWN_TICKS = 5  # ~3.75 hours at 45-minute ticks
_SKILL_OPPORTUNITY_STATE_FILENAME = "skill_opportunity_cooldown.json"
_SKILL_OPPORTUNITY_IGNORED_TOOLS = {
    "read_file",
    "write_file",
    "list_files",
    "edit_file",
    "save_memory",
    "search_memory",
}


def _load_skill_opportunity_state(ws_root) -> dict:
    import json

    if ws_root is None:
        return {}
    path = ws_root / "evolution" / _SKILL_OPPORTUNITY_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[Heartbeat] failed to read skill opportunity state: {}", exc)
        return {}


def _save_capability_opportunity_state(ws_root, *, tick: int, tools: list[str]) -> None:
    import json

    if ws_root is None:
        return
    try:
        evo_dir = ws_root / "evolution"
        evo_dir.mkdir(parents=True, exist_ok=True)
        (evo_dir / _SKILL_OPPORTUNITY_STATE_FILENAME).write_text(
            json.dumps({"tick": tick, "tools": sorted(tools)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("[Heartbeat] failed to write skill opportunity state: {}", exc)


def _skill_already_covers_tools(ws_root, frequent_tools: list[str]) -> str | None:
    """If an existing skill declares tools ⊇ frequent_tools, return its name (skip suggestion)."""
    if ws_root is None or not frequent_tools:
        return None
    try:
        from app.skills import SkillRegistry, WorkspaceSkillLoader

        loader = WorkspaceSkillLoader()
        registry = SkillRegistry()
        registry.register_many(loader.load_from_workspace(ws_root))
    except Exception as exc:
        logger.debug("[Heartbeat] skill coverage check failed: {}", exc)
        return None

    target = set(frequent_tools)
    for name in registry.names():
        parsed = registry.resolve(name)
        declared = set(parsed.metadata.declared_tools or ())
        if target.issubset(declared):
            return name
    return None


async def _build_evolution_context(
    agent_id: uuid.UUID,
    recent_activities: list,
    tick_count: int = 0,
    *,
    owner_id: uuid.UUID | str | None = None,
    owner_name: str | None = None,
    company_id: uuid.UUID | str | None = None,
    company_name: str | None = None,
) -> str:
    """Build structured learning context from activity logs and governed memory paths.

    This feeds the heartbeat prompt with bounded runtime signals without reviving
    the retired ``evolution/scorecard.md`` and ``evolution/lineage.md`` semantic
    stores.
    """
    from collections import Counter

    parts: list[str] = []

    # 1. Read non-semantic runtime context from canonical workspace.
    ws_root = _get_canonical_workspace(agent_id)
    if ws_root:
        # Read compaction summary — context the agent lost during mid-loop compression
        compaction_path = ws_root / "runtime_artifacts" / "compaction_summary.md"
        if not compaction_path.exists():
            compaction_path = ws_root / "workspace" / "compaction_summary.md"
        if compaction_path.exists():
            try:
                compaction = compaction_path.read_text(encoding="utf-8", errors="replace").strip()
                if compaction:
                    parts.append(f"\n---\n## Last Session Compaction Summary\n{compaction[:2000]}")
            except Exception as e:
                logger.debug(f"Failed to read compaction summary: {e}")

        # No fallback needed — _get_canonical_workspace already resolved the right path

    try:
        pending_t3_intake = _read_pending_t3_intake(agent_id)
        if pending_t3_intake:
            parts.append("\n---\n" + pending_t3_intake)
    except Exception as exc:
        logger.warning("[Heartbeat] Failed to stage T3 consolidation job for {}: {}", agent_id, exc)

    # 2. Compute pattern summary from activity logs
    if recent_activities:
        error_count = sum(1 for a in recent_activities if a.action_type == "error")
        heartbeat_count = sum(1 for a in recent_activities if a.action_type == "heartbeat")
        tool_count = sum(1 for a in recent_activities if a.action_type == "tool_call")
        total = len(recent_activities)

        # Detect repeated failure patterns
        error_summaries = [a.summary[:80] for a in recent_activities if a.action_type == "error"]
        repeated_errors = [
            f"  - '{err}' (x{count})" for err, count in Counter(error_summaries).most_common(3) if count > 1
        ]

        # Tool usage frequency
        tool_names = []
        for a in recent_activities:
            if a.action_type == "tool_call" and a.detail_json:
                tool_name = a.detail_json.get("tool", "")
                if tool_name:
                    tool_names.append(tool_name)
        top_tools = [f"  - {name} (x{count})" for name, count in Counter(tool_names).most_common(5)]

        # Include error details (not just summaries) for learning
        error_details = []
        for a in recent_activities:
            if a.action_type == "error" and a.detail_json:
                detail = a.detail_json.get("error", "") or a.detail_json.get("message", "")
                if detail:
                    error_details.append(f"  - {str(detail)[:300]}")
        error_details = error_details[:5]  # Top 5 most recent errors

        pattern_section = (
            f"\n---\n## Activity Pattern Analysis (auto-computed, last {total} activities)\n"
            f"- Errors: {error_count} ({error_count * 100 // max(total, 1)}%)\n"
            f"- Heartbeats logged: {heartbeat_count}\n"
            f"- Tool calls: {tool_count}\n"
        )
        if repeated_errors:
            pattern_section += (
                "- **Repeated failures** (MUST NOT retry these approaches):\n" + "\n".join(repeated_errors) + "\n"
            )
        if error_details:
            pattern_section += "- **Recent error details** (learn from these):\n" + "\n".join(error_details) + "\n"
        if top_tools:
            pattern_section += "- Top tools used:\n" + "\n".join(top_tools) + "\n"

        parts.append(pattern_section)

        # Skill candidate hint — detect repeated tool-use patterns worth codifying.
        # This is a candidate signal only; heartbeat itself has no tool surface.
        _SKILL_THRESHOLD = 3  # same tool combo used 3+ times → suggest skill
        if top_tools and tool_count >= 6:
            # Check if any tool appears frequently enough to be worth a skill
            frequent_tools = [
                name
                for name, count in Counter(tool_names).most_common(3)
                if count >= _SKILL_THRESHOLD and name not in _SKILL_OPPORTUNITY_IGNORED_TOOLS
            ]

            should_push = bool(frequent_tools)
            suppression_note: str | None = None

            if should_push:
                # Coverage check — skip if an existing skill already declares these tools.
                covered_by = _skill_already_covers_tools(ws_root, frequent_tools)
                if covered_by:
                    should_push = False
                    suppression_note = f"skill '{covered_by}' already covers tools {sorted(frequent_tools)}"

            if should_push:
                # Cooldown — skip if the same tool set was suggested recently.
                state = _load_skill_opportunity_state(ws_root)
                last_tick = int(state.get("tick", 0)) if isinstance(state.get("tick"), (int, float)) else 0
                last_tools = sorted(state.get("tools", []) or [])
                if (
                    last_tools == sorted(frequent_tools)
                    and tick_count
                    and tick_count - last_tick < _SKILL_OPPORTUNITY_COOLDOWN_TICKS
                ):
                    should_push = False
                    suppression_note = (
                        f"cooldown: same tools suggested at tick {last_tick} "
                        f"(<{_SKILL_OPPORTUNITY_COOLDOWN_TICKS} ticks ago)"
                    )

            if should_push and frequent_tools:
                parts.append(
                    "\n---\n## Skill Candidate Opportunity\n"
                    f"You have used these tools repeatedly: {', '.join(frequent_tools)}.\n"
                    "If the workflow around them is genuinely reusable, record it as a candidate "
                    "signal. The skill distillation lane decides promotion:\n"
                    "1. Include a `skill_candidate` capability block in the active "
                    "`consolidation_pitch.md` or `revised_patch.md` artifact.\n"
                    "2. Include source refs pointing at the sessions/evidence where the workflow repeated.\n"
                    "3. A good candidate captures the *workflow* (multiple tools in sequence), not a single tool or one-off note."
                )
                if tick_count:
                    _save_capability_opportunity_state(ws_root, tick=tick_count, tools=list(frequent_tools))
            elif suppression_note:
                logger.debug(
                    "[Heartbeat] skill opportunity suppressed for {}: {}",
                    agent_id,
                    suppression_note,
                )

    # Cold start note: heartbeat is not a full agent bootstrap loop.
    non_heartbeat_activities = [a for a in recent_activities if a.action_type != "heartbeat"]
    is_cold_start = len(non_heartbeat_activities) < 3

    if is_cold_start:
        # Detect repeated bootstrap failures — use sliding window (not consecutive-only)
        # to catch intermittent failure patterns like [ok, fail, ok, fail, fail]
        recent_heartbeats = [a for a in recent_activities if a.action_type == "heartbeat"]
        total_failures = sum(
            1 for hb in recent_heartbeats[:6] if (hb.detail_json or {}).get("outcome_type", "") in ("crash", "failure")
        )

        if total_failures >= 5:
            # M-19: Hard cap — stop retrying bootstrap (5 of 6 recent heartbeats failed)
            parts.append(
                "\n---\n## Bootstrap Exhausted (10 failures)\n"
                "Bootstrap has failed repeatedly. Stop attempting bootstrap actions.\n"
                "Proceed directly with normal heartbeat: review your recent work and memory, then do one small evidence-backed task.\n"
                "Output: [OUTCOME:noop] [SCORE:1]"
            )
        elif total_failures >= 3:
            parts.append(
                "\n---\n## Bootstrap Recovery (auto-seeded)\n"
                "Your previous bootstrap attempts failed. Evolution files have been\n"
                "retired as a recovery surface. Skip bootstrapping and proceed with\n"
                "the normal heartbeat protocol using governed memory/session evidence.\n"
                "Focus on ONE simple action: review your recent work and memory, then do something small with evidence.\n"
                "Output: [OUTCOME:action_taken] [SCORE:3]"
            )
        else:
            parts.append(
                "\n---\n## Bootstrap Mode (first heartbeats)\n"
                "You have very little activity history. This is normal for a new agent.\n"
                "Heartbeat does not perform bootstrap actions. It waits for reviewed T2 evidence "
                "or explicit memory overlay entries before direct T3 consolidation."
            )

    return _truncate_heartbeat_text(
        "\n\n".join(parts) if parts else "",
        _HEARTBEAT_EVOLUTION_CONTEXT_MAX_CHARS,
        "heartbeat evolution context",
    )


def _get_canonical_workspace(agent_id: uuid.UUID) -> "Path | None":
    """Return the single canonical workspace path for an agent.

    Priority: AGENT_DATA_DIR (persistent) > /tmp (ephemeral).
    Syncs from /tmp → AGENT_DATA_DIR if /tmp has newer files.
    """
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    persistent = Path(settings.AGENT_DATA_DIR) / str(agent_id)
    ephemeral = Path("/tmp/hive_workspaces") / str(agent_id)

    # If persistent exists, it's canonical
    if persistent.exists():
        return persistent

    if ephemeral.exists():
        return ephemeral

    return None


async def _touch_last_heartbeat(agent_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> None:
    """Update last_heartbeat_at even on early return to prevent infinite re-triggering."""
    try:
        from app.models.agent import Agent as _Agent

        async with tenant_scoped_session(tenant_id) as _db:
            _result = await _db.execute(select(_Agent).where(_Agent.id == agent_id))
            _agent = _result.scalar_one_or_none()
            if _agent:
                _agent.last_heartbeat_at = datetime.now(timezone.utc)
                await _db.commit()
    except Exception as _exc:
        logger.debug(f"[Heartbeat] Failed to touch last_heartbeat_at for {agent_id}: {_exc}")


def _run_memory_lifecycle_maintenance(agent_id: uuid.UUID, *, now: datetime | None = None) -> dict:
    from app.config import get_settings
    from app.memory.lifecycle_maintenance import run_memory_lifecycle_maintenance

    return run_memory_lifecycle_maintenance(Path(get_settings().AGENT_DATA_DIR), agent_id, now=now)


async def _run_t2_job_sweep(agent_id: uuid.UUID):
    """C9-1: pick up held/failed T2 package jobs on the heartbeat cadence."""
    from app.config import get_settings
    from app.memory.t2.job_sweep import sweep_t2_jobs

    return await sweep_t2_jobs(agent_id=agent_id, data_root=Path(get_settings().AGENT_DATA_DIR))


async def _run_consolidation_debt_refresh(agent_id: uuid.UUID):
    """C9-2: refresh the consolidation-debt ledger and alert on stalls."""
    from app.config import get_settings
    from app.memory.consolidation_debt import (
        DEFAULT_EXPLICIT_AGE_ALERT_HOURS,
        DEFAULT_PENDING_AGE_ALERT_HOURS,
        refresh_consolidation_debt,
    )

    settings = get_settings()
    return await refresh_consolidation_debt(
        agent_id=agent_id,
        data_root=Path(settings.AGENT_DATA_DIR),
        pending_age_alert_hours=float(
            getattr(settings, "MEMORY_DEBT_PENDING_AGE_ALERT_HOURS", DEFAULT_PENDING_AGE_ALERT_HOURS)
        ),
        explicit_age_alert_hours=float(
            getattr(settings, "MEMORY_DEBT_EXPLICIT_AGE_ALERT_HOURS", DEFAULT_EXPLICIT_AGE_ALERT_HOURS)
        ),
    )


async def _run_t2_retention(agent_id: uuid.UUID):
    """C9-3: archive cold unreferenced T2 packages (never delete) on the heartbeat cadence."""
    from app.config import get_settings
    from app.memory.t2_retention import DEFAULT_ARCHIVE_AFTER_DAYS, run_t2_retention

    settings = get_settings()
    return await run_t2_retention(
        agent_id=agent_id,
        data_root=Path(settings.AGENT_DATA_DIR),
        archive_after_days=float(getattr(settings, "MEMORY_RETENTION_ARCHIVE_AFTER_DAYS", DEFAULT_ARCHIVE_AFTER_DAYS)),
    )


async def _run_chat_artifact_snapshot_retention(agent_id: uuid.UUID, *, db=None) -> dict:
    """Remove expired unreferenced chat artifact snapshots on the heartbeat cadence."""
    from app.config import get_settings
    from app.services.chat_artifact_delivery import (
        DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS,
        cleanup_chat_artifact_snapshots_for_agent,
    )

    settings = get_settings()
    if db is not None:
        if not isinstance(db, AsyncSession):
            return {"schema": "chat_artifact_snapshot_gc.v1", "skipped": "non_async_session"}
        return await cleanup_chat_artifact_snapshots_for_agent(
            db=db,
            agent_id=agent_id,
            workspace_root=Path(settings.AGENT_DATA_DIR) / str(agent_id),
            retention_days=float(
                getattr(
                    settings,
                    "CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS",
                    DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS,
                )
            ),
        )
    async with (
        tenant_scoped_session(None) as db,
        enter_rls_bypass(db, reason="chat artifact snapshot retention across tenants"),
    ):
        return await cleanup_chat_artifact_snapshots_for_agent(
            db=db,
            agent_id=agent_id,
            workspace_root=Path(settings.AGENT_DATA_DIR) / str(agent_id),
            retention_days=float(
                getattr(
                    settings,
                    "CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS",
                    DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS,
                )
            ),
        )


async def _run_convergence_dirtiness_refresh(agent_id: uuid.UUID):
    """工序 4 (Part F): measure profile-plane dirtiness for the convergence loop."""
    from app.config import get_settings
    from app.memory.convergence import (
        DEFAULT_MAX_CHARS_PER_FILE,
        DEFAULT_MAX_RETIRED_ENTRIES,
        refresh_convergence_dirtiness,
    )

    settings = get_settings()
    return refresh_convergence_dirtiness(
        agent_id=agent_id,
        data_root=Path(settings.AGENT_DATA_DIR),
        max_chars_per_file=float(
            getattr(settings, "MEMORY_CONVERGENCE_MAX_CHARS_PER_FILE", DEFAULT_MAX_CHARS_PER_FILE)
        ),
        max_retired_entries=int(
            getattr(settings, "MEMORY_CONVERGENCE_MAX_RETIRED_ENTRIES", DEFAULT_MAX_RETIRED_ENTRIES)
        ),
    )


async def _run_growth_report_refresh(agent_id: uuid.UUID, tenant_id: uuid.UUID | None):
    """J2: refresh the zero-LLM growth report (eval-system-spec §2.1) so the
    reflection prompt can read fresh numbers. Opens its own tenant-scoped
    session for the DB-side metrics — the maintenance batch's session must
    not be consumed by side reads."""
    from app.config import get_settings
    from app.services.growth_report import refresh_growth_report

    settings = get_settings()
    async with tenant_scoped_session(tenant_id) as growth_db:
        return await refresh_growth_report(
            data_root=Path(settings.AGENT_DATA_DIR),
            agent_id=agent_id,
            db=growth_db,
        )


async def _run_heartbeat_core_and_persist(
    *,
    agent,
    tenant_id: uuid.UUID | None,
    model,
    session_id: uuid.UUID,
    runtime_task_id: str | None,
    heartbeat_session_id: str | None,
    agent_participant_id: uuid.UUID | None,
    tick_count: int,
) -> None:
    """Run direct T3 consolidation outside the preparatory DB session."""
    from app.config import get_settings
    from app.models.audit import ChatMessage

    _HEARTBEAT_TIMEOUT_SECONDS = 300
    result = await asyncio.wait_for(
        run_heartbeat_t3_core(
            agent_id=agent.id,
            tenant_id=tenant_id,
            data_root=Path(get_settings().AGENT_DATA_DIR),
            model=model,
        ),
        timeout=_HEARTBEAT_TIMEOUT_SECONDS,
    )
    outcome_type = result.outcome_type
    outcome_lane = _heartbeat_outcome_lane(outcome_type)
    heartbeat_score = result.score
    reply = (
        f"Heartbeat direct T3 core: {result.summary}\n\n"
        f"[OUTCOME:{outcome_type}] [SCORE:{heartbeat_score}]\n"
        f"status: {result.status}\n"
        f"job_id: {result.job_id or '(none)'}\n"
        f"artifacts: {', '.join(result.artifact_paths) if result.artifact_paths else '(none)'}"
    )
    assistant_message_id: str | None = None
    async with tenant_scoped_session(tenant_id) as db2:
        assistant_message = ChatMessage(
            agent_id=agent.id,
            tenant_id=tenant_id,
            conversation_id=str(session_id),
            role="assistant",
            content=reply or "",
            user_id=agent.creator_id,
            participant_id=agent_participant_id,
        )
        db2.add(assistant_message)
        await db2.flush()
        assistant_message_id = str(assistant_message.id)
        await db2.commit()

    await _touch_last_heartbeat(agent.id, tenant_id)

    from app.services.activity_logger import log_activity

    summary = result.summary[:120] if result.summary else "empty"
    await log_activity(
        agent.id,
        "heartbeat",
        f"Heartbeat [{outcome_type}]: {summary}",
        detail={
            "reply": reply[:500] if reply else "",
            "outcome_type": outcome_type,
            "outcome_lane": outcome_lane,
            "score": heartbeat_score,
            "session_id": str(session_id),
            "runtime": "direct_t3_core",
            "status": result.status,
            "job_id": result.job_id,
            "skip_reason": result.skip_reason,
            "platform_gate_status": result.platform_gate_status,
            "issues": list(result.issues),
            "artifact_paths": list(result.artifact_paths),
        },
    )

    try:
        from app.runtime.hooks import HookEvent, emit_hook

        await emit_hook(
            HookEvent.HEARTBEAT_TICK_END,
            agent_id=agent.id,
            session_id=str(session_id),
            messages=[],
            source="heartbeat",
            metadata={
                "tick": tick_count,
                "outcome": outcome_type,
                "outcome_lane": outcome_lane,
                "score": heartbeat_score,
                "distillation_scope": "direct_t3_core",
                "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
                "runtime_task_id": runtime_task_id,
                "assistant_message_id": assistant_message_id,
                "job_id": result.job_id,
                "status": result.status,
                "skip_reason": result.skip_reason,
            },
        )
    except Exception as _hook_err:
        logger.debug("[Heartbeat] HEARTBEAT_TICK_END hook failed (non-fatal): {}", _hook_err)

    try:
        await _run_growth_report_refresh(agent.id, tenant_id)
    except Exception as _growth_err:
        logger.warning("[Heartbeat] Growth report refresh failed for {}: {}", agent.id, _growth_err)

    score_str = f" score={heartbeat_score}" if heartbeat_score is not None else ""
    logger.info(f"💓 Heartbeat for {agent.name}: {outcome_type}{score_str} — {summary}")
    task_status = "skipped" if result.status == "skipped" else "completed"
    await _update_heartbeat_runtime_task(
        runtime_task_id,
        status=task_status,
        result_summary=f"Heartbeat [{outcome_type}]: {summary}",
        session_id=heartbeat_session_id,
        metadata_json={
            "outcome": outcome_type,
            "outcome_lane": outcome_lane,
            "score": heartbeat_score,
            "runtime": "direct_t3_core",
            "status": result.status,
            "job_id": result.job_id,
            "skip_reason": result.skip_reason,
            "platform_gate_status": result.platform_gate_status,
            "issues": list(result.issues),
            "artifact_paths": list(result.artifact_paths),
        },
    )


async def _execute_heartbeat(
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    lease_acquired: bool = False,
    runtime_task_id: str | None = None,
):
    """Execute a single heartbeat for an agent.

    Creates a hidden audit session, runs deterministic maintenance, then calls
    the direct T3 consolidation core. The model never enters the full agent
    runtime and receives no tool executor.

    ``tenant_id`` is threaded from ``_heartbeat_tick`` (which already filtered
    on it) so every session here can pin the RLS GUC — under enforced
    (non-owner) RLS a bare session would fail-closed even on the agent's own
    rows. Falls back to an audited bypass read when omitted (e.g. an isolated
    re-invocation without the tick's tenant in scope).
    """
    if tenant_id is None:
        admission = await admit_agent_runtime_tenant(agent_id, source="heartbeat")
        if not admission.ok:
            await _skip_heartbeat_runtime_task(
                runtime_task_id,
                skip_reason=admission.reason_code,
                result_summary=admission.message,
                metadata_json=admission.metadata(),
            )
            return
        tenant_id = admission.tenant_id
    heartbeat_session_id: str | None = None
    lease_held = lease_acquired
    if not lease_held:
        lease_held = await _try_acquire_heartbeat_lease_async(agent_id)
        if not lease_held:
            logger.info("[Heartbeat] Skip duplicate in-flight heartbeat for {}", agent_id)
            runtime_task_id = runtime_task_id or await _create_heartbeat_runtime_task(agent_id, tenant_id=tenant_id)
            await _skip_heartbeat_runtime_task(
                runtime_task_id,
                skip_reason="duplicate_in_flight",
                result_summary="Skipped heartbeat because another heartbeat is already in flight.",
            )
            return

    if runtime_task_id is None:
        runtime_task_id = await _create_heartbeat_runtime_task(agent_id, tenant_id=tenant_id)

    try:
        from app.models.agent import Agent
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.models.llm import LLMModel
        from app.models.participant import Participant

        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"[Heartbeat] Agent {agent_id} not found in DB — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_found",
                    result_summary=f"Skipped heartbeat because agent {agent_id} was not found.",
                )
                return

            # Set execution identity — autonomous heartbeat action
            from app.core.execution_context import set_agent_bot_identity

            set_agent_bot_identity(agent_id, agent.name, source="heartbeat")

            try:
                lifecycle_report = _run_memory_lifecycle_maintenance(agent_id)
                if (
                    lifecycle_report.get("discarded_expired_count")
                    or lifecycle_report.get("conflict_hold_count")
                    or lifecycle_report.get("revalidation_hold_count")
                ):
                    logger.info("[Heartbeat] Memory lifecycle maintenance for {}: {}", agent_id, lifecycle_report)
            except Exception as _lifecycle_err:
                logger.warning("[Heartbeat] Memory lifecycle maintenance failed for {}: {}", agent_id, _lifecycle_err)

            try:
                sweep_report = await _run_t2_job_sweep(agent_id)
                if sweep_report.recovered_stale or sweep_report.retried or sweep_report.alerted:
                    logger.info(
                        "[Heartbeat] T2 job sweep for {}: recovered={} retried={} committed={} exhausted={}",
                        agent_id,
                        list(sweep_report.recovered_stale),
                        list(sweep_report.retried),
                        list(sweep_report.committed),
                        list(sweep_report.exhausted),
                    )
            except Exception as _sweep_err:
                logger.warning("[Heartbeat] T2 job sweep failed for {}: {}", agent_id, _sweep_err)

            try:
                debt_report = await _run_consolidation_debt_refresh(agent_id)
                if debt_report.stalled:
                    logger.warning(
                        "[Heartbeat] Memory consolidation stalled for {}: reasons={} pending={} oldest_age_h={}",
                        agent_id,
                        list(debt_report.stall_reasons),
                        debt_report.pending_packages,
                        debt_report.oldest_pending_age_hours,
                    )
            except Exception as _debt_err:
                logger.warning("[Heartbeat] Consolidation debt refresh failed for {}: {}", agent_id, _debt_err)

            try:
                retention_report = await _run_t2_retention(agent_id)
                if retention_report.archived:
                    logger.info(
                        "[Heartbeat] T2 retention for {}: archived={} kept_referenced={} kept_pipeline={}",
                        agent_id,
                        list(retention_report.archived),
                        retention_report.kept_referenced,
                        retention_report.kept_pipeline,
                    )
            except Exception as _retention_err:
                logger.warning("[Heartbeat] T2 retention failed for {}: {}", agent_id, _retention_err)

            try:
                artifact_gc_report = await _run_chat_artifact_snapshot_retention(agent_id, db=db)
                if artifact_gc_report.get("removed_count"):
                    logger.info(
                        "[Heartbeat] Chat artifact snapshot retention for {}: removed={} bytes={}",
                        agent_id,
                        artifact_gc_report.get("removed_count"),
                        artifact_gc_report.get("removed_bytes"),
                    )
            except Exception as _artifact_gc_err:
                logger.warning(
                    "[Heartbeat] Chat artifact snapshot retention failed for {}: {}", agent_id, _artifact_gc_err
                )

            try:
                dirtiness_report = await _run_convergence_dirtiness_refresh(agent_id)
                if dirtiness_report.dirty_files:
                    logger.info(
                        "[Heartbeat] Convergence needed for {}: {}",
                        agent_id,
                        [item["target"] for item in dirtiness_report.dirty_files],
                    )
            except Exception as _convergence_err:
                logger.warning(
                    "[Heartbeat] Convergence dirtiness refresh failed for {}: {}", agent_id, _convergence_err
                )

            if not (agent.primary_model_id or agent.fallback_model_id):
                logger.warning(f"[Heartbeat] Agent {agent.name} ({agent_id}) has no model configured — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="no_model",
                    result_summary=f"Skipped heartbeat because agent {agent.name} has no primary or fallback model configured.",
                )
                return

            model = None
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_result.scalar_one_or_none()

            fallback_model = None
            if agent.fallback_model_id:
                fallback_result = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
                )
                fallback_model = fallback_result.scalar_one_or_none()

            if model and agent.tenant_id:
                from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

                default_runtime_model = await resolve_default_model_for_tenant(
                    db,
                    agent.tenant_id,
                    exclude_model_id=model.id,
                )
                model, fallback_model = choose_runtime_model_pair(model, fallback_model, default_runtime_model)
            elif fallback_model:
                from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

                model = fallback_model
                fallback_model = None
                default_runtime_model = None
                if agent.tenant_id:
                    default_runtime_model = await resolve_default_model_for_tenant(
                        db,
                        agent.tenant_id,
                        exclude_model_id=model.id,
                    )
                model, fallback_model = choose_runtime_model_pair(model, fallback_model, default_runtime_model)

            if not model:
                logger.warning(f"[Heartbeat] Model for agent {agent.name} ({agent_id}) not found — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="model_not_found",
                    result_summary=f"Skipped heartbeat because configured model was not found for {agent.name}.",
                    metadata_json={
                        "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                        "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
                    },
                )
                return

            tick_count = _heartbeat_tick_counts.get(agent_id, 0) + 1
            _heartbeat_tick_counts[agent_id] = tick_count

            # Resolve participant for DB session
            p_result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = p_result.scalar_one_or_none()
            agent_participant_id = agent_participant.id if agent_participant else None

            session = ChatSession(
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                participant_id=agent_participant_id,
                source_channel="heartbeat",
                session_kind="agent_internal_maintenance",
                actor_type="platform",
                runtime_source="heartbeat",
                visibility_scope="agent_internal",
                listed_surface="hidden",
                title=f"Heartbeat T3: {agent.name}"[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id
            heartbeat_session_id = str(session_id)
            tick_msg = f"Heartbeat direct T3 core tick #{tick_count} at {datetime.now(timezone.utc).isoformat()}"
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    conversation_id=str(session_id),
                    role="user",
                    content=tick_msg,
                    user_id=agent.creator_id,
                    participant_id=agent_participant_id,
                )
            )
            await db.commit()
            if runtime_task_id:
                await update_runtime_task_record(
                    runtime_task_id,
                    status="running",
                    child_session_id=str(session_id),
                    result_summary="Heartbeat direct T3 core started.",
                    metadata_json={
                        "session_id": str(session_id),
                        "session_bound": False,
                        "runtime": "direct_t3_core",
                    },
                )
            logger.info("[Heartbeat] Tick #{} direct T3 core for {}", tick_count, agent.name)

            heartbeat_continuation = _run_heartbeat_core_and_persist(
                agent=agent,
                tenant_id=tenant_id,
                model=model,
                session_id=session_id,
                runtime_task_id=runtime_task_id,
                heartbeat_session_id=heartbeat_session_id,
                agent_participant_id=agent_participant_id,
                tick_count=tick_count,
            )

        await heartbeat_continuation

    except Exception as e:
        error_text = _format_heartbeat_exception(e)
        _log_heartbeat_error(agent_id, error_text)
        # CRITICAL: Update last_heartbeat_at even on failure to prevent
        # every-minute storm (if timestamp stays None, agent is always eligible)
        try:
            async with tenant_scoped_session(tenant_id) as _db:
                from app.models.agent import Agent as _Agent

                _result = await _db.execute(select(_Agent).where(_Agent.id == agent_id))
                _agent = _result.scalar_one_or_none()
                if _agent:
                    _agent.last_heartbeat_at = datetime.now(timezone.utc)
                    await _db.commit()
        except Exception as db_err:
            logger.opt(exception=True).warning("Failed to update last_heartbeat_at after error: {}", db_err)
        # Log crash to activity so evolution system can see it
        try:
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "heartbeat",
                f"Heartbeat crash: {error_text[:80]}",
                detail={"outcome_type": "crash", "error": error_text[:300]},
            )
        except Exception as log_err:
            logger.opt(exception=True).debug("Failed to log heartbeat crash to activity: {}", log_err)
        await _update_heartbeat_runtime_task(
            runtime_task_id,
            status="failed",
            result_summary=f"Heartbeat failed: {error_text[:500]}",
            session_id=heartbeat_session_id,
            metadata_json={"error": error_text[:1000]},
        )
    finally:
        if lease_held:
            await _release_heartbeat_lease_async(agent_id)


async def _heartbeat_tick():
    """One heartbeat tick: find agents due for heartbeat."""
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
    from app.services.audit_logger import write_audit_log

    now = datetime.now(timezone.utc)

    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="heartbeat tick — enumerate all running/idle agents across tenants"),
        ):
            result = await db.execute(
                select(Agent).where(
                    Agent.status.in_(["running", "idle"]),
                    agent_lifecycle_active_clause(),
                )
            )
            agents = result.scalars().all()

            # Workspace sync moved to _workspace_sync_loop (600s cadence).
            # Keeping it inline blocked the 60s heartbeat tick on Volume I/O.

            triggered = 0
            skipped_hours = 0
            skipped_interval = 0
            for agent in agents:
                if agent.tenant_id is None:
                    skipped_interval += 1
                    continue

                interval = timedelta(minutes=managed_heartbeat_interval_minutes())
                if agent.last_heartbeat_at and (now - agent.last_heartbeat_at) < interval:
                    skipped_interval += 1
                    continue

                # Fire heartbeat
                if not await _try_acquire_heartbeat_lease_async(agent.id, now=now):
                    logger.info(f"[Heartbeat] Agent {agent.name} already has an in-flight heartbeat")
                    continue
                logger.info(f"💓 Triggering heartbeat for {agent.name}")
                await write_audit_log("heartbeat_fire", {"agent_name": agent.name}, agent_id=agent.id)
                asyncio.create_task(
                    run_bounded(
                        "heartbeat", _execute_heartbeat(agent.id, tenant_id=agent.tenant_id, lease_acquired=True)
                    )
                )
                triggered += 1

            logger.info(
                f"[Heartbeat] tick: eligible={len(agents)}, triggered={triggered},"
                f" skipped_hours={skipped_hours}, skipped_interval={skipped_interval}"
            )

    except Exception as e:
        logger.opt(exception=True).error("Heartbeat tick error: {}", e)
        await write_audit_log("heartbeat_error", {"error": str(e)[:300]})


async def _sync_one_tenant(tenant_id: uuid.UUID) -> None:
    """Run sync_all_for_tenant in an isolated session with one retry."""
    from app.services.workspace_sync import sync_all_for_tenant

    for attempt in range(2):
        try:
            async with tenant_scoped_session(tenant_id) as sync_db:
                await sync_all_for_tenant(sync_db, tenant_id)
            return
        except Exception as sync_err:
            if attempt == 0:
                logger.warning(f"Workspace sync failed for tenant {tenant_id}, retrying: {sync_err}")
                await asyncio.sleep(1)
            else:
                logger.warning(f"Workspace sync failed for tenant {tenant_id} after retry: {sync_err}")


async def _sync_one_agent(agent_id: uuid.UUID) -> None:
    """Refresh per-agent workspace projections after relationship-like changes."""
    from app.services.workspace_sync import sync_agent_relationships

    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as sync_db:
            await sync_agent_relationships(sync_db, agent_id)
    except Exception as sync_err:
        logger.warning(f"Agent workspace projection sync failed for {agent_id}: {sync_err}")


async def _workspace_dirty_tick() -> None:
    """Drain dirty-flag set and re-sync only what changed. Cheap when nothing changed."""
    from app.services.workspace_sync_dirty import consume_dirty

    try:
        tenants, agents = await consume_dirty()
        if not tenants and not agents:
            return
        logger.info(f"[workspace-sync] dirty drain: tenants={len(tenants)}, agents={len(agents)}")
        for tenant_id in tenants:
            await _sync_one_tenant(tenant_id)
        for agent_id in agents:
            await _sync_one_agent(agent_id)
    except Exception as e:
        logger.opt(exception=True).error("Workspace dirty tick error: {}", e)


async def _workspace_full_sweep() -> None:
    """Safety net: sync every active tenant in case dirty events were lost."""
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause

    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="workspace full sweep — enumerate active tenants across all agents"),
        ):
            tenant_result = await db.execute(
                select(Agent.tenant_id)
                .where(
                    Agent.status.in_(["running", "idle"]),
                    Agent.tenant_id.is_not(None),
                    agent_lifecycle_active_clause(),
                )
                .distinct()
            )
            tenant_ids = {row[0] for row in tenant_result.all() if row[0]}

        logger.info(f"[workspace-sync] full sweep: {len(tenant_ids)} tenants")
        for tenant_id in tenant_ids:
            await _sync_one_tenant(tenant_id)
    except Exception as e:
        logger.opt(exception=True).error("Workspace full sweep error: {}", e)


async def _workspace_sync_loop():
    """Dirty-flag consumer: 60s tick, only syncs changed tenants/agents."""
    logger.info("📁 Workspace dirty-sync loop started (60s tick)")
    await asyncio.sleep(30)
    while True:
        await _workspace_dirty_tick()
        await asyncio.sleep(60)


async def _workspace_full_sweep_loop():
    """Safety net loop: full sync every 1h to recover from any lost dirty events."""
    logger.info("📁 Workspace full-sweep loop started (3600s interval)")
    await asyncio.sleep(120)
    while True:
        await _workspace_full_sweep()
        await asyncio.sleep(3600)


async def start_heartbeat():
    """Start background loops: heartbeat (60s) + workspace dirty-sync + full-sweep + dirty Redis listener."""
    from app.services.workspace_sync_dirty import start_redis_listener

    logger.info("💓 Agent heartbeat service started (60s tick)")
    await start_redis_listener()
    asyncio.create_task(_workspace_sync_loop())
    asyncio.create_task(_workspace_full_sweep_loop())
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)
