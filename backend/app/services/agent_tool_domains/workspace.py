from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
import json
from pathlib import Path

from app.config import get_settings
from app.services.managed_capability_guard import sanitize_managed_credential_guidance
from app.skills import SkillRegistry, WorkspaceSkillLoader
from app.tools.result_envelope import ToolContentEnvelope, render_tool_error

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(get_settings().AGENT_DATA_DIR)

# read_file returns these as a typed image block (CC Read parity) so
# vision-capable models see the image natively rather than mojibake.
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — base64 inline guard


def _workspace_error(
    tool_name: str,
    error_class: str,
    message: str,
    *,
    actionable_hint: str | None = None,
    retryable: bool = False,
) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider="workspace",
        retryable=retryable,
        actionable_hint=actionable_hint,
    )


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _append_workspace_provenance_hint(
    result: str,
    ws: Path,
    rel_path: str,
    *,
    directory: bool,
) -> str:
    try:
        from app.services.chat_artifact_delivery import workspace_artifact_provenance_hint

        hint = workspace_artifact_provenance_hint(ws, rel_path, directory=directory)
    except Exception:
        hint = ""
    return f"{result}\n\n{hint}" if hint else result


def _list_files(ws: Path, rel_path: str, tenant_id: str | None = None, tool_name: str = "list_files") -> str:
    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        target = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(target, enterprise_root):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        target = (ws / rel_path) if rel_path else ws
        target = target.resolve()
        if not _is_within_path(target, ws):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")

    if not target.exists():
        return _workspace_error(
            tool_name,
            "not_found",
            f"Directory not found: {rel_path or '/'}",
            actionable_hint="Check the directory path and list the parent directory first if needed.",
        )

    items = []
    if not rel_path:
        if tenant_id:
            enterprise_dir = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
        else:
            enterprise_dir = WORKSPACE_ROOT / "enterprise_info"
        if enterprise_dir.exists():
            items.append("  📁 enterprise_info/ (shared company info)")

    dir_count = 0
    file_count = 0
    for p in sorted(target.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dir_count += 1
            child_count = len([c for c in p.iterdir() if not c.name.startswith(".")])
            items.append(f"  📁 {p.name}/ ({child_count} items)")
        elif p.is_file():
            file_count += 1
            size_bytes = p.stat().st_size
            size_str = f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"
            items.append(f"  📄 {p.name} ({size_str})")

    if not items:
        return _append_workspace_provenance_hint(
            f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)",
            ws,
            rel_path,
            directory=True,
        )

    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    return _append_workspace_provenance_hint(header + "\n".join(items), ws, rel_path, directory=True)


def _read_file(
    ws: Path, rel_path: str, tenant_id: str | None = None, tool_name: str = "read_file"
) -> "str | ToolContentEnvelope":
    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(file_path, enterprise_root):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        file_path = (ws / rel_path).resolve()
        if not _is_within_path(file_path, ws):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")

    if not file_path.exists():
        return _workspace_error(
            tool_name,
            "not_found",
            f"File not found: {rel_path}",
            actionable_hint="Check the path or use glob_search/list_files to discover the correct file first.",
        )

    # Image files → typed image block (CC Read parity): vision-capable models see
    # the image natively; the text fallback names the file for text-only providers.
    image_media = _IMAGE_MEDIA_TYPES.get(file_path.suffix.lower())
    if image_media:
        try:
            import base64

            raw = file_path.read_bytes()
            if len(raw) > _MAX_IMAGE_BYTES:
                return _workspace_error(
                    tool_name,
                    "operation_failed",
                    f"Image too large ({len(raw)} bytes; max {_MAX_IMAGE_BYTES}).",
                )
            image_text = _append_workspace_provenance_hint(
                f"[image: {rel_path} ({image_media}, {len(raw)} bytes)]",
                ws,
                rel_path,
                directory=False,
            )
            return ToolContentEnvelope.image(
                text=image_text,
                media_type=image_media,
                data=base64.b64encode(raw).decode("ascii"),
            )
        except Exception as e:
            return _workspace_error(tool_name, "operation_failed", f"Image read failed: {e}")

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if len(content) > 16000:
            content = content[:16000] + f"\n\n...[truncated, {len(content)} chars total]"
        if _is_skill_instruction_file(ws, file_path):
            content = sanitize_managed_credential_guidance(content)
        return _append_workspace_provenance_hint(content, ws, rel_path, directory=False)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Read failed: {e}")


def _is_skill_instruction_file(ws: Path, file_path: Path) -> bool:
    try:
        rel = file_path.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        return False
    parts = Path(rel).parts
    for idx, part in enumerate(parts):
        if part != "skills" or idx + 1 >= len(parts):
            continue
        skill_parts = parts[idx + 1 :]
        return (len(skill_parts) == 1 and skill_parts[0].endswith(".md")) or (
            len(skill_parts) == 2 and skill_parts[1] in {"SKILL.md", "skill.md"}
        )
    return False


