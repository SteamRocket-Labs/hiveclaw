"""T3 consolidation batch staging.

This builder prepares evidence and neighborhood context for LLM agents. It does
not decide memory semantics and does not write accepted T3 truth.
"""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.explicit_overlay import ExplicitMemoryOverlayEntry, load_explicit_overlay_entries
from app.memory.md_store import ensure_t3_layout, memory_dir
from app.memory.t3_platform_gate import PROFILE_PLANE_TARGETS, file_sha256

# Dynamic knowledge-plane targets are pattern-shaped, so the allowed-target
# list carries the pattern plus its contract inline (spec §3.4/§3.5).
DYNAMIC_TARGET_PATTERNS: tuple[str, ...] = (
    "memory/knowledge/<slug>.md (one atomic concept per page; a NEW page must carry >=1 `## Relations` edge)",
    "memory/milestones/<slug>.md (narrative anchors only; prefer ms- prefixed slugs)",
)


@dataclass(frozen=True, slots=True)
class T3ConsolidationBatchResult:
    status: str  # staged | held
    job_id: str
    job_dir: Path
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingT3Sources:
    package_dirs: tuple[Path, ...]
    explicit_entry_ids: tuple[str, ...]

    @property
    def has_sources(self) -> bool:
        return bool(self.package_dirs or self.explicit_entry_ids)


def discover_pending_t3_sources(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    max_packages: int = 8,
    max_explicit_entries: int = 8,
) -> PendingT3Sources:
    root = Path(data_root)
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    package_dirs = _discover_reviewed_t2_packages(root=root, agent_id=resolved_agent_id, limit=max_packages)
    explicit_ids = [
        entry.entry_id for entry in load_explicit_overlay_entries(root, resolved_agent_id) if entry.status == "active"
    ][:max_explicit_entries]
    return PendingT3Sources(package_dirs=tuple(package_dirs), explicit_entry_ids=tuple(explicit_ids))


