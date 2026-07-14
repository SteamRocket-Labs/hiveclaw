"""Model-owned Skill lifecycle review with governed, reversible commits.

Usage counts and timestamps are mechanical facts. They may inform a model
review, but never choose whether a Skill is active, stale, archived, or
restored. The platform owns provenance/pin checks, complete evidence delivery,
atomic filesystem commits, audit, and rollback metadata.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.llm_client import get_max_tokens

logger = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

_AGENT_PROVENANCE = "agent"
_UNKNOWN_PROVENANCE = "unknown"
_MUTABLE_PROVENANCE = frozenset({_AGENT_PROVENANCE, "skill_distiller"})
_USAGE_FILENAME = "skill_usage.json"
_LEGACY_USAGE_FILENAME = ".usage.json"
_ARCHIVE_DIRNAME = ".archive"
_REVIEW_LOG = "evolution/skill_curator_reviews.jsonl"
_VALID_MODEL_DECISIONS = frozenset({"keep_active", "mark_stale", "archive", "restore", "hold"})

LifecycleReviewer = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


def _skills_dir(workspace: Path) -> Path:
    return workspace / "skills"


def _usage_path(workspace: Path) -> Path:
    return workspace / "evolution" / _USAGE_FILENAME


def _legacy_usage_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / _LEGACY_USAGE_FILENAME


def _archive_dir(workspace: Path) -> Path:
    return _skills_dir(workspace) / _ARCHIVE_DIRNAME


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _empty_record(*, created_by: str = _UNKNOWN_PROVENANCE, now: datetime | None = None) -> dict[str, Any]:
    return {
        "created_by": created_by,
        "created_at": _now_iso(now),
        "last_used_at": None,
        "use_count": 0,
        "view_count": 0,
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


def _read_usage_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("[skill_curator] failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(slug): dict(record) for slug, record in data.items() if isinstance(record, dict)}


def load_skill_usage(
    workspace: Path,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, dict[str, Any]]:
    """Read usage facts, migrating the retired in-tree sidecar if necessary."""

    path = _usage_path(workspace)
    rendered = transaction.read_text("evolution/skill_usage.json") if transaction is not None else None
    if rendered is not None or path.exists():
        if rendered is not None:
            try:
                parsed = json.loads(rendered)
            except json.JSONDecodeError:
                parsed = {}
            usage = (
                {str(slug): dict(record) for slug, record in parsed.items() if isinstance(record, dict)}
                if isinstance(parsed, dict)
                else {}
            )
        else:
            usage = _read_usage_file(path)
        legacy = _legacy_usage_path(workspace)
        if legacy.exists() and transaction is None:
            legacy.unlink(missing_ok=True)
        return usage

    legacy = _legacy_usage_path(workspace)
    if not legacy.exists():
        return {}
    usage = _read_usage_file(legacy)
    if usage:
        save_skill_usage(workspace, usage, transaction=transaction)
        if transaction is None:
            legacy.unlink(missing_ok=True)
    return usage


def save_skill_usage(
    workspace: Path,
    data: dict[str, dict[str, Any]],
    *,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    """Persist mechanical usage and model-decided lifecycle state atomically."""

    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_usage_save") as own_transaction:
            save_skill_usage(workspace, data, transaction=own_transaction)
            own_transaction.commit()
        return
    transaction.stage_text(
        "evolution/skill_usage.json",
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    if transaction.read_bytes("skills/.usage.json") is not None:
        transaction.stage_delete("skills/.usage.json")


def _mutate_usage_fact(
    workspace: Path,
    slug: str,
    mutator: Callable[[dict[str, Any]], None],
    *,
    now: datetime | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_usage_fact") as own_transaction:
            _mutate_usage_fact(workspace, slug, mutator, now=now, transaction=own_transaction)
            own_transaction.commit()
        return
    usage = load_skill_usage(workspace, transaction=transaction)
    record = usage.get(slug)
    if not isinstance(record, dict):
        record = _empty_record(now=now)
    mutator(record)
    usage[slug] = record
    save_skill_usage(workspace, usage, transaction=transaction)


def mark_skill_created(
    workspace: Path,
    slug: str,
    *,
    created_by: str = _AGENT_PROVENANCE,
    now: datetime | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    """Record creation provenance without assigning future lifecycle meaning."""

    def apply(record: dict[str, Any]) -> None:
        if record.get("created_by") in (None, _UNKNOWN_PROVENANCE):
            record["created_by"] = created_by
        record.setdefault("created_at", _now_iso(now))
        record.setdefault("last_used_at", None)
        record.setdefault("use_count", 0)
        record.setdefault("view_count", 0)
        record.setdefault("state", STATE_ACTIVE)
        record.setdefault("pinned", False)
        record.setdefault("archived_at", None)

    _mutate_usage_fact(workspace, slug, apply, now=now, transaction=transaction)


def bump_skill_use(
    workspace: Path,
    slug: str,
    *,
    kind: str = "use",
    now: datetime | None = None,
) -> None:
    """Record use/view facts without changing model-decided lifecycle state."""

    def apply(record: dict[str, Any]) -> None:
        record.setdefault("created_at", _now_iso(now))
        record.setdefault("created_by", _UNKNOWN_PROVENANCE)
        record.setdefault("state", STATE_ACTIVE)
        record.setdefault("pinned", False)
        record.setdefault("archived_at", None)
        if kind == "view":
            record["view_count"] = int(record.get("view_count") or 0) + 1
        else:
            record["use_count"] = int(record.get("use_count") or 0) + 1
            record["last_used_at"] = _now_iso(now)

    _mutate_usage_fact(workspace, slug, apply, now=now)


def set_skill_pinned(workspace: Path, slug: str, pinned: bool) -> None:
    """Set an explicit authority guard against destructive lifecycle changes."""

    def apply(record: dict[str, Any]) -> None:
        record["pinned"] = bool(pinned)

    _mutate_usage_fact(workspace, slug, apply)


def archive_skill(workspace: Path, slug: str) -> bool:
    """Explicit reversible directory move retained for operator/manual callers."""

    source = _skills_dir(workspace) / slug
    if not source.is_dir():
        return False
    destination = _archive_dir(workspace) / slug
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        destination = destination.with_name(f"{slug}-{stamp}")
    try:
        source.rename(destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return True


def _archived_source(workspace: Path, slug: str) -> Path | None:
    exact = _archive_dir(workspace) / slug
    if exact.is_dir():
        return exact
    candidates = sorted(
        (path for path in _archive_dir(workspace).glob(f"{slug}-*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def restore_skill(workspace: Path, slug: str) -> bool:
    """Explicitly restore the most recent reversible archive."""

    source = _archived_source(workspace, slug)
    destination = _skills_dir(workspace) / slug
    if source is None or destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return True


def _read_skill_markdown(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_skill_lifecycle_review_evidence(
    workspace: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect every Skill body plus all telemetry; no age/count filtering."""

    generated_at = _now_iso(now)
    usage = load_skill_usage(workspace)
    active_dirs = (
        {
            path.name: path
            for path in _skills_dir(workspace).iterdir()
            if path.is_dir() and path.name != _ARCHIVE_DIRNAME
        }
        if _skills_dir(workspace).is_dir()
        else {}
    )
    archived_dirs = (
        {path.name: path for path in _archive_dir(workspace).iterdir() if path.is_dir()}
        if _archive_dir(workspace).is_dir()
        else {}
    )

    slugs = set(usage) | set(active_dirs)
    for archive_name in archived_dirs:
        matched = next((slug for slug in usage if archive_name == slug or archive_name.startswith(f"{slug}-")), None)
        slugs.add(matched or archive_name)

    skills: list[dict[str, Any]] = []
    for slug in sorted(slugs):
        active = active_dirs.get(slug)
        archived = _archived_source(workspace, slug)
        source_dir = active or archived
        record = dict(
            usage.get(slug)
            or _empty_record(
                created_by=_UNKNOWN_PROVENANCE,
                now=now,
            )
        )
        if active is not None:
            observed_location = "active"
        elif archived is not None:
            observed_location = "archived"
        else:
            observed_location = "missing"
        skill_path = source_dir / "SKILL.md" if source_dir is not None else None
        skills.append(
            {
                "slug": slug,
                "usage": record,
                "observed_location": observed_location,
                "source_path": (source_dir.relative_to(workspace).as_posix() if source_dir is not None else None),
                "skill_markdown": _read_skill_markdown(skill_path),
            }
        )
    return {
        "schema": "skill_lifecycle_review_evidence.v1",
        "generated_at": generated_at,
        "semantic_authority": "model_review",
        "policy": (
            "Counts, dates, current state, and filesystem location are observations only. "
            "The reviewer decides lifecycle meaning; the platform enforces provenance, pin, and reversible commit gates."
        ),
        "skills": skills,
    }