def _skill_scope_guidance(metadata) -> str:
    """Step 9: surface a skill's `allowed-tools` as scoped tool guidance.

    CC parity: a loaded skill advertises which tools it is designed to use. This
    is L1/L2-faithful guidance, NOT a hard filter — the model keeps full
    intelligence and every tool call is still governed by normal permissions.
    Only the registry path needs this; the explicit-path returns the raw file
    (frontmatter included), so the model already sees `allowed-tools` there.
    """
    allowed = tuple(getattr(metadata, "allowed_tools", ()) or ())
    if not allowed:
        return ""
    tools = ", ".join(allowed)
    return (
        "\n\n---\n"
        f"**Tool scope (skill guidance):** this skill is designed to work with: {tools}. "
        "Prefer these tools for its workflow. This is guidance, not a hard limit — "
        "every tool call remains governed by your normal permissions."
    )


def _load_skill(ws: Path, skill_name: str, tool_name: str = "load_skill") -> str:
    requested = (skill_name or "").strip()
    if not requested:
        return _workspace_error(tool_name, "bad_arguments", "Skill name cannot be empty.")

    workspace_root = ws.resolve()
    skills_dir = (ws / "skills").resolve()

    def _read_skill_file(path: Path) -> str:
        if not _is_within_path(path, workspace_root) or not _is_skill_instruction_file(ws, path):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this skill path.")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return sanitize_managed_credential_guidance(content)
        except Exception as e:
            return _workspace_error(tool_name, "operation_failed", f"Read failed: {e}")

    requested_path = requested
    explicit_paths: list[Path]
    if requested_path.startswith("skills/"):
        explicit_paths = [(workspace_root / requested_path).resolve()]
    elif "/" in requested_path or requested_path.endswith(".md"):
        explicit_paths = [(workspace_root / requested_path).resolve(), (skills_dir / requested_path).resolve()]
    else:
        explicit_paths = [(skills_dir / requested_path).resolve()]
    for explicit_path in explicit_paths:
        if explicit_path.is_file():
            body = _read_skill_file(explicit_path)
            try:
                rel = explicit_path.relative_to(ws).as_posix()
                slug = _skill_slug_from_relative_path(rel)
                if slug:
                    from app.services.skill_curator import bump_skill_use

                    bump_skill_use(ws, slug, kind="use")
            except Exception as exc:  # pragma: no cover - telemetry must never break load
                logger.debug("[workspace] curator use bump (explicit) failed: %s", exc)
            return body

    registry = _build_skill_registry(ws)
    try:
        # Step 9: resolve once so allowed-tools can be re-surfaced as scoped tool
        # guidance. The registry path strips frontmatter (where allowed-tools
        # lives), unlike the explicit path which returns the raw file. A missing
        # skill raises KeyError and falls through to the tool-pack check below.
        skill = registry.resolve(requested)
        body = sanitize_managed_credential_guidance(skill.body)
        body += _skill_scope_guidance(skill.metadata)
        _curator_bump_use(ws, requested)
        return body
    except KeyError:
        # Not a workspace skill — fall through to check tool packs
        logger.debug("Skill %r not found in workspace, checking tool packs", requested)

    # Fallback: if the name matches a runtime tool group, return truthful
    # guidance only. `load_skill` never changes the callable schema set.
    from app.tools.runtime_tool_groups import runtime_tool_group_for_name

    pack = runtime_tool_group_for_name(requested)
    if pack:
        tool_selectors = ", ".join(f"`select:{tool_name}`" for tool_name in pack.tools[:8])
        if len(pack.tools) > 8:
            tool_selectors += ", ..."
        return (
            f"## Runtime Tool Group: {pack.name}\n\n"
            f"{pack.summary}\n\n"
            f"**Deferred tools:** {', '.join(pack.tools)}\n\n"
            "This is a runtime tool group, not a skill file. load_skill does not make these schemas callable. "
            f"Use `tool_search` with this group name or a specific selector such as {tool_selectors} to load "
            "matching deferred schemas for this session. If you need capability-capsule guidance, load the relevant skill or "
            "system guide separately."
        )

    return _workspace_error(tool_name, "not_found", f"Skill not found: {skill_name}")


def _build_skill_registry(ws: Path) -> SkillRegistry:
    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(ws))
    return registry


def _skill_slug_from_relative_path(relative_path: str) -> str | None:
    """Map a skill ``relative_path`` to its curator slug (the dir/file name
    directly under a ``skills/`` directory). Returns None for anything not under skills/."""
    parts = Path(relative_path).parts
    for idx, part in enumerate(parts):
        if part != "skills" or idx + 1 >= len(parts):
            continue
        second = parts[idx + 1]
        # Flat layout: */skills/<slug>.md → slug is the stem.
        if second.endswith(".md") and len(parts) == idx + 2:
            return second[: -len(".md")]
        return second
    return None


