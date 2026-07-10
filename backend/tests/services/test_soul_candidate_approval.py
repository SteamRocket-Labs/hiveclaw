"""K1 red tests: the owner-approval loop for held soul candidates.

The Platform Soul Gate holds candidates marked ``requires_owner_approval`` —
but until now nothing let the owner actually approve or reject them (a door
with no doorbell). Contract:

- ``list_pending_soul_approvals`` — held candidates awaiting the owner, with
  pitch/patch so the owner can judge without reading raw files.
- ``approve_soul_candidate`` — owner-authorized commit. The semantic review
  already passed at staging time and the package is immutable, so approval
  re-runs the PHYSICAL hard checks only: package completeness, held+owner
  flag, base-sha drift (soul changed since nomination → refuse, renominate),
  hive.soul.v2 schema + source_refs, transient identifiers, frozen-charter
  contradiction + frozen-block preservation. Then: rollback snapshot, atomic
  soul.md write, manifest advanced, audit with the approver identity.
- ``reject_soul_candidate`` — marks the package rejected with reason and
  approver; soul.md untouched.

Gate discipline: approval unlocks the owner hold — it does not relax the
other hard checks.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest


AGENT_ID = uuid.uuid4()
OWNER_ID = uuid.uuid4()


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / str(AGENT_ID)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _seed_soul(ws: Path, *, frozen_line: str = "我们只服务企业客户。") -> str:
    # "## Identity & Mission" is one of the _FROZEN_SOUL_SECTIONS headers the
    # gate slices as the frozen charter; the frozen="true" block must survive
    # any rewrite.
    soul = (
        "schema: hive.soul.v2\n"
        f'<soul_identity frozen="true">\n## Identity & Mission\n{frozen_line}\n</soul_identity>\n'
        "<soul_style>\n## Style\n简洁直接。\n</soul_style>\n"
    )
    (ws / "soul.md").write_text(soul, encoding="utf-8")
    return soul


def _stage_held_candidate(
    ws: Path,
    *,
    candidate_id: str = "soulcand-1",
    soul_next: str | None = None,
    base_sha: str | None = None,
) -> Path:
    package_dir = ws / "memory" / ".staging" / "soul_candidates" / candidate_id
    package_dir.mkdir(parents=True, exist_ok=True)
    current = (ws / "soul.md").read_text(encoding="utf-8")
    next_text = soul_next or (
        current
        + "<soul_quality>\n## Quality\n交付前自查证据链。\n<source_ref>t3:worker.md#wr-1</source_ref>\n</soul_quality>\n"
    )
    (package_dir / "soul.md.next").write_text(next_text, encoding="utf-8")
    (package_dir / "soul_pitch.md").write_text("长期稳定的质量标准,提名入魂。", encoding="utf-8")
    (package_dir / "soul_patch.md").write_text(
        "+ soul_quality block\n<source_ref>t3:worker.md#wr-1</source_ref>\n", encoding="utf-8"
    )
    manifest = {
        "schema": "soul_candidate_package.v1",
        "candidate_id": candidate_id,
        "target_path": "soul.md",
        "status": "held",
        "reason": "candidate requires owner/company approval",
        "requires_owner_approval": True,
        "created_at": "2026-07-02T10:00:00+00:00",
        "source_refs": ["t3:worker.md#wr-1"],
        "base_sha256": base_sha or hashlib.sha256(current.encode("utf-8")).hexdigest(),
        "next_sha256": hashlib.sha256(next_text.encode("utf-8")).hexdigest(),
        "next_path": f"memory/.staging/soul_candidates/{candidate_id}/soul.md.next",
        "memory_gate_review": {"candidate_id": candidate_id, "recommendation": "promote"},
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return package_dir


def test_list_pending_soul_approvals_surfaces_held_candidates(tmp_path: Path) -> None:
    from app.services.soul_approval import list_pending_soul_approvals

    ws = _workspace(tmp_path)
    _seed_soul(ws)
    _stage_held_candidate(ws)

    pending = list_pending_soul_approvals(workspace=ws)

    assert len(pending) == 1
    assert pending[0]["candidate_id"] == "soulcand-1"
    assert pending[0]["requires_owner_approval"] is True
    assert pending[0]["pitch"]
    assert pending[0]["patch"]


@pytest.mark.asyncio
async def test_approve_commits_soul_with_rollback_and_owner_audit(tmp_path: Path) -> None:
    from app.services.soul_approval import approve_soul_candidate

    ws = _workspace(tmp_path)
    original = _seed_soul(ws)
    _stage_held_candidate(ws)

    result = await approve_soul_candidate(
        workspace=ws,
        agent_id=AGENT_ID,
        candidate_id="soulcand-1",
        approver_id=OWNER_ID,
    )

    assert result["status"] == "committed"
    soul_now = (ws / "soul.md").read_text(encoding="utf-8")
    assert "<soul_quality>" in soul_now
    # rollback snapshot preserves the pre-approval soul
    rollback = ws / "memory" / ".rollback" / "soul" / "soulcand-1.soul.md.before"
    assert rollback.read_text(encoding="utf-8") == original
    # package advanced to committed with the approver identity
    manifest = json.loads(
        (ws / "memory" / ".staging" / "soul_candidates" / "soulcand-1" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "committed"
    assert manifest["approved_by"] == str(OWNER_ID)
    # audit records the owner-approved outcome
    audit_text = (ws / "memory" / "distillation_audit.jsonl").read_text(encoding="utf-8")
    assert "owner_approved" in audit_text
    assert str(OWNER_ID) in audit_text


@pytest.mark.asyncio
async def test_approve_still_refuses_frozen_charter_violation(tmp_path: Path) -> None:
    """Owner approval unlocks the hold — it does not relax the other gates:
    a rewrite that drops the frozen identity block stays refused."""
    from app.services.soul_approval import approve_soul_candidate

    ws = _workspace(tmp_path)
    _seed_soul(ws, frozen_line="我们只服务企业客户。")
    bad_next = (
        "schema: hive.soul.v2\n"
        "<soul_identity>\n## Identity & Mission\n服务所有个人用户。\n"
        "<source_ref>t3:worker.md#wr-1</source_ref>\n</soul_identity>\n"
    )
    _stage_held_candidate(ws, candidate_id="soulcand-bad", soul_next=bad_next)

    result = await approve_soul_candidate(
        workspace=ws,
        agent_id=AGENT_ID,
        candidate_id="soulcand-bad",
        approver_id=OWNER_ID,
    )

    assert result["status"] == "refused"
    assert "frozen" in result["reason"].lower()
    assert "服务所有个人用户" not in (ws / "soul.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_approve_refuses_on_base_drift(tmp_path: Path) -> None:
    """soul.md changed since the nomination → the patch's base is gone;
    refuse and ask for a fresh nomination instead of committing blind."""
    from app.services.soul_approval import approve_soul_candidate

    ws = _workspace(tmp_path)
    _seed_soul(ws)
    _stage_held_candidate(ws, base_sha="0" * 64)

    result = await approve_soul_candidate(
        workspace=ws,
        agent_id=AGENT_ID,
        candidate_id="soulcand-1",
        approver_id=OWNER_ID,
    )

    assert result["status"] == "refused"
    assert "base" in result["reason"].lower() or "drift" in result["reason"].lower()


@pytest.mark.asyncio
async def test_reject_marks_package_without_touching_soul(tmp_path: Path) -> None:
    from app.services.soul_approval import reject_soul_candidate

    ws = _workspace(tmp_path)
    original = _seed_soul(ws)
    _stage_held_candidate(ws)

    result = await reject_soul_candidate(
        workspace=ws,
        agent_id=AGENT_ID,
        candidate_id="soulcand-1",
        approver_id=OWNER_ID,
        reason="不符合团队定位",
    )

    assert result["status"] == "rejected"
    assert (ws / "soul.md").read_text(encoding="utf-8") == original
    manifest = json.loads(
        (ws / "memory" / ".staging" / "soul_candidates" / "soulcand-1" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rejected"
    assert manifest["rejected_reason"] == "不符合团队定位"
    audit_text = (ws / "memory" / "distillation_audit.jsonl").read_text(encoding="utf-8")
    assert "owner_rejected" in audit_text


def test_evolution_api_registers_approval_routes() -> None:
    """The doorbell exists: approve/reject routes live on the agents router."""
    from app.api.agents import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/agents/{agent_id}/evolution/soul-candidates/{candidate_id}/approve" in paths
    assert "/agents/{agent_id}/evolution/soul-candidates/{candidate_id}/reject" in paths


def test_dream_stage_records_owner_approval_flag(tmp_path: Path) -> None:
    """The staging manifest carries requires_owner_approval explicitly so the
    approval surface never has to parse a reason string."""
    from app.services.auto_dream import _stage_soul_candidate_package

    ws = _workspace(tmp_path)
    _seed_soul(ws)
    candidate = {
        "soul_pitch_md": "pitch",
        "soul_patch_md": "patch",
        "soul_md_next": "next",
        "requires_owner_approval": True,
        "source_refs": ["t3:worker.md#wr-1"],
    }

    _candidate_id, package_dir = _stage_soul_candidate_package(
        workspace=ws,
        candidate=candidate,
        status="held",
        reason="candidate requires owner/company approval",
        current_soul=(ws / "soul.md").read_text(encoding="utf-8"),
    )

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requires_owner_approval"] is True
