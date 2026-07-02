"""Owner approval loop for held soul candidates (K1: the doorbell).

The Platform Soul Gate holds candidates flagged ``requires_owner_approval``
in ``memory/.staging/soul_candidates/<id>/``. This module gives the owner the
missing decision surface:

- list pending approvals (pitch/patch included, so the owner judges without
  opening raw files);
- approve → owner-authorized commit through the same physical hard checks
  the gate enforces (approval unlocks ONLY the owner hold — base drift,
  hive.soul.v2 schema, source_refs, transient identifiers, frozen-charter
  preservation all still refuse), then rollback snapshot + atomic soul.md
  write + manifest advance + audit with the approver identity;
- reject → package marked rejected with the reason, soul.md untouched.

The semantic Soul Memory Gate review already ran at staging time and the
package is immutable on disk, so approval re-verifies physics, not opinions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SOUL_CANDIDATES_RELATIVE = Path("memory") / ".staging" / "soul_candidates"


def list_pending_soul_approvals(*, workspace: Path) -> list[dict[str, Any]]:
    """Held candidates awaiting the owner, newest first."""
    root = Path(workspace) / _SOUL_CANDIDATES_RELATIVE
    if not root.exists():
        return []
    pending: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        if not manifest:
            continue
        status = str(manifest.get("status") or "").lower()
        if status != "held" or not bool(manifest.get("requires_owner_approval")):
            continue
        package_dir = manifest_path.parent
        pending.append(
            {
                "candidate_id": str(manifest.get("candidate_id") or package_dir.name),
                "status": status,
                "reason": str(manifest.get("reason") or ""),
                "requires_owner_approval": True,
                "created_at": str(manifest.get("created_at") or ""),
                "source_refs": [str(ref) for ref in (manifest.get("source_refs") or [])],
                "pitch": _read_text(package_dir / "soul_pitch.md"),
                "patch": _read_text(package_dir / "soul_patch.md"),
            }
        )
    pending.sort(key=lambda item: item["created_at"], reverse=True)
    return pending


async def approve_soul_candidate(
    *,
    workspace: Path,
    agent_id: uuid.UUID,
    candidate_id: str,
    approver_id: uuid.UUID,
) -> dict[str, Any]:
    workspace = Path(workspace)
    package_dir = workspace / _SOUL_CANDIDATES_RELATIVE / candidate_id
    manifest_path = package_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not manifest:
        return {"status": "refused", "reason": "candidate package not found"}

    ok, reason = _verify_physics(workspace=workspace, package_dir=package_dir, manifest=manifest)
    if not ok:
        _audit(
            workspace,
            agent_id,
            candidate_id=candidate_id,
            package_dir=package_dir,
            manifest=manifest,
            outcome="owner_approval_refused",
            reason=reason,
            approver_id=approver_id,
            rollback_ref=None,
        )
        return {"status": "refused", "reason": reason}

    soul_path = workspace / "soul.md"
    current_soul = _read_text(soul_path)
    soul_next = _read_text(package_dir / "soul.md.next")

    rollback_dir = workspace / "memory" / ".rollback" / "soul"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback_ref = rollback_dir / f"{candidate_id}.soul.md.before"
    rollback_ref.write_text(current_soul, encoding="utf-8")
    rollback_ref_rel = str(rollback_ref.relative_to(workspace))

    _write_atomic_text(soul_path, soul_next.rstrip() + "\n")

    manifest["status"] = "committed"
    manifest["approved_by"] = str(approver_id)
    manifest["approved_at"] = datetime.now(timezone.utc).isoformat()
    manifest["rollback_ref"] = rollback_ref_rel
    _write_json(manifest_path, manifest)

    _audit(
        workspace,
        agent_id,
        candidate_id=candidate_id,
        package_dir=package_dir,
        manifest=manifest,
        outcome="owner_approved",
        reason="owner approved held soul candidate",
        approver_id=approver_id,
        rollback_ref=rollback_ref_rel,
    )
    logger.info("Soul candidate %s approved by %s for agent %s", candidate_id, approver_id, agent_id)
    return {"status": "committed", "candidate_id": candidate_id, "rollback_ref": rollback_ref_rel}


async def reject_soul_candidate(
    *,
    workspace: Path,
    agent_id: uuid.UUID,
    candidate_id: str,
    approver_id: uuid.UUID,
    reason: str = "",
) -> dict[str, Any]:
    workspace = Path(workspace)
    package_dir = workspace / _SOUL_CANDIDATES_RELATIVE / candidate_id
    manifest_path = package_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not manifest:
        return {"status": "refused", "reason": "candidate package not found"}

    manifest["status"] = "rejected"
    manifest["rejected_by"] = str(approver_id)
    manifest["rejected_at"] = datetime.now(timezone.utc).isoformat()
    manifest["rejected_reason"] = reason or "rejected by owner"
    _write_json(manifest_path, manifest)

    _audit(
        workspace,
        agent_id,
        candidate_id=candidate_id,
        package_dir=package_dir,
        manifest=manifest,
        outcome="owner_rejected",
        reason=reason or "rejected by owner",
        approver_id=approver_id,
        rollback_ref=None,
    )
    return {"status": "rejected", "candidate_id": candidate_id}


def _verify_physics(*, workspace: Path, package_dir: Path, manifest: dict) -> tuple[bool, str]:
    """Hard checks the owner's click must NOT bypass."""
    from app.services.auto_dream import (
        _SOUL_TRANSIENT_PATTERNS,
        _extract_frozen_charter,
        _promotion_contradicts_frozen,
    )

    if str(manifest.get("status") or "").lower() != "held":
        return False, f"candidate is not held (status={manifest.get('status')})"
    if not bool(manifest.get("requires_owner_approval")):
        return False, "candidate does not require owner approval"

    soul_next = _read_text(package_dir / "soul.md.next")
    pitch = _read_text(package_dir / "soul_pitch.md")
    patch = _read_text(package_dir / "soul_patch.md")
    if not soul_next.strip() or not pitch.strip() or not patch.strip():
        return False, "candidate package is incomplete"

    current_soul = _read_text(workspace / "soul.md")
    base_sha = str(manifest.get("base_sha256") or "")
    if base_sha and base_sha != hashlib.sha256(current_soul.encode("utf-8")).hexdigest():
        return False, "soul.md base drift since nomination — renominate against the current soul"

    if "schema: hive.soul.v2" not in soul_next:
        return False, "soul.md.next must use hive.soul.v2 schema"
    if "<source_ref" not in soul_next:
        return False, "soul.md.next must preserve source_refs"

    candidate_text = "\n".join([pitch, patch, soul_next])
    if any(pattern.search(candidate_text) for pattern in _SOUL_TRANSIENT_PATTERNS):
        return False, "candidate contains transient runtime identifiers"

    frozen_charter = _extract_frozen_charter(current_soul)
    if frozen_charter:
        contradicts, contra_reason = _promotion_contradicts_frozen(frozen_charter, candidate_text, None)
        if contradicts:
            return False, f"contradicts frozen Mission/charter: {contra_reason}"
        if 'frozen="true"' not in soul_next:
            return False, "soul.md.next must preserve the frozen identity/charter block"

    return True, "physical checks passed"


def _audit(
    workspace: Path,
    agent_id: uuid.UUID,
    *,
    candidate_id: str,
    package_dir: Path,
    manifest: dict,
    outcome: str,
    reason: str,
    approver_id: uuid.UUID,
    rollback_ref: str | None,
) -> None:
    from app.memory.distillation_audit import write_distillation_audit

    write_distillation_audit(
        workspace.parent,
        agent_id,
        stage="soul_candidate_owner_decision",
        outcome=outcome,
        reason=reason,
        detail={
            "candidate_id": candidate_id,
            "candidate_package_path": str(package_dir.relative_to(workspace)),
            "target_path": "soul.md",
            "rollback_ref": rollback_ref,
            "source_refs": [str(ref) for ref in (manifest.get("source_refs") or [])],
            "approver_id": str(approver_id),
            "schema": "soul_candidate_package.v1",
            "physical_committer": "Platform Soul Gate (owner-authorized)",
        },
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)