def _curator_bump_use(ws: Path, skill_name: str) -> None:
    """Best-effort usage telemetry for the skill curator. Never raises.

    Resolves the on-disk slug for ``skill_name`` (via the registry's
    relative_path) and bumps its use counter. Failures are swallowed so a
    broken sidecar can never break ``load_skill``.
    """
    try:
        from app.services.skill_curator import bump_skill_use

        slug: str | None = None
        try:
            parsed = _build_skill_registry(ws).resolve(skill_name)
            slug = _skill_slug_from_relative_path(parsed.relative_path)
        except KeyError:
            slug = None
        if slug:
            bump_skill_use(ws, slug, kind="use")
    except Exception as exc:  # pragma: no cover - telemetry must never break load
        logger.debug("[workspace] curator use bump failed for %s: %s", skill_name, exc)


def _normalize_skill_folder_name(name: str) -> str:
    slug = re.sub(r"\s+", "-", name.strip().lower())
    slug = re.sub(r"[^\w\-]+", "-", slug, flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def _render_skill_markdown(
    *,
    name: str,
    description: str,
    instructions: str,
    declared_tools: tuple[str, ...],
    declared_packs: tuple[str, ...],
) -> str:
    lines = [
        "---",
        f'name: "{name}"',
        f'description: "{description}"',
    ]
    # New agent-authored skills follow the Skill Creator contract: SKILL.md
    # frontmatter contains only name/description. Tool and pack hints stay in
    # the candidate manifest so legacy parser compatibility does not leak into
    # newly generated skill bodies.
    _ = declared_tools, declared_packs
    lines.append("---")

    body = instructions.strip()
    if not body.startswith("#"):
        body = f"# {name}\n\n{body}"

    return "\n".join(lines) + "\n" + body.rstrip() + "\n"


def _find_similar_existing_skill(
    ws: Path,
    *,
    name: str,
    description: str,
):
    """Return the most similar existing skill (ParsedSkill + Jaccard score) if
    it exceeds SKILL_DEDUP_THRESHOLD, else None.

    Compares `(name + " " + description)` against every loaded skill in the
    workspace using word-level Jaccard. Catches heartbeat-pushed repeats
    like "web-research-brief" vs "research-brief-web" where the body is
    nearly identical.
    """
    from app.memory.md_store import SKILL_DEDUP_THRESHOLD, jaccard_similarity

    if not name or not description:
        return None

    try:
        registry = _build_skill_registry(ws)
    except Exception as exc:
        logger.warning("[workspace] skill dedup registry build failed: %s", exc)
        return None

    candidate_text = f"{name} {description}".strip()
    best = None
    for skill_name in registry.names():
        parsed = registry.resolve(skill_name)
        meta = parsed.metadata
        existing_text = f"{meta.name} {meta.description}".strip()
        score = jaccard_similarity(candidate_text, existing_text)
        if score >= SKILL_DEDUP_THRESHOLD and (best is None or score > best[1]):
            best = (parsed, score)
    return best


def _find_similar_pending_skill_candidate(
    ws: Path,
    *,
    name: str,
    description: str,
) -> tuple[dict, float] | None:
    """Return the most similar inactive skill candidate package if present."""

    from app.memory.md_store import SKILL_DEDUP_THRESHOLD, jaccard_similarity

    if not name or not description:
        return None
    root = ws / "evolution" / "skill_candidates"
    if not root.exists():
        return None
    candidate_text = f"{name} {description}".strip()
    best: tuple[dict, float] | None = None
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        draft_path_raw = manifest.get("draft_path") or manifest.get("candidate_signal_path")
        draft_text = ""
        if draft_path_raw:
            draft_path = (ws / str(draft_path_raw)).resolve()
            if _is_within_path(draft_path, ws) and draft_path.exists():
                draft_text = draft_path.read_text(encoding="utf-8", errors="replace")
        candidate_name = str(manifest.get("skill_name") or "")
        candidate_description = ""
        for line in draft_text.splitlines():
            if line.strip().startswith("description:"):
                candidate_description = line.split(":", 1)[1].strip().strip('"')
                break
        existing_text = f"{candidate_name} {candidate_description}".strip()
        if not existing_text:
            continue
        score = jaccard_similarity(candidate_text, existing_text)
        if score >= SKILL_DEDUP_THRESHOLD and (best is None or score > best[1]):
            best = (manifest, score)
    return best


async def check_declared_packs_authorized(
    *,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    declared_packs: tuple[str, ...],
) -> tuple[bool, str]:
    """Backward-compatible no-op.

    C2 makes ``packs:`` skill metadata a discovery hint rather than a save-time
    existence or permission gate. Actual access remains enforced by call-time
    governance when the agent invokes a discovered tool.
    """
    del tenant_id, agent_id, declared_packs
    return True, ""


def _save_skill(
    ws: Path,
    *,
    name: str,
    description: str,
    instructions: str,
    declared_tools: tuple[str, ...] = (),
    declared_packs: tuple[str, ...] = (),
    folder_name: str | None = None,
    overwrite: bool = False,
    tool_name: str = "save_skill",
) -> str:
    del name, description, instructions, declared_tools, declared_packs, folder_name, overwrite
    return _workspace_error(
        tool_name,
        "retired_direct_activation_path",
        "Direct active skill writes are retired. save_skill can only submit inactive Skill Candidate Packages.",
        actionable_hint=(
            "Call save_skill through the tool handler, or use _submit_skill_activation_candidate; "
            "Skill Distiller must promote a reviewed SKILL.md.draft through Platform Skill Gate."
        ),
    )


def _submit_skill_activation_candidate(
    ws: Path,
    *,
    agent_id: uuid.UUID | None,
    name: str,
    description: str,
    instructions: str,
    declared_tools: tuple[str, ...] = (),
    declared_packs: tuple[str, ...] = (),
    folder_name: str | None = None,
    overwrite: bool = False,
    tool_name: str = "save_skill",
) -> str:
    """Record an LLM-authored skill draft without activating it.

    Active skill writes are promotion decisions. They must go through platform
    verification (SkillGuard + behavior eval + regression gate), not through a
    model-supplied tool call.
    """

    skill_name = (name or "").strip()
    skill_description = (description or "").strip()
    skill_instructions = (instructions or "").strip()
    if not skill_name:
        return _workspace_error(tool_name, "bad_arguments", "Skill name cannot be empty.")
    if not skill_description:
        return _workspace_error(tool_name, "bad_arguments", "Skill description cannot be empty.")
    if not skill_instructions:
        return _workspace_error(tool_name, "bad_arguments", "Skill instructions cannot be empty.")

    requested_folder = (folder_name or "").strip()
    slug = _normalize_skill_folder_name(requested_folder or skill_name)
    if not slug:
        return _workspace_error(
            tool_name, "bad_arguments", "Skill name/folder_name must contain at least one valid character."
        )
    skills_dir = (ws / "skills").resolve()
    target = (skills_dir / slug / "SKILL.md").resolve()
    if not _is_within_path(target, skills_dir):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this skill path.")
    target_rel = f"skills/{slug}/SKILL.md"
    if target.exists() and not overwrite:
        return _workspace_error(
            tool_name,
            "already_exists",
            f"Skill already exists at {target_rel}.",
            actionable_hint=(
                "Pass overwrite=true to request a patch candidate, or choose a different skill name/folder_name."
            ),
        )
    if not overwrite:
        similar = _find_similar_existing_skill(ws, name=skill_name, description=skill_description)
        if similar is not None:
            sim_skill, sim_score = similar
            return _workspace_error(
                tool_name,
                "similar_skill_exists",
                (
                    f"A semantically similar skill already exists "
                    f"(similarity={sim_score:.2f}): {sim_skill.metadata.name} at "
                    f"{sim_skill.relative_path}. Description: {sim_skill.metadata.description}"
                ),
                actionable_hint=(
                    "Patch the existing skill (pass overwrite=true and the same name/folder), "
                    "or pick a clearly distinct name and description that captures the difference."
                ),
            )
        similar_candidate = _find_similar_pending_skill_candidate(
            ws,
            name=skill_name,
            description=skill_description,
        )
        if similar_candidate is not None:
            manifest, sim_score = similar_candidate
            return _workspace_error(
                tool_name,
                "similar_skill_exists",
                (
                    f"A semantically similar skill candidate already exists "
                    f"(similarity={sim_score:.2f}): {manifest.get('skill_name')} at "
                    f"{manifest.get('draft_path') or manifest.get('candidate_signal_path')}"
                ),
                actionable_hint=(
                    "Wait for the existing candidate to pass Skill Gate, or submit a patch candidate with "
                    "overwrite=true if this adds distinct value."
                ),
            )

    rendered = _render_skill_markdown(
        name=skill_name,
        description=skill_description,
        instructions=skill_instructions,
        declared_tools=tuple(dict.fromkeys(tool.strip() for tool in declared_tools if tool.strip())),
        declared_packs=tuple(dict.fromkeys(pack.strip() for pack in declared_packs if pack.strip())),
    )
    from app.services.skill_guard import scan_skill_files

    guard_report = scan_skill_files([{"path": "SKILL.md", "content": rendered}], source="save_skill_candidate")
    if not guard_report.allowed:
        categories = ", ".join(finding.category for finding in guard_report.blocking_findings)
        return _workspace_error(
            tool_name,
            "skill_guard_blocked",
            f"SkillGuard blocked this skill before activation: {categories}",
            actionable_hint="Remove embedded secrets, remote shell installers, path escapes, or destructive commands.",
        )

    candidate_id = f"save-skill-{uuid.uuid4()}"
    from app.services.skill_candidate_package import write_skill_candidate_package
    from app.services.skill_evolution_registry import ORIGIN_USER_SKILL_CREATOR, upsert_skill_evolution_entry

    manifest = write_skill_candidate_package(
        workspace=ws,
        candidate_id=candidate_id,
        rendered_markdown=rendered,
        skill_name=skill_name,
        package_type="save_skill",
        target_path=target_rel,
        skill_origin=ORIGIN_USER_SKILL_CREATOR,
        evolvable=True,
        source_refs=["tool:save_skill"],
        reason="Agent submitted a reusable capability capsule for external behavior verification.",
        declared_tools=tuple(dict.fromkeys(tool.strip() for tool in declared_tools if tool.strip())),
        declared_packs=tuple(dict.fromkeys(pack.strip() for pack in declared_packs if pack.strip())),
        status="pending_behavior_verification",
        extra_metadata={
            "agent_id": str(agent_id) if agent_id else None,
            "overwrite_requested": bool(overwrite),
            "source_tool": tool_name,
        },
    )
    upsert_skill_evolution_entry(
        ws,
        skill_name=skill_name,
        target_path=target_rel,
        skill_origin=ORIGIN_USER_SKILL_CREATOR,
        evolvable=True,
        last_candidate_id=candidate_id,
        state="candidate",
        source_refs=["tool:save_skill"],
        metadata={"draft_path": manifest["draft_path"], "source_tool": tool_name},
    )
    from app.services.evolution_ledger import record_evolution_candidate

    record_evolution_candidate(
        ws,
        target_type="skill",
        target_id=target_rel,
        diff=rendered,
        source_attempt_ids=["tool:save_skill"],
        baseline_version=target_rel if overwrite else "none",
        candidate_id=candidate_id,
        metadata={
            "lane": "save_skill",
            "package_type": "save_skill",
            "package_manifest_path": manifest["manifest_path"],
            "draft_path": manifest["draft_path"],
            "target_path": target_rel,
            "overwrite_requested": bool(overwrite),
            "agent_id": str(agent_id) if agent_id else None,
            "source_tool": tool_name,
        },
    )

    try:
        from app.services.skill_lifecycle import record_skill_lifecycle_event

        record_skill_lifecycle_event(
            ws,
            skill_name=skill_name,
            status="candidate",
            note=(
                f"Submitted via save_skill for external behavior verification at {manifest['draft_path']}; "
                f"target={target_rel}"
            ),
        )
    except Exception as exc:
        logger.warning("[workspace] Failed to record skill activation candidate for %s: %s", skill_name, exc)

    return (
        f"🟡 save_skill submitted for review at evolution/skill_candidates/{candidate_id}/\n"
        f"- candidate_id: {candidate_id}\n"
        f"- target: {target_rel}\n"
        f"- draft: {manifest['draft_path']}\n"
        f"- declared_tools: {', '.join(declared_tools) or '(none)'}\n"
        f"- declared_packs: {', '.join(declared_packs) or '(none)'}\n"
        "- active_skill_created: false\n"
        "- promotion gate: external behavior eval + SkillGuard + regression check must pass before activation."
    )


async def _read_document(
    ws: Path,
    rel_path: str,
    max_chars: int = 8000,
    tenant_id: str | None = None,
    tool_name: str = "read_document",
    mode: str = "auto",
    max_pages: int | None = None,
    force_refresh: bool = False,
    return_format: str = "preview",
) -> str:
    workspace_root = ws
    if rel_path and rel_path.startswith("enterprise_info"):
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        workspace_root = enterprise_root
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(file_path, enterprise_root):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        file_path = (ws / rel_path).resolve()
        if not _is_within_path(file_path, ws):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")

    if not file_path.exists():
        return _workspace_error(tool_name, "not_found", f"File not found: {rel_path}")

    try:
        from app.services.document_conversion import (
            DocumentConversionRequest,
            DocumentConversionService,
            render_conversion_preview,
        )

        result = DocumentConversionService().convert(
            DocumentConversionRequest(
                source_path=file_path,
                workspace_root=workspace_root,
                source_uri=None,
                tenant_id=tenant_id,
                agent_id=None,
                user_id=None,
                mode=mode if mode in {"auto", "fast", "ocr", "layout", "vision"} else "auto",
                max_pages=max_pages,
                max_output_chars=max_chars,
                force_refresh=force_refresh,
            )
        )
        if return_format == "markdown":
            content = result.markdown
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n...[truncated, {len(result.markdown)} chars total]"
            return content
        if return_format == "metadata":
            metadata_path = workspace_root / result.artifact_metadata_path
            return metadata_path.read_text(encoding="utf-8", errors="replace")
        if return_format == "pages":
            return (
                "Page-level Markdown artifacts are not available yet.\n"
                f"Full Markdown: {result.artifact_markdown_path}\n"
                f"Metadata: {result.artifact_metadata_path}"
            )
        return render_conversion_preview(result, max_chars=max_chars)
    except ImportError as e:
        return _workspace_error(
            tool_name,
            "dependency_missing",
            f"Missing dependency: {e}. Install the document conversion backend dependencies.",
            actionable_hint="Install the required document parsing dependency in the backend environment.",
        )
    except ValueError as e:
        return _workspace_error(tool_name, "bad_arguments", str(e))
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Document read failed: {str(e)[:200]}")


_WRITE_PROTECTED = {
    "tasks.json": (
        "tasks.json is a read-only DB Task snapshot. Use track_todo/read_ledger for the agent work board; "
        "use the Tasks UI or REST task API for DB-backed task execution."
    ),
    "soul.md": (
        "soul.md is governed by Dream/Soul promotion. Direct write_file/edit_file is refused; "
        "Dream must write a soul.md.next candidate and the promotion gate performs the audited commit."
    ),
}

# memory/ is governed by the Memory Control Plane: explicit memories must run
# through save_memory -> Explicit Memory Overlay, and accepted T3 writes must
# run through T3 Consolidation + Platform Gate. Raw file writes bypass audit.
_GOVERNED_MEMORY_PREFIX = "memory/"
_GOVERNED_MEMORY_MESSAGE = (
    "memory/ is managed by platform services and governed by the Memory Control Plane — "
    "direct file writes are not allowed. "
    "Use save_memory for explicit user-commanded memories, or the T3 consolidation tools "
    "for staged pitch/review/revised_patch artifacts. Accepted T3 files are committed only "
    "by Platform Gate."
)

_PLATFORM_MANAGED_PREFIX_MESSAGES = {
    "logs": (
        "logs/ is managed by platform services — direct file writes, edits, and deletes are not allowed. "
        "Use read_file, list_files, glob_search, or grep_search when you need log evidence."
    ),
    "evolution": (
        "evolution/ is managed by platform services — direct file writes, edits, and deletes are not allowed. "
        "Return the outcome summary instead; runtime evidence enters governed memory/session paths, "
        "and Skill changes use inactive Skill Candidate Packages plus Skill Gate."
    ),
    "runtime_artifacts": (
        "runtime_artifacts/ is managed by platform services — direct file writes, edits, and deletes are not allowed. "
        "Read it only as recovery or audit evidence when a tool result points there."
    ),
}

_ROOT_WRITE_ALLOWLIST: set[str] = set()
_ROOT_PREFIX_ALLOWLIST = {"workspace", "skills"}
_ENTERPRISE_ASSET_PREFIX_MESSAGES = {
    "subagents": "subagents/ contains enterprise Sub-agent assets; use governed Sub-agent APIs instead of raw file writes.",
    "enterprise_info": (
        "enterprise_info/ contains governed company knowledge; use enterprise knowledge APIs instead of raw file writes."
    ),
}
_ROOT_MANAGED_FILE_MESSAGES = {
    "HEARTBEAT.md": "HEARTBEAT.md is a platform template; heartbeat protocol updates must ship through system templates.",
    "DREAM.md": "DREAM.md is a platform template; dream protocol updates must ship through system templates.",
    "state.json": "state.json is a retired legacy runtime snapshot; runtime state belongs under runtime_artifacts/.",
}


def _is_governed_memory_path(rel_path: str) -> bool:
    normalized = rel_path.strip("/").replace("\\", "/")
    return normalized == "memory" or normalized.startswith(_GOVERNED_MEMORY_PREFIX)


def _managed_system_path_message(rel_path: str) -> str | None:
    normalized = rel_path.strip("/").replace("\\", "/")
    if _is_governed_memory_path(normalized):
        return _GOVERNED_MEMORY_MESSAGE
    top_level = normalized.split("/", 1)[0]
    return _PLATFORM_MANAGED_PREFIX_MESSAGES.get(top_level)


def _root_write_guard_message(rel_path: str) -> str | None:
    normalized = rel_path.strip("/").replace("\\", "/")
    if not normalized:
        return "Missing file path. Write deliverables under workspace/."
    if normalized in _ROOT_WRITE_ALLOWLIST:
        return None
    protected_message = _WRITE_PROTECTED.get(normalized)
    if protected_message:
        return protected_message
    managed_file_message = _ROOT_MANAGED_FILE_MESSAGES.get(normalized)
    if managed_file_message:
        return managed_file_message
    top_level = normalized.split("/", 1)[0]
    enterprise_asset_message = _ENTERPRISE_ASSET_PREFIX_MESSAGES.get(top_level)
    if enterprise_asset_message:
        return enterprise_asset_message
    if "/" in normalized and top_level in _ROOT_PREFIX_ALLOWLIST:
        return None
    if "/" not in normalized:
        return "Top-level work files are not allowed. Write deliverables under workspace/ (for example workspace/report.md)."
    if top_level not in _ROOT_PREFIX_ALLOWLIST and not _managed_system_path_message(normalized):
        return (
            f"{top_level}/ is not a writable agent file namespace. Use workspace/ for deliverables, "
            "save_skill for skill activation candidates, or a dedicated platform API."
        )
    return None


def _skill_package_path_guard_message(rel_path: str, *, operation: str) -> str | None:
    normalized = rel_path.strip("/").replace("\\", "/")
    if not normalized.startswith("skills/"):
        return None
    del operation
    return (
        "Active skill packages are governed by Skill promotion. Direct file writes, edits, and deletes under "
        "skills/ are refused; use save_skill to submit an activation candidate, or let Skill Distiller promote "
        "a verified SKILL.md.draft through Platform Skill Gate."
    )


def _write_file(ws: Path, rel_path: str, content: str, tool_name: str = "write_file") -> str:
    if not rel_path or not rel_path.strip("/"):
        return _workspace_error(
            tool_name,
            "bad_arguments",
            "Missing file path.",
            actionable_hint="Pass a workspace-relative file path such as workspace/report.md.",
        )

    _blocked = _WRITE_PROTECTED.get(rel_path.strip("/"))
    if _blocked:
        return _workspace_error(tool_name, "auth_or_permission", _blocked)

    managed_message = _managed_system_path_message(rel_path)
    if managed_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            managed_message,
            actionable_hint=(
                "Use save_memory only for explicit user-commanded memory; use "
                "submit_t3_consolidation_pitch / submit_t3_revised_patch for accepted T3 candidates."
                if _is_governed_memory_path(rel_path)
                else "Write deliverables under workspace/ unless a dedicated tool returns another writable path."
            ),
        )

    root_guard_message = _root_write_guard_message(rel_path)
    if root_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            root_guard_message,
            actionable_hint="Use a path under workspace/ for user-facing work artifacts.",
        )

    skill_guard_message = _skill_package_path_guard_message(rel_path, operation="write")
    if skill_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            skill_guard_message,
            actionable_hint="Use save_skill to submit a skill activation candidate; active skills require Platform Skill Gate.",
        )

    file_path = (ws / rel_path).resolve()
    if not _is_within_path(file_path, ws):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"✅ Written to {rel_path} ({len(content)} chars)"
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Write failed: {e}")