def build_t3_consolidation_batch(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    package_dirs: list[Path] | tuple[Path, ...],
    explicit_entry_ids: list[str] | tuple[str, ...] | None = None,
    job_id: str | None = None,
) -> T3ConsolidationBatchResult:
    root = Path(data_root)
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    resolved_job_id = job_id or f"t3job-{uuid.uuid4().hex}"
    mem_dir = ensure_t3_layout(root, resolved_agent_id)
    job_dir = mem_dir / ".staging" / "t3_jobs" / resolved_job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    issues: list[str] = []
    packages = [
        _load_package(root=root, agent_id=resolved_agent_id, package_dir=Path(path), issues=issues)
        for path in package_dirs
    ]
    packages = [package for package in packages if package]
    explicit_entries = _load_explicit_overlay_sources(
        root=root,
        agent_id=resolved_agent_id,
        explicit_entry_ids=explicit_entry_ids,
        issues=issues,
    )
    if not packages and not explicit_entries:
        issues.append("T3 batch requires at least one reviewed T2 package or explicit overlay entry")

    source_bundle = {
        "schema_version": "t3.source_bundle.v1",
        "job_id": resolved_job_id,
        "agent_id": str(resolved_agent_id),
        "source_packages": packages,
        "explicit_overlay_entries": explicit_entries,
        "source_signature": _source_signature(packages=packages, explicit_entries=explicit_entries),
        "allowed_target_files": [*PROFILE_PLANE_TARGETS, *DYNAMIC_TARGET_PATTERNS],
        "created_at": _now(),
        "write_boundary": {
            "semantic_writer": "T3 Consolidator",
            "reviewer": "Memory Gate Agent",
            "physical_committer": "Platform Gate",
        },
    }
    (job_dir / "source_bundle.json").write_text(
        json.dumps(source_bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (job_dir / "t3_neighborhood.md").write_text(
        build_t3_neighborhood(data_root=root, agent_id=resolved_agent_id),
        encoding="utf-8",
    )

    _write_if_missing(
        job_dir / "consolidation_pitch.md",
        "# T3 Consolidation Pitch\n\nPending T3 Consolidator. Use `submit_t3_consolidation_pitch`.\n",
    )
    _write_if_missing(
        job_dir / "review.md",
        "# T3 Memory Gate Review\n\nPending Memory Gate Agent. Use `submit_t3_memory_gate_review`.\n",
    )
    _write_if_missing(
        job_dir / "revised_patch.md",
        "# T3 Revised Patch\n\nPending T3 Consolidator. Use `submit_t3_revised_patch`.\n",
    )
    manifest = {
        "schema_version": "t3.job_manifest.v1",
        "job_id": resolved_job_id,
        "status": "held" if issues else "staged",
        "issues": issues,
        "source_signature": _source_signature(packages=packages, explicit_entries=explicit_entries),
        "files": [
            "source_bundle.json",
            "t3_neighborhood.md",
            "consolidation_pitch.md",
            "review.md",
            "revised_patch.md",
            "manifest.json",
        ],
        "created_at": _now(),
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return T3ConsolidationBatchResult(
        status="held" if issues else "staged",
        job_id=resolved_job_id,
        job_dir=job_dir,
        issues=tuple(issues),
    )


def stage_pending_t3_consolidation_job(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    job_id: str | None = None,
    max_packages: int = 8,
    max_explicit_entries: int = 8,
) -> T3ConsolidationBatchResult:
    """Stage one T3 job from currently unabsorbed canonical inputs.

    This is the runtime handoff point for Heartbeat/T3 Consolidator. It does not
    write accepted T3 truth and it does not manufacture semantic content.
    """

    root = Path(data_root)
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    pending = discover_pending_t3_sources(
        agent_id=resolved_agent_id,
        data_root=root,
        max_packages=max_packages,
        max_explicit_entries=max_explicit_entries,
    )
    if job_id is None:
        existing = _find_existing_staged_job(root=root, agent_id=resolved_agent_id, pending=pending)
        if existing is not None:
            return existing
    return build_t3_consolidation_batch(
        agent_id=resolved_agent_id,
        data_root=root,
        package_dirs=pending.package_dirs,
        explicit_entry_ids=pending.explicit_entry_ids,
        job_id=job_id,
    )


def build_t3_neighborhood(*, data_root: Path | str, agent_id: uuid.UUID | str) -> str:
    root = Path(data_root)
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    mem_dir = ensure_t3_layout(root, resolved_agent_id)
    lines = [
        "# T3 Neighborhood",
        "",
        "This is a platform-built retrieval view for the T3 Consolidator and Memory Gate Agent.",
        "It is not semantic truth.",
        "",
        "## Base Revisions",
        "",
    ]
    lines.extend(_two_plane_neighborhood_lines(mem_dir))
    from app.memory.convergence import convergence_warning_lines

    lines.extend(convergence_warning_lines(agent_id=resolved_agent_id, data_root=root))
    return "\n".join(lines).rstrip() + "\n"


def _two_plane_neighborhood_lines(mem_dir: Path) -> list[str]:
    """Show the curator the current two-plane state (L1: complete visibility).

    Update-vs-create on knowledge pages and add-vs-update on profile entries
    are LLM judgments — they need the existing network and entry ids in view.
    """
    lines: list[str] = ["", "## Profile Plane (fixed convergent files)", ""]
    any_profile = False
    for target in PROFILE_PLANE_TARGETS:
        path = mem_dir.parent / target
        lines.append(f"- `{target}` base_revision=`{file_sha256(path)}`")
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(
            r"^###\s+(?P<heading>.+)$\n<!--\s*id:\s*(?P<entry_id>[^\s>]+)\s*-->", content, re.MULTILINE
        ):
            any_profile = True
            lines.append(f"  - entry id={match.group('entry_id')} heading={match.group('heading').strip()}")
    if not any_profile:
        lines.append("  (no profile-plane entries yet)")

    lines.extend(["", "## Knowledge Plane (network pages)", ""])
    any_page = False
    for subdir, label in (("knowledge", "knowledge"), ("milestones", "milestone")):
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            any_page = True
            content = path.read_text(encoding="utf-8", errors="replace")
            title = _frontmatter_field(content, "title") or path.stem
            status = _frontmatter_field(content, "status") or "active"
            lines.append(
                f"- `{subdir}/{path.stem}` [{label}] title={title} status={status} base_revision=`{file_sha256(path)}`"
            )
            claim = _first_section_line(content, "Current Claim")
            if claim:
                lines.append(f"  claim: {claim}")
            for relation in _relation_lines(content)[:6]:
                lines.append(f"  relation: {relation}")
    if not any_page:
        lines.append(
            "  (no knowledge/milestone pages yet — new knowledge pages must open the network with `## Relations` edges; forward references are allowed)"
        )
    return lines


def _frontmatter_field(content: str, field: str) -> str:
    if not content.startswith("---") or content.count("---") < 2:
        return ""
    frontmatter = content.split("---", 2)[1]
    match = re.search(rf"^{re.escape(field)}:\s*(?P<value>.+)$", frontmatter, re.MULTILINE)
    return match.group("value").strip() if match else ""


def _first_section_line(content: str, section: str) -> str:
    match = re.search(rf"^##\s+{re.escape(section)}\s*$\n+(?P<line>[^\n#].*)$", content, re.MULTILINE)
    return match.group("line").strip() if match else ""


def _relation_lines(content: str) -> list[str]:
    in_relations = False
    found: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r"^##\s+Relations\s*$", stripped):
            in_relations = True
            continue
        if in_relations and stripped.startswith("## "):
            break
        if in_relations and stripped.startswith("- "):
            found.append(stripped[2:].strip())
    return found


def write_t3_job_artifact(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    job_id: str,
    filename: str,
    content: str,
) -> Path:
    if filename not in {"consolidation_pitch.md", "review.md", "revised_patch.md"}:
        raise ValueError(f"unsupported T3 job artifact: {filename}")
    resolved_agent_id = uuid.UUID(str(agent_id)) if not isinstance(agent_id, uuid.UUID) else agent_id
    job_dir = memory_dir(Path(data_root), resolved_agent_id) / ".staging" / "t3_jobs" / job_id
    if not (job_dir / "source_bundle.json").exists():
        raise FileNotFoundError(f"T3 job not found or not staged: {job_id}")
    path = job_dir / filename
    path.write_text((content or "").strip() + "\n", encoding="utf-8")
    return path


def t3_explicit_ref(entry_id: str) -> str:
    return f"explicit://memory/{entry_id}"


def _load_package(*, root: Path, agent_id: uuid.UUID, package_dir: Path, issues: list[str]) -> dict[str, Any] | None:
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.exists():
        issues.append(f"package manifest missing: {package_dir}")
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        issues.append(f"package manifest unreadable: {package_dir}: {exc}")
        return None
    package_kind = _t2_package_kind(package_dir=package_dir, manifest=manifest)
    package_issues = _validate_t2_package_for_t3(package_dir=package_dir, manifest=manifest, package_kind=package_kind)
    issues.extend(f"{issue}: {package_dir}" for issue in package_issues)
    status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
    session_id, source_id = _session_and_source_id(
        root=root, agent_id=agent_id, package_dir=package_dir, manifest=manifest
    )
    ref_kind = "episode" if package_kind == "episode_stitch_package" else "segment"
    ref = f"t2://session/{session_id}/{ref_kind}/{source_id}"
    payload = {
        "ref": ref,
        "source_kind": package_kind,
        "path": _relative_agent_path(root, agent_id, package_dir),
        "status": status or "reviewed",
        "source_refs": [str(ref) for ref in manifest.get("source_refs") or []],
        "review_sha256": _sha256_file(package_dir / "review.md"),
        "manifest_sha256": _sha256_file(package_dir / "manifest.json"),
    }
    if package_kind == "episode_stitch_package":
        payload.update(
            {
                "synthesis_sha256": _sha256_file(package_dir / "synthesis.md"),
                "source_package_refs": [str(ref) for ref in manifest.get("source_packages") or []],
                "trigger_package_id": str(manifest.get("trigger_package_id") or ""),
            }
        )
        return payload
    return {
        **payload,
        "summary_sha256": _sha256_file(package_dir / "summary.md"),
        "labels_sha256": _sha256_file(package_dir / "labels.md"),
    }


def _load_explicit_overlay_sources(
    *,
    root: Path,
    agent_id: uuid.UUID,
    explicit_entry_ids: list[str] | tuple[str, ...] | None,
    issues: list[str],
) -> list[dict[str, Any]]:
    wanted = {str(entry_id).strip() for entry_id in (explicit_entry_ids or []) if str(entry_id).strip()}
    if not wanted:
        return []
    entries_by_id = {entry.entry_id: entry for entry in load_explicit_overlay_entries(root, agent_id)}
    sources: list[dict[str, Any]] = []
    for entry_id in sorted(wanted):
        entry = entries_by_id.get(entry_id)
        if entry is None:
            issues.append(f"explicit overlay entry missing: {entry_id}")
            continue
        if entry.status != "active":
            issues.append(f"explicit overlay entry must be active: {entry_id}")
            continue
        sources.append(_explicit_overlay_source(entry))
    return sources


def _explicit_overlay_source(entry: ExplicitMemoryOverlayEntry) -> dict[str, Any]:
    return {
        "id": entry.entry_id,
        "ref": t3_explicit_ref(entry.entry_id),
        "path": entry.path.as_posix(),
        "status": entry.status,
        "category": entry.category,
        "target_hint": entry.target_hint,
        "content": entry.content,
        "source_refs": list(entry.source_refs),
        "sensitivity": entry.sensitivity,
        "created_at": entry.created_at,
        "metadata": entry.metadata,
    }


def _source_signature(*, packages: list[dict[str, Any]], explicit_entries: list[dict[str, Any]]) -> str:
    package_refs = sorted(str(package.get("ref") or "") for package in packages if package.get("ref"))
    explicit_refs = sorted(str(entry.get("ref") or "") for entry in explicit_entries if entry.get("ref"))
    return json.dumps({"explicit": explicit_refs, "packages": package_refs}, ensure_ascii=False, sort_keys=True)


def _pending_signature(root: Path, agent_id: uuid.UUID, pending: PendingT3Sources) -> str:
    package_refs = sorted(
        _package_ref_from_dir(root=root, agent_id=agent_id, package_dir=path) for path in pending.package_dirs
    )
    explicit_refs = sorted(t3_explicit_ref(entry_id) for entry_id in pending.explicit_entry_ids)
    return json.dumps({"explicit": explicit_refs, "packages": package_refs}, ensure_ascii=False, sort_keys=True)


def _find_existing_staged_job(
    *,
    root: Path,
    agent_id: uuid.UUID,
    pending: PendingT3Sources,
) -> T3ConsolidationBatchResult | None:
    if not pending.has_sources:
        return None
    wanted_signature = _pending_signature(root, agent_id, pending)
    jobs_dir = memory_dir(root, agent_id) / ".staging" / "t3_jobs"
    if not jobs_dir.exists():
        return None
    for manifest_path in sorted(jobs_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if manifest.get("status") != "staged":
            continue
        if manifest.get("source_signature") != wanted_signature:
            continue
        return T3ConsolidationBatchResult(
            status="staged",
            job_id=str(manifest.get("job_id") or manifest_path.parent.name),
            job_dir=manifest_path.parent,
            issues=tuple(str(issue) for issue in manifest.get("issues") or []),
        )
    return None


def _package_ref_from_dir(*, root: Path, agent_id: uuid.UUID, package_dir: Path) -> str:
    try:
        manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        manifest = {}
    package_kind = _t2_package_kind(package_dir=package_dir, manifest=manifest)
    session_id, source_id = _session_and_source_id(
        root=root, agent_id=agent_id, package_dir=package_dir, manifest=manifest
    )
    ref_kind = "episode" if package_kind == "episode_stitch_package" else "segment"
    return f"t2://session/{session_id}/{ref_kind}/{source_id}"


def _discover_reviewed_t2_packages(*, root: Path, agent_id: uuid.UUID, limit: int) -> list[Path]:
    package_dirs: list[Path] = []
    manifest_paths: list[Path] = []
    for sessions_dir in (
        root / str(agent_id) / "memory" / "t2" / "sessions",
        root / str(agent_id) / "memory" / "sessions",
    ):
        if sessions_dir.exists():
            manifest_paths.extend(
                [*sessions_dir.glob("*/segments/*/manifest.json"), *sessions_dir.glob("*/episodes/*/manifest.json")]
            )
    for manifest_path in sorted(set(manifest_paths)):
        package_dir = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        package_kind = _t2_package_kind(package_dir=package_dir, manifest=manifest)
        if _validate_t2_package_for_t3(package_dir=package_dir, manifest=manifest, package_kind=package_kind):
            continue
        package_dirs.append(package_dir)
        if len(package_dirs) >= limit:
            break
    return package_dirs


def _t2_package_kind(*, package_dir: Path, manifest: dict[str, Any]) -> str:
    schema_version = str(manifest.get("schema_version") or "").strip()
    if schema_version == "t2.episode-stitch.manifest.v1" or package_dir.parent.name == "episodes":
        return "episode_stitch_package"
    return "segment_package"


def _validate_t2_package_for_t3(*, package_dir: Path, manifest: dict[str, Any], package_kind: str) -> list[str]:
    issues: list[str] = []
    status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
    if status not in {"reviewed", "closed"}:
        issues.append("package status must be reviewed/closed")
    review = _extract_single_xml(
        _read_optional(package_dir / "review.md"),
        "episode_review" if package_kind == "episode_stitch_package" else "t2_review",
    )
    if review is None:
        issues.append("review XML missing or invalid")
    elif (review.findtext("allowed_next") or "").strip() != "t3_intake":
        issues.append("package is not approved for t3_intake")
    if package_kind == "episode_stitch_package":
        synthesis = _extract_single_xml(_read_optional(package_dir / "synthesis.md"), "episode_synthesis")
        if synthesis is None:
            issues.append("episode synthesis XML missing or invalid")
        elif (synthesis.attrib.get("status") or "").strip() != "closed":
            issues.append("episode synthesis status must be closed")
        if not manifest.get("source_packages"):
            issues.append("episode source_packages missing")
        return issues

    summary = _extract_single_xml(_read_optional(package_dir / "summary.md"), "t2_summary")
    labels = _extract_single_xml(_read_optional(package_dir / "labels.md"), "t2_labels")
    if summary is None:
        issues.append("summary XML missing or invalid")
    elif _segment_state(summary) != "complete":
        issues.append("segment_state must be complete for t3_intake")
    if labels is None:
        issues.append("labels XML missing or invalid")
    elif (labels.findtext(".//continuity_state") or "").strip() != "standalone":
        issues.append("continuity_state must be standalone for t3_intake")
    return issues


def _session_and_source_id(
    *,
    root: Path,
    agent_id: uuid.UUID,
    package_dir: Path,
    manifest: dict[str, Any],
) -> tuple[str, str]:
    for sessions_root in (
        root / str(agent_id) / "memory" / "t2" / "sessions",
        root / str(agent_id) / "memory" / "sessions",
    ):
        try:
            rel = package_dir.relative_to(sessions_root)
            parts = rel.parts
            session_id = parts[0]
            if len(parts) >= 3 and parts[1] == "episodes":
                return session_id, parts[2]
            if len(parts) >= 3 and parts[1] == "segments":
                return session_id, parts[2]
        except ValueError:
            pass
    session_id = str(manifest.get("session_id") or "unknown")
    source_id = str(manifest.get("episode_id") or manifest.get("t0_segment_id") or package_dir.name)
    return session_id, source_id


def _segment_state(summary: ET.Element) -> str:
    node = summary.find("segment_state")
    if node is None:
        return ""
    return (node.attrib.get("value") or node.text or "").strip()


def _extract_single_xml(markdown: str, tag: str) -> ET.Element | None:
    text = markdown or ""
    starts = list(re.finditer(rf"<{re.escape(tag)}(?=[\s>/])", text))
    end = text.rfind(f"</{tag}>")
    if not starts or end < 0 or len(starts) > 1:
        return None
    end += len(f"</{tag}>")
    try:
        return ET.fromstring(text[starts[0].start() : end])
    except ET.ParseError:
        return None


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sha256_file(path: Path) -> str:
    return file_sha256(path) if path.exists() else ""


def _relative_agent_path(root: Path, agent_id: uuid.UUID | str, path: Path) -> str:
    try:
        return path.relative_to(root / str(agent_id)).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(UTC).isoformat()