def _stage_directory_move(
    transaction: AgentAssetTransaction,
    *,
    source: Path,
    destination: Path,
) -> bool:
    if not source.is_dir() or destination.exists():
        return False
    files = [path for path in source.rglob("*") if path.is_file()]
    if not files:
        return False
    for path in files:
        relative_tail = path.relative_to(source)
        transaction.stage_bytes(
            destination.joinpath(relative_tail).relative_to(transaction.agent_root), path.read_bytes()
        )
        transaction.stage_delete(path.relative_to(transaction.agent_root))
    return True


def _model_archive_destination(workspace: Path, slug: str, *, now: datetime) -> Path:
    destination = _archive_dir(workspace) / slug
    if not destination.exists():
        return destination
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return destination.with_name(f"{slug}-{stamp}")


def _append_review_audit(
    workspace: Path,
    payload: dict[str, Any],
    *,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    if transaction is not None:
        transaction.append_text(_REVIEW_LOG, rendered)
        return
    with AgentAssetTransaction(workspace, operation="skill_lifecycle_review_audit") as own_transaction:
        own_transaction.append_text(_REVIEW_LOG, rendered)
        own_transaction.commit()


async def review_skill_lifecycle(
    workspace: Path,
    *,
    reviewer: LifecycleReviewer,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply only explicit model lifecycle decisions through platform gates."""

    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    evidence = build_skill_lifecycle_review_evidence(workspace, now=effective_now)
    try:
        response = reviewer(evidence)
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, dict) or not isinstance(response.get("decisions"), list):
            raise ValueError("reviewer response must contain a decisions list")
    except Exception as exc:  # model failure is an observable hold, never a fallback decision
        audit = {
            "schema": "skill_lifecycle_review.v1",
            "reviewed_at": _now_iso(effective_now),
            "semantic_authority": "model_review",
            "status": "held",
            "reason": "reviewer_failed",
            "error_class": type(exc).__name__,
        }
        try:
            _append_review_audit(workspace, audit)
        except Exception as audit_exc:  # pragma: no cover - failure is already observable in logs
            logger.warning("[skill_curator] failed to persist reviewer failure: %s", audit_exc)
        return {
            "status": "held",
            "reason": "reviewer_failed",
            "scanned": len(evidence["skills"]),
            "applied": [],
            "blocked": [],
        }

    evidence_by_slug = {str(item["slug"]): item for item in evidence["skills"]}
    usage = load_skill_usage(workspace)
    applied: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    held: list[dict[str, str]] = []
    seen: set[str] = set()

    with AgentAssetTransaction(
        workspace,
        operation="skill_lifecycle_model_review",
        evidence_refs=("model:skill_lifecycle_review",),
    ) as transaction:
        for raw in response["decisions"]:
            if not isinstance(raw, dict):
                blocked.append({"slug": "", "decision": "", "reason": "malformed_decision"})
                continue
            slug = str(raw.get("slug") or "").strip()
            decision = str(raw.get("decision") or "").strip().lower()
            reason = str(raw.get("reason") or "").strip()
            if not slug or slug not in evidence_by_slug:
                blocked.append({"slug": slug, "decision": decision, "reason": "unknown_skill"})
                continue
            if slug in seen:
                blocked.append({"slug": slug, "decision": decision, "reason": "duplicate_model_decision"})
                continue
            seen.add(slug)
            if decision not in _VALID_MODEL_DECISIONS:
                blocked.append({"slug": slug, "decision": decision, "reason": "invalid_decision"})
                continue
            if decision == "hold":
                held.append({"slug": slug, "decision": decision, "reason": reason})
                continue

            observed = evidence_by_slug[slug]
            record = dict(usage.get(slug) or observed["usage"])
            provenance = str(record.get("created_by") or _UNKNOWN_PROVENANCE)
            if provenance not in _MUTABLE_PROVENANCE:
                blocked.append({"slug": slug, "decision": decision, "reason": "provenance_not_mutable"})
                continue
            if record.get("pinned") and decision in {"mark_stale", "archive"}:
                blocked.append({"slug": slug, "decision": decision, "reason": "pinned_authority_guard"})
                continue

            changed = False
            if decision == "archive":
                source = _skills_dir(workspace) / slug
                destination = _model_archive_destination(workspace, slug, now=effective_now)
                changed = _stage_directory_move(transaction, source=source, destination=destination)
                if changed:
                    record["state"] = STATE_ARCHIVED
                    record["archived_at"] = _now_iso(effective_now)
            elif decision == "restore":
                source = _archived_source(workspace, slug)
                destination = _skills_dir(workspace) / slug
                changed = bool(source) and _stage_directory_move(
                    transaction,
                    source=source,
                    destination=destination,
                )
                if changed:
                    record["state"] = STATE_ACTIVE
                    record["archived_at"] = None
            elif decision == "mark_stale":
                changed = (_skills_dir(workspace) / slug).is_dir()
                if changed:
                    record["state"] = STATE_STALE
            elif decision == "keep_active":
                changed = (_skills_dir(workspace) / slug).is_dir()
                if changed:
                    record["state"] = STATE_ACTIVE
                    record["archived_at"] = None

            if not changed:
                blocked.append({"slug": slug, "decision": decision, "reason": "filesystem_state_conflict"})
                continue
            usage[slug] = record
            applied.append({"slug": slug, "decision": decision})
            from app.services.skill_lifecycle import record_skill_lifecycle_event

            record_skill_lifecycle_event(
                workspace,
                skill_name=slug,
                status=decision,
                note=f"Model lifecycle review: {reason or 'no additional reason supplied'}",
                transaction=transaction,
            )

        save_skill_usage(workspace, usage, transaction=transaction)
        _append_review_audit(
            workspace,
            {
                "schema": "skill_lifecycle_review.v1",
                "reviewed_at": _now_iso(effective_now),
                "semantic_authority": "model_review",
                "status": "reviewed",
                "model_response": response,
                "applied": applied,
                "blocked": blocked,
                "held": held,
            },
            transaction=transaction,
        )
        transaction.commit()

    return {
        "status": "reviewed",
        "scanned": len(evidence["skills"]),
        "applied": applied,
        "blocked": blocked,
        "held": held,
    }


def run_skill_curator_pass(
    workspace: Path,
    *,
    now: datetime | None = None,
    **_compatibility: Any,
) -> dict[str, Any]:
    """Compatibility read pass: expose evidence but never mutate semantics."""

    evidence = build_skill_lifecycle_review_evidence(workspace, now=now)
    return {
        "status": "model_review_required",
        "scanned": len(evidence["skills"]),
        "evidence": evidence,
        "to_stale": [],
        "to_archived": [],
        "revived": [],
    }


async def review_skill_lifecycle_with_model(
    workspace: Path,
    *,
    model: Any,
    agent_id: Any = None,
    tenant_id: Any = None,
) -> dict[str, Any]:
    """Give every lifecycle byte to the configured model, then govern its decision."""

    from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context
    from app.services.semantic_input_coverage import prepare_covered_semantic_input

    client = create_llm_client_from_config(
        with_llm_usage_context(
            {
                "provider": getattr(model, "provider"),
                "model": getattr(model, "model"),
                "api_key": getattr(model, "api_key"),
                "base_url": getattr(model, "base_url", None),
            },
            source="skill_lifecycle_review",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    )
    try:
        try:
            max_input_tokens = int(getattr(model, "max_input_tokens", None) or 128_000)
        except (TypeError, ValueError):
            max_input_tokens = 128_000
        semantic_budget = max(max_input_tokens - 16_000, 8_000) * 3
        output_tokens = get_max_tokens(
            str(getattr(model, "provider", "") or ""),
            str(getattr(model, "model", "") or ""),
            getattr(model, "max_output_tokens", None),
        )

        async def reviewer(evidence: dict[str, Any]) -> dict[str, Any]:
            full_input = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)

            async def review_chunk(_phase: str, prompt: str) -> str:
                response = await client.complete(
                    messages=[
                        LLMMessage(
                            role="system",
                            content=(
                                "Preserve every Skill body, usage fact, contradiction, provenance fact, pin, "
                                "filesystem state, and decisive tail for the final lifecycle reviewer."
                            ),
                        ),
                        LLMMessage(role="user", content=prompt),
                    ],
                    temperature=0.1,
                    max_tokens=output_tokens,
                )
                return str(response.content or "").strip()

            covered = await prepare_covered_semantic_input(
                phase="skill_lifecycle_review",
                sections=[("skill_lifecycle:complete_catalog", full_input)],
                max_chars=semantic_budget,
                coverage_path=workspace / "memory" / "control" / "skill_lifecycle_review.coverage.json",
                review_chunk=review_chunk,
            )
            response = await client.complete(
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "You are the semantic authority for Skill lifecycle. Review every supplied Skill and "
                            "all evidence. Counts, dates, rankings, and current state are observations only. "
                            "For each Skill that needs a decision, choose keep_active, mark_stale, archive, restore, "
                            "or hold. Platform provenance, pin, transaction, and rollback gates run afterward. "
                            "Return raw JSON only: "
                            '{"decisions":[{"slug":"...","decision":"keep_active|mark_stale|archive|restore|hold",'
                            '"reason":"complete evidence-grounded reason"}]}'
                        ),
                    ),
                    LLMMessage(role="user", content=covered),
                ],
                temperature=0.1,
                max_tokens=output_tokens,
            )
            parsed = json.loads(str(response.content or "").strip())
            if not isinstance(parsed, dict):
                raise ValueError("Skill lifecycle review must be a JSON object")
            return parsed

        return await review_skill_lifecycle(workspace, reviewer=reviewer)
    finally:
        await client.close()