def _edit_file(
    ws: Path,
    rel_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    tool_name: str = "edit_file",
) -> str:
    managed_message = _managed_system_path_message(rel_path)
    if managed_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            managed_message,
            actionable_hint=(
                "Use save_memory only for explicit user-commanded memory; use "
                "submit_t3_consolidation_pitch / submit_t3_revised_patch for accepted T3 candidates."
                if _is_governed_memory_path(rel_path)
                else "Create or update normal work artifacts under workspace/ instead."
            ),
        )
    root_guard_message = _root_write_guard_message(rel_path)
    if root_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            root_guard_message,
            actionable_hint="Edit normal work artifacts under workspace/.",
        )
    skill_guard_message = _skill_package_path_guard_message(rel_path, operation="edit")
    if skill_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            skill_guard_message,
            actionable_hint="Submit a new save_skill candidate or let Skill Distiller patch through Platform Skill Gate.",
        )
    file_path = (ws / rel_path).resolve()
    if not _is_within_path(file_path, ws):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if not file_path.exists():
        return _workspace_error(tool_name, "not_found", f"File not found: {rel_path}")

    try:
        original = file_path.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(old_text)
        if occurrences == 0:
            return _workspace_error(
                tool_name,
                "bad_arguments",
                f"Could not find the target text in {rel_path}.",
                actionable_hint="Read the file first and provide a unique exact old_text match.",
            )
        if not replace_all and occurrences != 1:
            return _workspace_error(
                tool_name,
                "bad_arguments",
                f"Found {occurrences} matches in {rel_path}. Refine old_text or set replace_all=true.",
            )
        updated = original.replace(old_text, new_text, -1 if replace_all else 1)
        file_path.write_text(updated, encoding="utf-8")
        replaced = occurrences if replace_all else 1
        return f"✅ Updated {rel_path} ({replaced} replacement{'s' if replaced != 1 else ''})"
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Edit failed: {e}")


