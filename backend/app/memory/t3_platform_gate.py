"""T3 Platform Gate: hard validation and atomic commit for accepted T3 files.

The LLM owns semantic writing in `revised_patch.md`; this module owns only
schema/path/source/base-revision checks and exact block application.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.md_store import ensure_t3_layout, rebuild_index

ACCEPTED_T3_TARGETS: tuple[str, ...] = (
    "memory/t3/episodes.md",
    "memory/t3/user.md",
    "memory/t3/worker.md",
    "memory/t3/capabilities.md",
)

# Two-plane fixed convergent files (spec §1.1): written as ### markdown
# entries anchored by `<!-- id: ... -->` through upsert_entry/retire_entry.
PROFILE_PLANE_TARGETS: tuple[str, ...] = (
    "memory/self/self.md",
    "memory/profiles/owner.md",
    "memory/profiles/collaborators.md",
    "memory/profiles/domain.md",
)
# Dynamic knowledge-plane pages (spec §3.4/§3.5): whole-page upsert_page.
# Slug charset excludes '/' and '.', so traversal cannot pass validation.
_DYNAMIC_TARGET_RE = re.compile(r"^memory/(?:knowledge|milestones)/[a-z0-9][a-z0-9-]{0,78}\.md$")
_RELATION_EDGE_RE = re.compile(r"^-\s+[a-z_]+\s+\[\[[^\]]+\]\]\s*$", re.MULTILINE)
_ENTRY_ID_COMMENT_RE = re.compile(r"<!--\s*id:\s*(?P<entry_id>[A-Za-z0-9:_.-]+)\s*-->")

TARGET_VIEW_VALUES = frozenset(
    {"episodes", "user", "worker", "capabilities", "self", "profiles", "knowledge", "milestones"}
)
CONSOLIDATION_MODE_VALUES = frozenset({"create", "merge", "supersede", "reinforce", "contradict", "retract", "noop"})
SOURCE_COVERAGE_VALUES = frozenset({"single_session", "multi_session", "explicit_user", "tool_verified", "weak"})
STABILITY_VALUES = frozenset({"ephemeral", "short_lived", "evolving", "stable"})
BEHAVIOR_IMPACT_VALUES = frozenset(
    {
        "recall_only",
        "response_style",
        "tool_policy",
        "memory_policy",
        "method_reuse",
        "identity_candidate",
        "skill_candidate",
        "recall_and_method_reuse",
    }
)
PROMPT_PRIORITY_VALUES = frozenset({"p0_if_relevant", "p1_dynamic", "p2_on_demand", "archive_only"})
RUBRIC_SCORE_NAMES = (
    "evidence_strength",
    "scope_clarity",
    "stability",
    "future_utility",
    "conflict_safety",
)
ACCEPTED_REVIEW_DECISIONS = frozenset({"accept"})
ACCEPTED_RUBRIC_DECISIONS = frozenset({"accept_new", "merge_required", "supersede_existing", "reinforced"})
_T0_SESSION_SEGMENT_RE = re.compile(r"t0://session/([^/#\s<>'\"]+)/segment/([^/#\s<>'\"]+)[^\s<>'\"]*")
_T2_SESSION_SEGMENT_RE = re.compile(r"t2://session/([^/#\s<>'\"]+)/segment/([^/#\s<>'\"]+)[^\s<>'\"]*")


@dataclass(frozen=True, slots=True)
class T3PlatformGateResult:
    status: str  # committed | held | rebase_required
    job_id: str
    committed_paths: tuple[str, ...] = ()
    committed_blocks: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    manifest_path: Path | None = None
    conflict_bundle_path: Path | None = None


def is_accepted_t3_target(path: str) -> bool:
    normalized = _normalize_target(path)
    if normalized in ACCEPTED_T3_TARGETS or normalized in PROFILE_PLANE_TARGETS:
        return True
    return bool(_DYNAMIC_TARGET_RE.match(normalized))


def is_dynamic_page_target(path: str) -> bool:
    return bool(_DYNAMIC_TARGET_RE.match(_normalize_target(path)))


def is_profile_plane_target(path: str) -> bool:
    return _normalize_target(path) in PROFILE_PLANE_TARGETS


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else hashlib.sha256(b"").hexdigest()


def apply_t3_consolidation_patch(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    job_id: str,
    revised_patch_md: str,
    review_md: str,
) -> T3PlatformGateResult:
    """Apply a reviewed LLM-authored T3 patch exactly, or hold/rebase it."""

    root = Path(data_root)
    mem_dir = ensure_t3_layout(root, uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id)
    job_dir = mem_dir / ".staging" / "t3_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "revised_patch.md").write_text(revised_patch_md, encoding="utf-8")
    (job_dir / "review.md").write_text(review_md, encoding="utf-8")

    issues: list[str] = []
    patch = _extract_xml(revised_patch_md, "t3_consolidation_patch", issues)
    review = _extract_xml(review_md, "memory_gate_review", issues)
    if patch is not None:
        _validate_patch_shape(patch, issues)
    if review is not None:
        _validate_review(review, issues)
    if issues:
        return _write_manifest_result(
            job_dir,
            status="held",
            job_id=job_id,
            issues=issues,
        )

    assert patch is not None
    base_revisions = {
        _normalize_target(node.attrib.get("path") or ""): str(node.attrib.get("sha256") or "").strip()
        for node in patch.findall("./base_revisions/base_revision")
        if str(node.attrib.get("path") or "").strip()
    }
    targets = sorted(
        {
            _normalize_target(node.attrib.get("path") or "")
            for node in patch.findall("./target_files/target_file")
            if str(node.attrib.get("path") or "").strip()
        }
    )
    for target in targets:
        if target not in base_revisions:
            issues.append(f"missing base_revision for {target}")
    if issues:
        return _write_manifest_result(job_dir, status="held", job_id=job_id, issues=issues)

    for target, expected_sha in base_revisions.items():
        current_path = _target_path(mem_dir, target)
        current_sha = file_sha256(current_path)
        if expected_sha and expected_sha != current_sha:
            conflict_path = _write_conflict_bundle(
                job_dir=job_dir,
                target=target,
                expected_sha=expected_sha,
                current_sha=current_sha,
                current_content=current_path.read_text(encoding="utf-8", errors="replace")
                if current_path.exists()
                else "",
            )
            return _write_manifest_result(
                job_dir,
                status="rebase_required",
                job_id=job_id,
                issues=[f"base revision changed for {target}"],
                conflict_bundle_path=conflict_path,
            )

    current_contents = {target: _read_target(mem_dir, target) for target in targets}
    updated_contents = dict(current_contents)
    committed_blocks: list[str] = []
    for change in list(patch.findall("./proposed_changes/append_block")) + list(
        patch.findall("./proposed_changes/replace_block")
    ):
        op = change.tag
        target = _normalize_target(change.attrib.get("target") or "")
        block_id = str(change.attrib.get("block_id") or "").strip()
        block_content = (change.findtext("block_content") or "").strip()
        if not target or not block_id or not block_content:
            issues.append(f"{op} missing target/block_id/block_content")
            continue
        if target not in ACCEPTED_T3_TARGETS:
            issues.append(f"non-canonical target: {target}")
            continue
        block_issue = _validate_block_content(block_content, block_id=block_id, target=target)
        if block_issue:
            issues.append(block_issue)
            continue
        if op == "append_block":
            if _block_exists(updated_contents.get(target, ""), block_id):
                issues.append(f"block already exists in {target}: {block_id}")
                continue
            updated_contents[target] = _append_block(updated_contents.get(target, ""), block_content)
            committed_blocks.append(block_id)
        elif op == "replace_block":
            expected_old_hash = str(change.attrib.get("expected_old_hash") or "").replace("sha256:", "").strip()
            existing = _find_block(updated_contents.get(target, ""), block_id)
            if existing is None:
                issues.append(f"replace_block target block missing in {target}: {block_id}")
                continue
            current_block_hash = hashlib.sha256(existing.encode("utf-8")).hexdigest()
            if expected_old_hash and expected_old_hash != current_block_hash:
                conflict_path = _write_conflict_bundle(
                    job_dir=job_dir,
                    target=target,
                    expected_sha=expected_old_hash,
                    current_sha=current_block_hash,
                    current_content=existing,
                )
                return _write_manifest_result(
                    job_dir,
                    status="rebase_required",
                    job_id=job_id,
                    issues=[f"block revision changed for {target}#{block_id}"],
                    conflict_bundle_path=conflict_path,
                )
            updated_contents[target] = updated_contents[target].replace(existing, block_content)
            committed_blocks.append(block_id)

    for change in patch.findall("./proposed_changes/retire_block"):
        target = _normalize_target(change.attrib.get("target") or "")
        block_id = str(change.attrib.get("block_id") or "").strip()
        if not is_accepted_t3_target(target) or not block_id:
            issues.append("retire_block missing canonical target/block_id")
            continue
        existing = _find_block(updated_contents.get(target, ""), block_id)
        if existing is None:
            issues.append(f"retire_block target block missing in {target}: {block_id}")
            continue
        expected_old_hash = str(change.attrib.get("expected_old_hash") or "").replace("sha256:", "").strip()
        current_block_hash = hashlib.sha256(existing.strip().encode("utf-8")).hexdigest()
        if expected_old_hash and expected_old_hash != current_block_hash:
            conflict_path = _write_conflict_bundle(
                job_dir=job_dir,
                target=target,
                expected_sha=expected_old_hash,
                current_sha=current_block_hash,
                current_content=existing,
            )
            return _write_manifest_result(
                job_dir,
                status="rebase_required",
                job_id=job_id,
                issues=[f"block revision changed for {target}#{block_id}"],
                conflict_bundle_path=conflict_path,
            )
        updated_contents[target] = updated_contents[target].replace(
            existing,
            _retire_block(
                existing,
                job_id=job_id,
                reason=str(change.attrib.get("reason") or "").strip(),
            ),
        )
        committed_blocks.append(block_id)

    # -- two-plane operations (spec §3.2/§3.4/§3.5) --

    for change in patch.findall("./proposed_changes/upsert_page"):
        target = _normalize_target(change.attrib.get("target") or "")
        page_content = (change.findtext("page_content") or "").strip()
        if not is_dynamic_page_target(target):
            issues.append(f"upsert_page target must be a knowledge/milestones page: {target or '<missing>'}")
            continue
        if not page_content:
            issues.append(f"upsert_page missing page_content: {target}")
            continue
        is_new_page = not _target_path(mem_dir, target).exists() and not (current_contents.get(target) or "").strip()
        if target.startswith("memory/knowledge/") and is_new_page and not _RELATION_EDGE_RE.search(page_content):
            issues.append(f"new knowledge page must carry >=1 `## Relations` edge (forward references count): {target}")
            continue
        updated_contents[target] = page_content
        committed_blocks.append(target)

    for change in patch.findall("./proposed_changes/upsert_entry"):
        target = _normalize_target(change.attrib.get("target") or "")
        entry_id = str(change.attrib.get("entry_id") or "").strip()
        section = str(change.attrib.get("section") or "").strip()
        entry_content = (change.findtext("entry_content") or "").strip()
        if not is_profile_plane_target(target):
            issues.append(f"upsert_entry target must be a self/profiles file: {target or '<missing>'}")
            continue
        if not entry_id or not entry_content:
            issues.append(f"upsert_entry missing entry_id/entry_content: {target}")
            continue
        id_match = _ENTRY_ID_COMMENT_RE.search(entry_content)
        if not entry_content.startswith("###") or id_match is None or id_match.group("entry_id") != entry_id:
            issues.append(f"upsert_entry content must start with ### and carry `<!-- id: {entry_id} -->`: {target}")
            continue
        content = updated_contents.get(target, "")
        existing = _find_md_entry(content, entry_id)
        if existing is not None:
            updated_contents[target] = content.replace(existing, entry_content)
        else:
            updated_contents[target] = _insert_md_entry(content, section=section, entry_content=entry_content)
        committed_blocks.append(entry_id)

    for change in patch.findall("./proposed_changes/retire_entry"):
        target = _normalize_target(change.attrib.get("target") or "")
        entry_id = str(change.attrib.get("entry_id") or "").strip()
        reason = str(change.attrib.get("reason") or "").strip()
        if not is_profile_plane_target(target) or not entry_id:
            issues.append("retire_entry missing profile-plane target/entry_id")
            continue
        content = updated_contents.get(target, "")
        existing = _find_md_entry(content, entry_id)
        if existing is None:
            issues.append(f"retire_entry target entry missing in {target}: {entry_id}")
            continue
        updated_contents[target] = content.replace(
            existing, _retire_md_entry(existing, entry_id=entry_id, job_id=job_id, reason=reason)
        )
        committed_blocks.append(entry_id)

    for change in patch.findall("./proposed_changes/reinforce_block"):
        target = _normalize_target(change.attrib.get("target") or "")
        block_id = str(change.attrib.get("block_id") or "").strip()
        if not is_accepted_t3_target(target) or not block_id:
            issues.append("reinforce_block missing canonical target/block_id")
            continue
        if _find_block(updated_contents.get(target, ""), block_id) is None:
            issues.append(f"reinforce_block target block missing in {target}: {block_id}")
            continue
        committed_blocks.append(block_id)

    if issues:
        return _write_manifest_result(job_dir, status="held", job_id=job_id, issues=issues)

    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    _atomic_write_targets(mem_dir, updated_contents)
    source_updates = _apply_source_lifecycle_updates(
        root=root,
        agent_id=resolved_agent_id,
        job_id=job_id,
        patch=patch,
        review=review,
        committed_blocks=tuple(committed_blocks),
    )
    rebuild_index(root, resolved_agent_id)
    return _write_manifest_result(
        job_dir,
        status="committed",
        job_id=job_id,
        committed_paths=tuple(sorted(updated_contents)),
        committed_blocks=tuple(committed_blocks),
        source_updates=source_updates,
        issues=[],
    )


def _normalize_target(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("/")


def _target_path(mem_dir: Path, target: str) -> Path:
    if not is_accepted_t3_target(target):
        raise ValueError(f"non-canonical T3 target: {target}")
    return mem_dir.parent / target


def _extract_xml(markdown: str, tag: str, issues: list[str]) -> ET.Element | None:
    text = markdown or ""
    start = text.find(f"<{tag}")
    end = text.rfind(f"</{tag}>")
    if start < 0 or end < 0:
        issues.append(f"{tag} block missing")
        return None
    end += len(f"</{tag}>")
    try:
        return ET.fromstring(text[start:end])
    except ET.ParseError as exc:
        issues.append(f"{tag} XML parse failed: {exc}")
        return None


def _validate_patch_shape(patch: ET.Element, issues: list[str]) -> None:
    if patch.attrib.get("schema_version") != "t3.consolidation_patch.v1":
        issues.append("t3_consolidation_patch schema_version must be t3.consolidation_patch.v1")
    targets = [
        _normalize_target(node.attrib.get("path") or "")
        for node in patch.findall("./target_files/target_file")
        if str(node.attrib.get("path") or "").strip()
    ]
    if not targets:
        issues.append("target_files missing")
    for target in targets:
        if not is_accepted_t3_target(target):
            issues.append(f"non-canonical target: {target}")
    source_packages = [
        str(node.attrib.get("ref") or "").strip() for node in patch.findall("./source_packages/source_package")
    ]
    evidence_refs = [str(node.text or "").strip() for node in patch.findall("./evidence/source_ref")]
    if not any(ref.startswith("t2://") for ref in source_packages + evidence_refs) and not any(
        ref.startswith("explicit://") for ref in source_packages + evidence_refs
    ):
        issues.append("patch must reference at least one T2 package or explicit overlay entry")
    for ref in evidence_refs:
        if ref.startswith("t0://") and not any(
            src.startswith("t2://") or src.startswith("explicit://") for src in evidence_refs
        ):
            issues.append(f"T0 ref is not traceable through T2/explicit source refs: {ref}")
    _validate_t0_refs_derivable_from_t2(patch, issues)
    _validate_target_labels(patch.find("./target_view_labels"), issues)
    if patch.find("./proposed_changes") is None:
        issues.append("proposed_changes missing")


def _validate_t0_refs_derivable_from_t2(patch: ET.Element, issues: list[str]) -> None:
    provenance_refs = _source_refs_from_patch(patch)
    allowed_t2_segments = {_session_segment_key(ref, kind="t2") for ref in provenance_refs if ref.startswith("t2://")}
    allowed_t2_segments.discard(None)
    patch_refs = _refs_embedded_in_patch(patch)
    for ref in patch_refs:
        if not ref.startswith("t0://"):
            continue
        key = _session_segment_key(ref, kind="t0")
        if key is not None and key not in allowed_t2_segments:
            issues.append(f"T0 ref is not derivable from T2 source refs: {ref}")


def _refs_embedded_in_patch(patch: ET.Element) -> list[str]:
    refs = _source_refs_from_patch(patch)
    for change in patch.findall("./proposed_changes/*"):
        text = ET.tostring(change, encoding="unicode", method="xml")
        refs.extend(match.group(0) for match in _T0_SESSION_SEGMENT_RE.finditer(text))
        refs.extend(match.group(0) for match in _T2_SESSION_SEGMENT_RE.finditer(text))
    return _dedupe(refs)


def _session_segment_key(ref: str, *, kind: str) -> tuple[str, str] | None:
    pattern = _T0_SESSION_SEGMENT_RE if kind == "t0" else _T2_SESSION_SEGMENT_RE
    match = pattern.search(ref.strip())
    if not match:
        return None
    session_id, segment_id = match.groups()
    return session_id, segment_id


def _validate_target_labels(labels: ET.Element | None, issues: list[str]) -> None:
    if labels is None:
        issues.append("target_view_labels missing")
        return
    closed_fields = {
        "target_view": TARGET_VIEW_VALUES,
        "consolidation_mode": CONSOLIDATION_MODE_VALUES,
        "source_coverage": SOURCE_COVERAGE_VALUES,
        "stability": STABILITY_VALUES,
        "behavior_impact": BEHAVIOR_IMPACT_VALUES,
        "prompt_priority": PROMPT_PRIORITY_VALUES,
    }
    for field, allowed in closed_fields.items():
        value = (labels.findtext(field) or "").strip()
        if value and value not in allowed:
            issues.append(f"target_view_labels.{field} has invalid value: {value}")
    cue_strength = (labels.findtext("cue_strength") or "").strip()
    if cue_strength:
        try:
            cue = float(cue_strength)
        except ValueError:
            issues.append("cue_strength must be numeric")
        else:
            if cue < 0.0 or cue > 1.0:
                issues.append("cue_strength must be between 0.00 and 1.00")


def _validate_review(review: ET.Element, issues: list[str]) -> None:
    if review.attrib.get("schema_version") != "t3.review.v1":
        issues.append("memory_gate_review schema_version must be t3.review.v1")
    decision = (review.findtext("decision") or "").strip()
    if decision not in ACCEPTED_REVIEW_DECISIONS:
        issues.append(f"memory_gate_review decision must be accept for commit, got {decision or '<missing>'}")
    rubric = review.find("memory_gate_rubric")
    if rubric is None:
        issues.append("memory_gate_rubric missing")
        return
    if rubric.attrib.get("schema_version") != "memory_gate_rubric.v1":
        issues.append("memory_gate_rubric schema_version must be memory_gate_rubric.v1")
    scores: dict[str, int] = {}
    for node in rubric.findall("score"):
        name = str(node.attrib.get("name") or "").strip()
        raw_value = str(node.attrib.get("value") or "").strip()
        if name not in RUBRIC_SCORE_NAMES:
            issues.append(f"unknown memory_gate_rubric score {name or '<missing>'}")
            continue
        try:
            value = int(raw_value)
        except ValueError:
            issues.append(f"memory_gate_rubric score {name} must be integer 0-4")
            continue
        if value < 0 or value > 4:
            issues.append(f"memory_gate_rubric score {name} must be 0-4")
            continue
        rationale = (node.findtext("rationale") or "").strip()
        refs = [
            str(ref.text or "").strip()
            for ref in node.findall("./source_refs/source_ref")
            if str(ref.text or "").strip()
        ]
        if not rationale:
            issues.append(f"memory_gate_rubric score {name} missing rationale")
        if not refs:
            issues.append(f"memory_gate_rubric score {name} missing source_refs")
        scores[name] = value
    missing = sorted(set(RUBRIC_SCORE_NAMES) - set(scores))
    if missing:
        issues.append(f"memory_gate_rubric missing scores: {', '.join(missing)}")
    rubric_decision = (rubric.findtext("decision") or "").strip()
    if rubric_decision not in ACCEPTED_RUBRIC_DECISIONS:
        issues.append(f"memory_gate_rubric decision is not committable: {rubric_decision or '<missing>'}")
    if not missing and rubric_decision in {"accept_new", "merge_required", "supersede_existing", "reinforced"}:
        if scores["evidence_strength"] < 3:
            issues.append("evidence_strength below accepted threshold")
        if scores["scope_clarity"] < 3:
            issues.append("scope_clarity below accepted threshold")
        if scores["future_utility"] < 3:
            issues.append("future_utility below accepted threshold")
        if scores["conflict_safety"] < 3:
            issues.append("conflict_safety below accepted threshold")
        if sum(scores.values()) < 16:
            issues.append("memory_gate_rubric total below accepted threshold 16/20")


def _review_rubric_decision(review: ET.Element) -> str:
    return (review.findtext("./memory_gate_rubric/decision") or "").strip()


def _source_refs_from_patch(patch: ET.Element) -> list[str]:
    refs: list[str] = []
    for node in patch.findall("./source_packages/source_package"):
        ref = str(node.attrib.get("ref") or "").strip()
        if ref:
            refs.append(ref)
    for node in patch.findall("./evidence/source_ref"):
        ref = str(node.text or "").strip()
        if ref:
            refs.append(ref)
    return _dedupe(refs)


def _apply_source_lifecycle_updates(
    *,
    root: Path,
    agent_id: uuid.UUID,
    job_id: str,
    patch: ET.Element,
    review: ET.Element,
    committed_blocks: tuple[str, ...],
) -> dict[str, Any]:
    refs = _source_refs_from_patch(patch)
    decision = _review_rubric_decision(review)
    source_status = "reinforced" if decision == "reinforced" else "absorbed"
    updates: dict[str, Any] = {"t2_packages": [], "explicit_overlay_entries": []}
    for ref in refs:
        package_path = _t2_package_manifest_path(root=root, agent_id=agent_id, ref=ref)
        if package_path is not None and package_path.exists():
            if _update_t2_package_manifest(
                manifest_path=package_path,
                job_id=job_id,
                status=source_status,
                committed_blocks=committed_blocks,
            ):
                updates["t2_packages"].append(_relative_agent_path(root, agent_id, package_path.parent))
            continue
        explicit_id = _explicit_entry_id_from_ref(ref)
        if explicit_id:
            from app.memory.explicit_overlay import update_explicit_overlay_status

            if update_explicit_overlay_status(
                root,
                agent_id,
                explicit_id,
                status=source_status,
                reason=f"t3_commit:{job_id}",
                accepted_blocks=list(committed_blocks),
            ):
                updates["explicit_overlay_entries"].append(explicit_id)
    return updates


def _t2_package_manifest_path(*, root: Path, agent_id: uuid.UUID, ref: str) -> Path | None:
    match = re.match(r"^t2://session/([^/#]+)/(segment|episode)/([^/#]+)", ref.strip())
    if not match:
        return None
    session_id, ref_kind, source_id = match.groups()
    folder = "episodes" if ref_kind == "episode" else "segments"
    candidates = [
        root / str(agent_id) / "memory" / "t2" / "sessions" / session_id / folder / source_id / "manifest.json",
        root / str(agent_id) / "memory" / "sessions" / session_id / folder / source_id / "manifest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _explicit_entry_id_from_ref(ref: str) -> str:
    normalized = ref.strip()
    if normalized.startswith("explicit://memory/"):
        return normalized.removeprefix("explicit://memory/").split("#", 1)[0].strip()
    if normalized.startswith("explicit://"):
        return normalized.removeprefix("explicit://").split("#", 1)[0].strip()
    return ""


def _update_t2_package_manifest(
    *,
    manifest_path: Path,
    job_id: str,
    status: str,
    committed_blocks: tuple[str, ...],
) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    previous_status = str(manifest.get("package_status") or manifest.get("status") or "")
    if previous_status in {"absorbed", "reinforced", "contested", "retired"}:
        return False
    now = _now()
    manifest["package_status"] = status
    manifest["t3_absorbed_by"] = job_id
    manifest["t3_lifecycle_status"] = status
    manifest["t3_committed_blocks"] = list(committed_blocks)
    manifest["t3_lifecycle_updated_at"] = now
    if status == "absorbed":
        manifest["absorbed_at"] = now
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return True


def _relative_agent_path(root: Path, agent_id: uuid.UUID | str, path: Path) -> str:
    try:
        return path.relative_to(root / str(agent_id)).as_posix()
    except ValueError:
        return path.as_posix()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _validate_block_content(block_content: str, *, block_id: str, target: str) -> str:
    try:
        block = ET.fromstring(block_content)
    except ET.ParseError as exc:
        return f"block_content XML parse failed for {block_id}: {exc}"
    if (block.attrib.get("id") or "").strip() != block_id:
        return f"block_content id does not match block_id: {block_id}"
    target_prefix = {
        "memory/t3/episodes.md": "t3_episode",
        "memory/t3/user.md": "t3_user_memory",
        "memory/t3/worker.md": "t3_worker_rule",
        "memory/t3/capabilities.md": "t3_capability",
    }[target]
    if block.tag != target_prefix:
        return f"block_content root for {target} must be {target_prefix}"
    return ""


def _read_target(mem_dir: Path, target: str) -> str:
    path = _target_path(mem_dir, target)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _find_md_entry(content: str, entry_id: str) -> str | None:
    """Locate one `###` markdown entry anchored by `<!-- id: entry_id -->`."""
    lines = (content or "").splitlines()
    entry_start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("### "):
            entry_start = index
            continue
        if stripped.startswith("## "):
            entry_start = None
            continue
        match = _ENTRY_ID_COMMENT_RE.search(stripped)
        if match and match.group("entry_id") == entry_id and entry_start is not None:
            end = len(lines)
            for probe in range(index + 1, len(lines)):
                probe_stripped = lines[probe].strip()
                if probe_stripped.startswith("### ") or probe_stripped.startswith("## "):
                    end = probe
                    break
            return "\n".join(lines[entry_start:end]).rstrip()
    return None


def _insert_md_entry(content: str, *, section: str, entry_content: str) -> str:
    """Insert an entry at the end of `## <section>`; create the section if absent."""
    base = (content or "").rstrip()
    entry = entry_content.strip()
    if not section:
        return f"{base}\n\n{entry}\n" if base else f"{entry}\n"
    lines = base.splitlines()
    section_header = f"## {section}"
    section_start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == section_header:
            section_start = index
            break
    if section_start is None:
        prefix = f"{base}\n\n" if base else ""
        return f"{prefix}{section_header}\n\n{entry}\n"
    insert_at = len(lines)
    for probe in range(section_start + 1, len(lines)):
        if lines[probe].strip().startswith("## "):
            insert_at = probe
            break
    new_lines = lines[:insert_at] + ["", entry, ""] + lines[insert_at:]
    return "\n".join(new_lines).rstrip() + "\n"


def _retire_md_entry(existing: str, *, entry_id: str, job_id: str, reason: str) -> str:
    """Mark a profile-plane entry retired. Marking only — the convergence
    loop (工序 4) removes retired entries during full rewrites; the gate
    never deletes authored content mechanically."""
    marker = f"<!-- retired: by {job_id} at {_now()}" + (f"; reason: {reason}" if reason else "") + " -->"
    lines = existing.splitlines()
    for index, line in enumerate(lines):
        match = _ENTRY_ID_COMMENT_RE.search(line.strip())
        if match and match.group("entry_id") == entry_id:
            return "\n".join(lines[: index + 1] + [marker] + lines[index + 1 :])
    return existing + "\n" + marker


def _block_regex(block_id: str) -> re.Pattern[str]:
    escaped = re.escape(block_id)
    return re.compile(
        rf"<(?P<tag>t3_[A-Za-z0-9_]+)\b(?=[^>]*\bid=[\"']{escaped}[\"'])[^>]*>.*?</(?P=tag)>|"
        rf"<(?P<tag_self>t3_[A-Za-z0-9_]+)\b(?=[^>]*\bid=[\"']{escaped}[\"'])[^>]*/>",
        re.DOTALL,
    )


def _find_block(content: str, block_id: str) -> str | None:
    match = _block_regex(block_id).search(content)
    return match.group(0) if match else None


def _block_exists(content: str, block_id: str) -> bool:
    return _find_block(content, block_id) is not None


def _append_block(existing: str, block_content: str) -> str:
    base = existing.rstrip()
    return f"{base}\n\n{block_content.strip()}\n" if base else f"{block_content.strip()}\n"


def _retire_block(existing: str, *, job_id: str, reason: str) -> str:
    block = ET.fromstring(existing)
    block.set("status", "retired")
    block.set("retired_by", job_id)
    block.set("retired_at", _now())
    if reason:
        block.set("retire_reason", reason)
    return ET.tostring(block, encoding="unicode").strip()


def _atomic_write_targets(mem_dir: Path, contents: dict[str, str]) -> None:
    if not contents:
        return
    rollback_dir = mem_dir / ".staging" / "rollback" / f"t3-{uuid.uuid4().hex}"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for target, content in contents.items():
            path = _target_path(mem_dir, target)
            if path.exists():
                backup = rollback_dir / target.replace("/", "__")
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
            tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
            os.replace(tmp, path)
            written.append(path)
    except Exception:
        for path in written:
            backup = rollback_dir / _target_rel_from_path(mem_dir, path).replace("/", "__")
            if backup.exists():
                path.write_text(backup.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        raise


def _target_rel_from_path(mem_dir: Path, path: Path) -> str:
    return f"memory/{path.relative_to(mem_dir).as_posix()}"


def _write_conflict_bundle(
    *,
    job_dir: Path,
    target: str,
    expected_sha: str,
    current_sha: str,
    current_content: str,
) -> Path:
    path = job_dir / "conflict_bundle.json"
    payload = {
        "schema_version": "t3.conflict_bundle.v1",
        "target": target,
        "expected_sha": expected_sha,
        "current_sha": current_sha,
        "current_content_sha256": hashlib.sha256(current_content.encode("utf-8")).hexdigest(),
        "created_at": _now(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest = job_dir / "latest_t3_neighborhood.md"
    latest.write_text(f"# Latest T3 Neighborhood\n\n## {target}\n\n```\n{current_content}\n```\n", encoding="utf-8")
    return path


def _write_manifest_result(
    job_dir: Path,
    *,
    status: str,
    job_id: str,
    issues: list[str],
    committed_paths: tuple[str, ...] = (),
    committed_blocks: tuple[str, ...] = (),
    source_updates: dict[str, Any] | None = None,
    conflict_bundle_path: Path | None = None,
) -> T3PlatformGateResult:
    manifest_path = job_dir / "manifest.json"
    payload: dict[str, Any] = {
        "schema_version": "t3.job_manifest.v1",
        "job_id": job_id,
        "status": status,
        "issues": issues,
        "committed_paths": list(committed_paths),
        "committed_blocks": list(committed_blocks),
        "source_updates": source_updates or {},
        "updated_at": _now(),
        "write_audit": {
            "semantic_writer": "T3 Consolidator",
            "physical_committer": "Platform Gate",
            "commit_mode": "exact_llm_patch_atomic_commit",
        },
    }
    if conflict_bundle_path:
        payload["conflict_bundle"] = conflict_bundle_path.name
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return T3PlatformGateResult(
        status=status,
        job_id=job_id,
        committed_paths=committed_paths,
        committed_blocks=committed_blocks,
        issues=tuple(issues),
        manifest_path=manifest_path,
        conflict_bundle_path=conflict_bundle_path,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