def _glob_search(ws: Path, pattern: str, root: str = "", tool_name: str = "glob_search") -> str:
    search_root = (ws / root).resolve() if root else ws.resolve()
    if not _is_within_path(search_root, ws):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if not search_root.exists():
        return _workspace_error(tool_name, "not_found", f"Directory not found: {root or '/'}")

    matches: list[str] = []
    try:
        for path in sorted(search_root.glob(pattern)):
            resolved = path.resolve()
            if not _is_within_path(resolved, ws):
                continue
            matches.append(resolved.relative_to(ws).as_posix())
            if len(matches) >= 100:
                break
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Glob search failed: {e}", retryable=True)

    if not matches:
        return f"🔎 No files matched pattern '{pattern}'"
    lines = [f"🔎 Glob results for '{pattern}' ({len(matches)} match(es)):"]
    lines.extend(f"- {match}" for match in matches)
    return "\n".join(lines)


def _grep_search(ws: Path, pattern: str, root: str = "", max_results: int = 50, tool_name: str = "grep_search") -> str:
    search_root = (ws / root).resolve() if root else ws.resolve()
    if not _is_within_path(search_root, ws):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if not search_root.exists():
        return _workspace_error(tool_name, "not_found", f"Directory not found: {root or '/'}")

    max_results = max(1, min(int(max_results), 200))
    matches: list[str] = []

    if shutil.which("rg"):
        try:
            proc = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--color",
                    "never",
                    "--max-count",
                    str(max_results),
                    pattern,
                    str(search_root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.stdout.strip():
                for line in proc.stdout.splitlines():
                    normalized = line.replace(str(ws.resolve()) + os.sep, "")
                    matches.append(normalized)
            elif proc.returncode not in (0, 1):
                return _workspace_error(
                    tool_name,
                    "operation_failed",
                    f"Grep search failed: {proc.stderr.strip()[:200]}",
                    retryable=True,
                )
        except Exception as e:
            return _workspace_error(tool_name, "operation_failed", f"Grep search failed: {e}", retryable=True)
    else:
        try:
            compiled = re.compile(pattern)
        except re.error as regex_err:
            return _workspace_error(
                tool_name,
                "bad_arguments",
                f"Invalid regex pattern: {regex_err}",
            )
        try:
            for path in sorted(search_root.rglob("*")):
                if len(matches) >= max_results:
                    break
                if not path.is_file():
                    continue
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for idx, line in enumerate(handle, start=1):
                            if compiled.search(line):
                                matches.append(f"{path.relative_to(ws).as_posix()}:{idx}:{line.strip()}")
                                if len(matches) >= max_results:
                                    break
                except Exception as _read_err:
                    logger.debug("[Workspace] grep: skipped file %s: %s", path, _read_err)
                    continue
        except Exception as e:
            return _workspace_error(tool_name, "operation_failed", f"Grep search failed: {e}", retryable=True)

    if not matches:
        return f"🔎 No matches for '{pattern}'"
    lines = [f"🔎 Grep results for '{pattern}' ({len(matches)} match(es)):"]
    lines.extend(f"- {match}" for match in matches[:max_results])
    return "\n".join(lines)


async def _tool_search(ws: Path, query: str = "", agent_id: uuid.UUID | str | None = None) -> str:
    from app.services.agent_tools import discoverable_tool_names_for_query
    from app.services.tool_search_manifest import build_tool_search_manifest, render_tool_search_manifest_sections

    registry = _build_skill_registry(ws)
    normalized = query.strip().lower()
    matching_skills = [
        skill
        for skill in (registry.resolve(name) for name in registry.names())
        if not normalized
        or normalized in skill.metadata.name.lower()
        or normalized in skill.metadata.description.lower()
        or any(normalized in tool.lower() for tool in skill.metadata.declared_tools)
    ]

    # Step 3: single source of truth — the deferred tool names listed here are
    # EXACTLY what the schema path loads. discoverable_tool_names_for_query covers
    # static packs + agent-reachable MCP and excludes CORE (already turn-1). Text
    # == schema (🦴#2). agent_id falls back to the workspace dir name.
    deferred_names: list[str] = []
    resolved_agent_id = agent_id if agent_id is not None else ws.name
    if resolved_agent_id:
        try:
            deferred_names = await discoverable_tool_names_for_query(uuid.UUID(str(resolved_agent_id)), query)
        except Exception:
            deferred_names = []
    subagent_rows: list[dict] = []
    if resolved_agent_id:
        try:
            from app.agents.subagent_definition import list_subagent_definitions

            subagent_rows = list_subagent_definitions(agent_id=resolved_agent_id, tenant_id=None)
        except Exception:
            subagent_rows = []

    manifest = build_tool_search_manifest(
        query=query,
        loaded_tool_names=deferred_names,
        skills=matching_skills,
        subagents=subagent_rows,
    )

    lines = [
        "Tool search discovered deferred capabilities. Matching deferred tool schemas become callable in this session.",
        "",
        render_tool_search_manifest_sections(manifest),
    ]
    if deferred_names:
        lines.append("")
        lines.append(
            "Discovered deferred tools (matching schemas are loaded by this search; "
            "use `load_skill` only for method instructions):"
        )
        lines.append(f"- {', '.join(deferred_names)}")
    if matching_skills:
        lines.append("")
        lines.append("Matching skills:")
        for skill in matching_skills[:20]:
            declared = (
                ", ".join(skill.metadata.declared_tools) if skill.metadata.declared_tools else "no declared tools"
            )
            lines.append(f"- {skill.metadata.name}: {skill.metadata.description} | declared tools: {declared}")
    if len(lines) == 1:
        return f"🔎 No delayed tools or skills matched '{query}'"
    return "\n".join(lines)


def _delete_file(ws: Path, rel_path: str, tool_name: str = "delete_file") -> str:
    protected = {"tasks.json", "soul.md"}
    if rel_path.strip("/") in protected:
        return _workspace_error(tool_name, "auth_or_permission", f"{rel_path} cannot be deleted (protected)")

    managed_message = _managed_system_path_message(rel_path)
    if managed_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            managed_message,
            actionable_hint=(
                "Memory entries must be corrected through governed memory workflows."
                if _is_governed_memory_path(rel_path)
                else "Only delete normal work artifacts under workspace/."
            ),
        )

    root_guard_message = _root_write_guard_message(rel_path)
    if root_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            root_guard_message,
            actionable_hint="Only delete normal work artifacts under workspace/.",
        )

    skill_guard_message = _skill_package_path_guard_message(rel_path, operation="delete")
    if skill_guard_message:
        return _workspace_error(
            tool_name,
            "auth_or_permission",
            skill_guard_message,
            actionable_hint="Use skill lifecycle controls such as pin/unpin or governed promotion review; do not delete active skills directly.",
        )

    file_path = (ws / rel_path).resolve()
    if not _is_within_path(file_path, ws):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if not file_path.exists():
        return _workspace_error(tool_name, "not_found", f"File not found: {rel_path}")

    try:
        if file_path.is_dir():
            shutil.rmtree(file_path)
            return f"✅ Deleted directory {rel_path}"
        file_path.unlink()
        return f"✅ Deleted {rel_path}"
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Delete failed: {e}")
