from __future__ import annotations

import logging
import io
import os
import hashlib
import re
import shutil
import subprocess
import uuid
import json
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import get_settings
from app.services.chat_artifact_delivery import is_chat_artifact_snapshot_storage_path
from app.services.legacy_company_files import COMPANY_CONTEXT_FILENAMES, company_context_path_allowed
from app.services.managed_capability_guard import sanitize_managed_credential_guidance
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityError,
    WorkspaceAuthorityScope,
    authorize_workspace_tool_path,
    delete_workspace_path,
    is_recovery_manifest_storage_path,
    resolve_workspace_storage_path,
    open_workspace_file,
    write_workspace_file,
    workspace_glob_read_observation,
)
from app.skills import SkillRegistry, WorkspaceSkillLoader
from app.runtime.ccplus_contracts import permission_profile_snapshot
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


def _file_read_failure(tool_name, error_class, message, *, path, typed_errors, **options):
    text = _workspace_error(tool_name, error_class, message, **options)
    if not typed_errors:
        return text
    metadata = {"filesystem_read_error": True}
    if error_class == "not_found" and message == f"File not found: {path}":
        metadata["native_read_denial"] = {"kind": "filesystem_read_not_found", "path": path}
    elif error_class == "auth_or_permission":
        boundary = {"Access denied for this resource owner.": "resource_owner",
            "Access denied for this path.": "path",
            "Only generated company context is available.": "company_context"}.get(message)
        if boundary is not None:
            metadata["native_read_denial"] = {"kind": "filesystem_read_denied", "path": path, "boundary": boundary}
    return ToolContentEnvelope(text=text, metadata=metadata)


def _workspace_storage_error(tool_name: str, exc: WorkspaceAuthorityError, *, mutation: bool = False) -> str:
    conflict = exc.code == "workspace_predecessor_changed"
    status = 503 if exc.code == "workspace_storage_unavailable" else 409 if conflict or exc.code == "workspace_resource_changed" else 403
    return render_tool_error(
        tool_name=tool_name,
        error_class="auth_or_permission" if status == 403 else "provider_unavailable" if status == 503 else "operation_failed",
        message=exc.message,
        provider="workspace",
        http_status=status,
        retryable=status != 403 and not mutation,
        actionable_hint="Use an independent canonical file; refresh and reconcile the path before retrying.",
        extra={"reason_code": exc.code, "outcome": "failed" if conflict else "denied" if status == 403 else "unknown" if mutation else "unavailable" if status == 503 else "failed"},
    )


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _resolved_workspace_path(ws: Path, rel_path: str, permission_profile: Any | None = None) -> Path | None:
    """Resolve a workspace path and enforce an exact session root at the final I/O boundary."""

    if is_chat_artifact_snapshot_storage_path(rel_path):
        return None
    workspace = ws.resolve()
    try:
        target = resolve_workspace_storage_path(ws, rel_path)
    except WorkspaceAuthorityError:
        return None
    profile = permission_profile_snapshot(permission_profile)
    snapshot = profile.get("capability_policy_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("session_exact_scope") is not True:
        return target
    raw_path = str(rel_path or "")
    scoped_path = PurePosixPath(raw_path)
    if (
        not raw_path
        or "\\" in raw_path
        or scoped_path.is_absolute()
        or any(part in {"", ".", ".."} for part in scoped_path.parts)
        or scoped_path.as_posix() != raw_path
    ):
        return None
    writable_roots = profile.get("writable_roots")
    if (
        not isinstance(writable_roots, list)
        or len(writable_roots) != 1
        or profile.get("readable_roots") != writable_roots
    ):
        return None
    raw_root = str(writable_roots[0] or "")
    root = PurePosixPath(raw_root)
    if (
        not raw_root
        or "\\" in raw_root
        or root.is_absolute()
        or len(root.parts) < 2
        or root.parts[0] != "workspace"
        or any(part in {"", ".", ".."} for part in root.parts)
        or root.as_posix() != raw_root
    ):
        return None
    logical_root = workspace.joinpath(*root.parts)
    resolved_root = logical_root.resolve()
    if resolved_root != logical_root or not _is_within_path(target, resolved_root):
        return None
    return target


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


def _authority_allows_path(authority_scope, rel_path: str, *, directory: bool = False, action: str = "read") -> bool:
    if is_recovery_manifest_storage_path(rel_path) or is_chat_artifact_snapshot_storage_path(rel_path):
        return False
    if authority_scope is None:
        return True
    if action != "read":
        return authority_scope.can_perform(rel_path, action)
    normalized = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not (normalized == "workspace" or normalized.startswith("workspace/")):
        return authority_scope.can_read(normalized)
    if directory:
        return authority_scope.can_read(normalized)
    return normalized in authority_scope.allowed_paths or authority_scope.operator_view


def _authority_known_foreign_path(authority_scope, rel_path: str) -> bool:
    """Known-but-unreadable workspace manifests deny BEFORE any existence probe.

    Native session sources also deny from the trusted database scope first.
    Mirrors ``authorize_workspace_tool_path``: the trusted manifest registry
    (``known_paths``) decides foreign ownership first, so filesystem existence
    never becomes an oracle distinguishing a foreign manifest that happens to be
    absent from one that is present. Unknown new paths and owned paths fall
    through to the honest existence check.
    """
    if authority_scope is None:
        return False
    normalized = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if authority_scope.can_read_session_source(normalized) is False:
        return True
    if authority_scope.can_read_learning_source(normalized) is False:
        return True
    if not normalized.startswith("workspace/"):
        return False
    return normalized in authority_scope.known_paths and not authority_scope.can_read(normalized)


def _authorize_workspace_mutation_path(
    ws: Path,
    rel_path: str,
    *,
    authority_scope,
    action: str,
    tool_name: str,
) -> tuple[str | None, str | None]:
    try:
        normalized = authorize_workspace_tool_path(
            ws,
            authority_scope,
            rel_path,
            action=action,
        )
    except WorkspaceAuthorityError as exc:
        return None, _workspace_error(
            tool_name,
            "auth_or_permission",
            exc.message,
            actionable_hint="Use a canonical workspace-relative path and do not use `..` segments.",
        )
    return normalized, None


def _list_files(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    tool_name: str = "list_files",
    authority_scope=None,
) -> str:
    if _authority_known_foreign_path(authority_scope, rel_path):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    enterprise_context = bool(rel_path and rel_path.startswith("enterprise_info"))
    if enterprise_context:
        if not company_context_path_allowed(rel_path, directory=True):
            return _workspace_error(tool_name, "auth_or_permission", "Only generated company context is available.")
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        target = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(target, enterprise_root):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        target = _resolved_workspace_path(ws, rel_path)
        if target is None:
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
        rel_path = target.relative_to(ws.resolve()).as_posix() if target != ws.resolve() else ""
        if _authority_known_foreign_path(authority_scope, rel_path):
            return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    if not target.exists():
        return _workspace_error(
            tool_name,
            "not_found",
            f"Directory not found: {rel_path or '/'}",
            actionable_hint="Check the directory path and list the parent directory first if needed.",
        )
    if not _authority_allows_path(authority_scope, rel_path, directory=True):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

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

    def visible(p):
        if p.name.startswith("."):
            return False
        if enterprise_context and p.name not in COMPANY_CONTEXT_FILENAMES:
            return False
        if not enterprise_context:
            relative = p.relative_to(ws.resolve()).as_posix()
            resolved = _resolved_workspace_path(ws, relative)
            if resolved is None or not _authority_allows_path(authority_scope,
                resolved.relative_to(ws.resolve()).as_posix(), directory=p.is_dir()):
                return False
            parent = p.parent.relative_to(ws.resolve()).as_posix()
        else:
            parent = rel_path
        return authority_scope is None or authority_scope.visible_child(parent, p.name, is_dir=p.is_dir())

    for p in sorted(target.iterdir()):
        if not visible(p):
            continue
        if p.is_dir():
            dir_count += 1
            child_count = sum(1 for c in p.iterdir() if visible(c))
            items.append(f"  📁 {p.name}/ ({child_count} items)")
        elif p.is_file():
            file_count += 1
            size_bytes = p.stat().st_size
            size_str = f"{size_bytes}B" if size_bytes < 1024 else f"{size_bytes / 1024:.1f}KB"
            items.append(f"  📄 {p.name} ({size_str})")

    if not items:
        result = _append_workspace_provenance_hint(
            f"📂 {rel_path or 'root'}: Empty directory (0 files, 0 folders)",
            ws,
            rel_path,
            directory=True,
        )
        if authority_scope is not None and not enterprise_context and "\n" not in result:
            return ToolContentEnvelope(text=result, metadata={"native_empty_read": {
                "kind": "authorized_directory_empty", "path": rel_path,
            }})
        return result

    header = f"📂 {rel_path or 'root'}: {dir_count} folder(s), {file_count} file(s)\n"
    listing = header + "\n".join(items)
    result = _append_workspace_provenance_hint(listing, ws, rel_path, directory=True)
    if (authority_scope is not None and (rel_path == "workspace" or rel_path.startswith("workspace/"))
        and result == listing):
        # Names, sizes and counts are a native observation, never a receipt for file bodies.
        return ToolContentEnvelope(text=result, metadata={"native_directory_read": {
            "kind": "authorized_workspace_directory_listing", "path": rel_path,
        }})
    return result


def _read_receipted_workspace_bytes(read_root: Path, read_path: str, authority_scope, authority_path: str) -> bytes:
    with open_workspace_file(read_root, read_path) as source:
        raw = source.read()
    read = authority_scope.input_read(authority_path) if hasattr(authority_scope, "input_read") else None
    if read is not None:
        read.verify(raw)
    learned = next((digest for path, _revision, digest in getattr(authority_scope, "learning_files", ())
        if path == authority_path), None)
    if learned is not None and hashlib.sha256(raw).hexdigest() != learned:
        raise WorkspaceAuthorityError("learned_skill_bytes_changed", "The learned source bytes changed after authority was checked.")
    capture = getattr(authority_scope, "skill_inputs", None)
    if capture is not None:
        capture.observe_file(authority_path, hashlib.sha256(raw).hexdigest())
    return raw


def _read_file(
    ws: Path,
    rel_path: str,
    tenant_id: str | None = None,
    tool_name: str = "read_file",
    authority_scope=None,
    permission_profile=None,
    typed_errors: bool = False,
) -> "str | ToolContentEnvelope":
    def _typed_failure(value):
        return ToolContentEnvelope(text=value, metadata={"filesystem_read_error": True}) if typed_errors else value

    def _failure(*args, **kwargs):
        return _file_read_failure(*args, path=rel_path, typed_errors=typed_errors, **kwargs)

    # Storage reserved for authority-bound recovery refs is denied regardless of
    # existence; every other workspace read decides KNOWN FOREIGN OWNERSHIP from
    # the trusted manifest registry before touching the filesystem, then checks
    # existence, then the owner decision: a foreign manifest denies whether or
    # not it is on disk (no existence oracle), while a not-yet-created file the
    # requester may own (e.g. a fresh Plan Mode plan file) is an honest
    # ``not_found``. An EXISTING file the requester cannot read stays denied.
    if is_recovery_manifest_storage_path(rel_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if _authority_known_foreign_path(authority_scope, rel_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    profile = permission_profile_snapshot(permission_profile)
    profile_snapshot = profile.get("capability_policy_snapshot")
    exact_scope = isinstance(profile_snapshot, dict) and profile_snapshot.get("session_exact_scope") is True
    read_root, read_path = ws, rel_path
    if exact_scope:
        file_path = _resolved_workspace_path(ws, rel_path, permission_profile)
        if file_path is None:
            return _failure(tool_name, "auth_or_permission", "Access denied for this path.")
    elif rel_path and rel_path.startswith("enterprise_info"):
        if not company_context_path_allowed(rel_path):
            return _failure(tool_name, "auth_or_permission", "Only generated company context is available.")
        if tenant_id:
            enterprise_root = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
        else:
            enterprise_root = WORKSPACE_ROOT / "enterprise_info"
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        read_root, read_path = enterprise_root, sub
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(file_path, enterprise_root):
            return _failure(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        file_path = _resolved_workspace_path(ws, rel_path, permission_profile)
        if file_path is None:
            return _failure(tool_name, "auth_or_permission", "Access denied for this path.")

    authority_path = file_path.relative_to(ws.resolve()).as_posix() if _is_within_path(file_path, ws) else rel_path
    if _authority_known_foreign_path(authority_scope, authority_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if not file_path.exists():
        return _failure(
            tool_name,
            "not_found",
            f"File not found: {rel_path}",
            actionable_hint="Check the path or use glob_search/list_files to discover the correct file first.",
        )
    if not _authority_allows_path(authority_scope, authority_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    # Image files → typed image block (CC Read parity): vision-capable models see
    # the image natively; the text fallback names the file for text-only providers.
    image_media = _IMAGE_MEDIA_TYPES.get(file_path.suffix.lower())
    if image_media:
        try:
            import base64

            raw = _read_receipted_workspace_bytes(read_root, read_path, authority_scope, rel_path)
            if len(raw) > _MAX_IMAGE_BYTES:
                return _failure(
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
        except WorkspaceAuthorityError as exc:
            return _typed_failure(_workspace_storage_error(tool_name, exc))
        except Exception as e:
            return _failure(tool_name, "operation_failed", f"Image read failed: {e}")

    try:
        raw = _read_receipted_workspace_bytes(read_root, read_path, authority_scope, rel_path)
        with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace") as reader:
            content = reader.read()
        if _is_skill_instruction_file(ws, file_path):
            content = sanitize_managed_credential_guidance(content)
        return _append_workspace_provenance_hint(content, ws, rel_path, directory=False)
    except WorkspaceAuthorityError as exc:
        return _typed_failure(_workspace_storage_error(tool_name, exc))
    except Exception as e:
        return _failure(tool_name, "operation_failed", f"Read failed: {e}")


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


def _skill_control_error(authority_scope, tool_name, error_class, message):
    capture = getattr(authority_scope, "skill_inputs", None)
    if capture is not None:
        capture.control_only = True
    return _workspace_error(tool_name, error_class, message)


def _load_skill(ws: Path, skill_name: str, tool_name: str = "load_skill", *, session_id: str | None = None, authority_scope=None) -> str:
    requested = (skill_name or "").strip()
    if not requested:
        return _skill_control_error(authority_scope, tool_name, "bad_arguments", "Skill name cannot be empty.")

    workspace_root = ws.resolve()
    skills_dir = (ws / "skills").resolve()

    from app.services.skill_evolution_registry import LOADABLE_STATES, skill_runtime_state

    def _apply_trial_state(content: str, identity: str) -> str:
        state = skill_runtime_state(ws, identity)
        if state not in LOADABLE_STATES:
            return _workspace_error(
                tool_name,
                "skill_state_blocked",
                f"Skill cannot be loaded while its governed state is {state}.",
            )
        if state == "provisional":
            return (
                content
                + "\n\n---\n"
                + "**Provisional trial:** this Skill version is active under monitored trial. "
                + "Use it normally; runtime evidence may promote or roll it back automatically."
            )
        return content

    def _read_skill_file(path: Path) -> str:
        if (
            not _is_within_path(path, workspace_root)
            or not _is_skill_instruction_file(ws, path)
            or not _session_extension_path_allowed(ws, path, session_id=session_id)
        ):
            return _skill_control_error(authority_scope, tool_name, "auth_or_permission", "Access denied for this skill path.")
        try:
            relative_path = path.relative_to(workspace_root).as_posix()
            if not _authority_allows_path(authority_scope, relative_path):
                return _skill_control_error(authority_scope, tool_name, "auth_or_permission", "Access denied for this skill source.")
            raw = _read_receipted_workspace_bytes(workspace_root, relative_path, authority_scope, relative_path)
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace") as reader:
                content = reader.read()
            return _apply_trial_state(sanitize_managed_credential_guidance(content), relative_path)
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
            if "skill_state_blocked" in body:
                return body
            try:
                rel = explicit_path.relative_to(ws).as_posix()
                slug = _skill_slug_from_relative_path(rel)
                if slug:
                    from app.services.skill_curator import bump_skill_use

                    bump_skill_use(ws, slug, kind="use")
            except Exception as exc:  # pragma: no cover - telemetry must never break load
                logger.debug("[workspace] curator use bump (explicit) failed: %s", exc)
            return body

    registry = _build_skill_registry(ws, session_id=session_id, source_authorizer=lambda path:
        _authority_allows_path(authority_scope, Path(path).resolve().relative_to(workspace_root).as_posix()))
    try:
        # Step 9: resolve once so allowed-tools can be re-surfaced as scoped tool
        # guidance. The registry path strips frontmatter (where allowed-tools
        # lives), unlike the explicit path which returns the raw file. A missing
        # skill raises KeyError and falls through to the tool-pack check below.
        skill = registry.resolve(requested)
        relative_path = skill.file_path.resolve().relative_to(workspace_root).as_posix()
        if not _authority_allows_path(authority_scope, relative_path):
            return _skill_control_error(authority_scope, tool_name, "auth_or_permission", "Access denied for this skill source.")
        raw = _read_receipted_workspace_bytes(workspace_root, relative_path, authority_scope, relative_path)
        from app.skills.parser import SkillParser

        with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace") as reader:
            skill = SkillParser().parse_content(reader.read(), path=skill.file_path, relative_path=skill.relative_path,
                default_name=skill.file_path.parent.name if skill.file_path.name.lower() == "skill.md" else skill.file_path.stem)
        body = sanitize_managed_credential_guidance(skill.body)
        body += _skill_scope_guidance(skill.metadata)
        body = _apply_trial_state(body, skill.relative_path or requested)
        if "skill_state_blocked" in body:
            return body
        _curator_bump_use(ws, skill.relative_path)
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

    return _skill_control_error(authority_scope, tool_name, "not_found", f"Skill not found: {skill_name}")


def _build_skill_registry(ws: Path, *, session_id: str | None = None, source_authorizer=None) -> SkillRegistry:
    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(ws, session_id=session_id, source_authorizer=source_authorizer))
    return registry


def _session_extension_path_allowed(ws: Path, path: Path, *, session_id: str | None = None) -> bool:
    try:
        parts = path.resolve().relative_to(ws.resolve()).parts
    except ValueError:
        return False
    if "session_extensions" not in parts:
        return True
    index = parts.index("session_extensions")
    return bool(session_id and len(parts) > index + 1 and parts[index + 1] == str(session_id))


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
        if skill_name.startswith("skills/"):
            slug = _skill_slug_from_relative_path(skill_name)
        else:
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
    source_authorizer=None,
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
        registry = _build_skill_registry(ws, source_authorizer=source_authorizer)
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
    source_authorizer=None,
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
        if source_authorizer is not None and not source_authorizer(manifest_path):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        draft_path_raw = manifest.get("draft_path") or manifest.get("candidate_signal_path")
        draft_text = ""
        if draft_path_raw:
            draft_path = (ws / str(draft_path_raw)).resolve()
            if (_is_within_path(draft_path, ws) and draft_path.exists()
                and (source_authorizer is None or source_authorizer(draft_path))):
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
    candidate_id: str | None = None,
    source_authorizer=None,
    prepared_markdown: str | None = None,
    outcome_observer=None,
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
    semantic_observations: list[dict[str, Any]] = []
    if not overwrite:
        similar = _find_similar_existing_skill(ws, name=skill_name, description=skill_description,
            **({"source_authorizer": source_authorizer} if source_authorizer is not None else {}))
        if similar is not None:
            sim_skill, sim_score = similar
            semantic_observations.append(
                {
                    "kind": "similar_existing_skill",
                    "similarity": sim_score,
                    "skill_name": sim_skill.metadata.name,
                    "relative_path": sim_skill.relative_path,
                    "description": sim_skill.metadata.description,
                    "authority": "observation_only",
                }
            )
        similar_candidate = _find_similar_pending_skill_candidate(
            ws,
            name=skill_name,
            description=skill_description,
            **({"source_authorizer": source_authorizer} if source_authorizer is not None else {}),
        )
        if similar_candidate is not None:
            manifest, sim_score = similar_candidate
            semantic_observations.append(
                {
                    "kind": "similar_pending_skill_candidate",
                    "similarity": sim_score,
                    "candidate_id": manifest.get("candidate_id"),
                    "skill_name": manifest.get("skill_name"),
                    "draft_path": manifest.get("draft_path") or manifest.get("candidate_signal_path"),
                    "authority": "observation_only",
                }
            )

    rendered = prepared_markdown if prepared_markdown is not None else _render_skill_markdown(
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

    candidate_id = candidate_id or f"save-skill-{uuid.uuid4()}"
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.services.skill_candidate_package import write_skill_candidate_package
    from app.services.skill_evolution_registry import ORIGIN_USER_SKILL_CREATOR, upsert_skill_evolution_entry
    from app.services.evolution_ledger import record_evolution_candidate
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    try:
        with AgentAssetTransaction(
            ws,
            operation="save_skill_candidate_submission",
            idempotency_key=f"save-skill:{candidate_id}",
            evidence_refs=("tool:save_skill",),
        ) as transaction:
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
                    "semantic_observations": semantic_observations,
                },
                transaction=transaction,
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
                metadata={
                    "draft_path": manifest["draft_path"],
                    "source_tool": tool_name,
                    "semantic_observations": semantic_observations,
                },
                transaction=transaction,
            )
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
                    "semantic_observations": semantic_observations,
                },
                transaction=transaction,
            )
            record_skill_lifecycle_event(
                ws,
                skill_name=skill_name,
                status="candidate",
                note=(
                    f"Submitted via save_skill for external behavior verification at {manifest['draft_path']}; "
                    f"target={target_rel}"
                ),
                transaction=transaction,
            )
            transaction.commit()
    except Exception as exc:
        logger.exception("[workspace] Failed atomic skill candidate submission for %s", skill_name)
        return _workspace_error(
            tool_name,
            "skill_candidate_commit_failed",
            f"Skill candidate transaction failed: {type(exc).__name__}: {exc}",
            actionable_hint="Retry the same save_skill request; no partial candidate or registry entry was activated.",
        )

    if outcome_observer is not None:
        outcome_observer({"candidate_id": candidate_id, "draft_path": manifest["draft_path"]})
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
    max_chars: int | None = None,
    tenant_id: str | None = None,
    tool_name: str = "read_document",
    mode: str = "auto",
    max_pages: int | None = None,
    force_refresh: bool = False,
    return_format: str = "markdown",
    authority_scope=None,
    typed_errors: bool = False,
) -> "str | ToolContentEnvelope":
    def _typed_failure(value):
        return ToolContentEnvelope(text=value, metadata={"filesystem_read_error": True}) if typed_errors else value

    def _failure(*args, **kwargs):
        return _file_read_failure(*args, path=rel_path, typed_errors=typed_errors, **kwargs)

    # Same ordering as _read_file: known foreign ownership denies before any
    # existence probe; a not-yet-created workspace path is ``not_found``; an
    # existing unreadable file stays denied.
    if is_recovery_manifest_storage_path(rel_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if _authority_known_foreign_path(authority_scope, rel_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    workspace_root = ws
    if rel_path and rel_path.startswith("enterprise_info"):
        if not company_context_path_allowed(rel_path):
            return _failure(tool_name, "auth_or_permission", "Only generated company context is available.")
        if tenant_id:
            enterprise_root = (WORKSPACE_ROOT / f"enterprise_info_{tenant_id}").resolve()
        else:
            enterprise_root = (WORKSPACE_ROOT / "enterprise_info").resolve()
        workspace_root = enterprise_root
        sub = rel_path[len("enterprise_info") :].lstrip("/")
        file_path = (enterprise_root / sub).resolve() if sub else enterprise_root
        if not _is_within_path(file_path, enterprise_root):
            return _failure(tool_name, "auth_or_permission", "Access denied for this path.")
    else:
        file_path = _resolved_workspace_path(ws, rel_path)
        if file_path is None:
            return _failure(tool_name, "auth_or_permission", "Access denied for this path.")

    authority_path = file_path.relative_to(ws.resolve()).as_posix() if _is_within_path(file_path, ws) else rel_path
    if _authority_known_foreign_path(authority_scope, authority_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if not file_path.exists():
        return _failure(tool_name, "not_found", f"File not found: {rel_path}")
    if not _authority_allows_path(authority_scope, authority_path):
        return _failure(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    try:
        from app.services.document_conversion import (
            DocumentConversionRequest,
            DocumentConversionService,
            render_conversion_preview,
        )
        input_read = authority_scope.input_read(rel_path) if hasattr(authority_scope, "input_read") else None

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
                max_output_chars=None,
                force_refresh=force_refresh,
                expected_source_sha256=input_read.content_sha256 if input_read is not None else None,
                input_read=input_read,
            )
        )
        if return_format == "markdown":
            return result.markdown
        if return_format == "metadata":
            if getattr(result, "metadata_json", None) is not None:
                return json.dumps(result.metadata_json, ensure_ascii=False, indent=2)
            metadata_path = workspace_root / result.artifact_metadata_path
            return metadata_path.read_text(encoding="utf-8", errors="replace")
        if return_format == "pages":
            return (
                "Page-level Markdown artifacts are not available yet.\n"
                f"Full Markdown: {result.artifact_markdown_path}\n"
                f"Metadata: {result.artifact_metadata_path}"
            )
        return render_conversion_preview(result, max_chars=max_chars)
    except WorkspaceAuthorityError as exc:
        return _typed_failure(_workspace_storage_error(tool_name, exc))
    except ImportError as e:
        return _failure(
            tool_name,
            "dependency_missing",
            f"Missing dependency: {e}. Install the document conversion backend dependencies.",
            actionable_hint="Install the required document parsing dependency in the backend environment.",
        )
    except ValueError as e:
        return _failure(tool_name, "bad_arguments", str(e))
    except Exception as e:
        return _failure(tool_name, "operation_failed", f"Document read failed: {str(e)}")


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
        "enterprise_info/ is generated read-only company context; only company_profile.md and org_structure.md are exposed."
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


def _write_file(
    ws: Path,
    rel_path: str,
    content: str,
    tool_name: str = "write_file",
    authority_scope=None,
    permission_profile=None,
) -> str:
    if not rel_path or not rel_path.strip("/"):
        return _workspace_error(
            tool_name,
            "bad_arguments",
            "Missing file path.",
            actionable_hint="Pass a workspace-relative file path such as workspace/report.md.",
        )

    normalized_path, authority_error = _authorize_workspace_mutation_path(
        ws,
        rel_path,
        authority_scope=authority_scope,
        action="write",
        tool_name=tool_name,
    )
    if authority_error:
        return authority_error
    assert normalized_path is not None
    rel_path = normalized_path

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

    file_path = _resolved_workspace_path(ws, rel_path, permission_profile)
    if file_path is None:
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if file_path.exists() and not _authority_allows_path(authority_scope, rel_path, action="write"):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    try:
        observation = authority_scope.output_write(rel_path) if isinstance(authority_scope, WorkspaceAuthorityScope) else None
        write_workspace_file(ws, rel_path, content.encode("utf-8"), observation=observation)
        return f"✅ Written to {rel_path} ({len(content)} chars)"
    except WorkspaceAuthorityError as exc:
        return _workspace_storage_error(tool_name, exc, mutation=True)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Write failed: {e}")


def _edit_file(
    ws: Path,
    rel_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    tool_name: str = "edit_file",
    authority_scope=None,
    permission_profile=None,
) -> str:
    normalized_path, authority_error = _authorize_workspace_mutation_path(
        ws,
        rel_path,
        authority_scope=authority_scope,
        action="write",
        tool_name=tool_name,
    )
    if authority_error:
        return authority_error
    assert normalized_path is not None
    rel_path = normalized_path

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
    file_path = _resolved_workspace_path(ws, rel_path, permission_profile)
    if file_path is None:
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if not file_path.exists():
        return _workspace_error(tool_name, "not_found", f"File not found: {rel_path}")
    if not _authority_allows_path(authority_scope, rel_path):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    try:
        observation = authority_scope.output_write(rel_path) if isinstance(authority_scope, WorkspaceAuthorityScope) else None
        with open_workspace_file(ws, rel_path, mode="r+b", write_observation=observation) as source:
            raw = source.read()
            if observation is not None:
                observation.verify_predecessor(raw)
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace") as text:
                original = text.read()
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
            output = updated.replace("\n", os.linesep).encode("utf-8")
            if observation is not None:
                observation.expected_output_sha256 = hashlib.sha256(output).hexdigest()
            source.seek(0)
            source.write(output)
            source.truncate()
        replaced = occurrences if replace_all else 1
        return f"✅ Updated {rel_path} ({replaced} replacement{'s' if replaced != 1 else ''})"
    except WorkspaceAuthorityError as exc:
        return _workspace_storage_error(tool_name, exc, mutation=True)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Edit failed: {e}")


def _glob_search(
    ws: Path,
    pattern: str,
    root: str = "",
    tool_name: str = "glob_search",
    authority_scope=None,
    permission_profile=None,
) -> str:
    if _authority_known_foreign_path(authority_scope, root):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    search_root = _resolved_workspace_path(ws, root, permission_profile)
    if search_root is None:
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if _authority_known_foreign_path(authority_scope, search_root.relative_to(ws.resolve()).as_posix()):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if not search_root.exists():
        return _workspace_error(tool_name, "not_found", f"Directory not found: {root or '/'}")
    if not _authority_allows_path(authority_scope, search_root.relative_to(ws.resolve()).as_posix(), directory=True):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    matches: list[str] = []
    try:
        for path in sorted(search_root.glob(pattern)):
            relative = path.relative_to(ws.resolve()).as_posix()
            resolved = _resolved_workspace_path(ws, relative, permission_profile)
            if resolved is None:
                continue
            rel = resolved.relative_to(ws.resolve()).as_posix()
            if not _authority_allows_path(authority_scope, rel, directory=resolved.is_dir()):
                continue
            matches.append(rel)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Glob search failed: {e}", retryable=True)

    if not matches:
        result = f"🔎 No files matched pattern '{pattern}'"
    else:
        lines = [f"🔎 Glob results for '{pattern}' ({len(matches)} match(es)):"]
        lines.extend(f"- {match}" for match in matches)
        result = "\n".join(lines)
    observation = workspace_glob_read_observation(root, pattern)
    resolved_root = search_root.relative_to(ws.resolve()).as_posix()
    if (authority_scope is not None and observation is not None
        and (resolved_root == "." or resolved_root == "workspace" or resolved_root.startswith("workspace/"))
        and all(match == "workspace" or match.startswith("workspace/") for match in matches)):
        return ToolContentEnvelope(result, metadata={"native_directory_read": observation})
    return result


def _grep_search(
    ws: Path,
    pattern: str,
    root: str = "",
    max_results: int | None = None,
    tool_name: str = "grep_search",
    authority_scope=None,
    permission_profile=None,
) -> str:
    if _authority_known_foreign_path(authority_scope, root):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    search_root = _resolved_workspace_path(ws, root, permission_profile)
    if search_root is None:
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this path.")
    if _authority_known_foreign_path(authority_scope, search_root.relative_to(ws.resolve()).as_posix()):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")
    if not search_root.exists():
        return _workspace_error(tool_name, "not_found", f"Directory not found: {root or '/'}")
    if not _authority_allows_path(authority_scope, search_root.relative_to(ws.resolve()).as_posix(), directory=True):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    explicit_limit = max(1, int(max_results)) if max_results is not None else None
    matches: list[str] = []

    native_rg = shutil.which("rg")
    compiled = None
    if not native_rg:
        try:
            compiled = re.compile(pattern)
        except re.error as regex_err:
            return _workspace_error(
                tool_name,
                "bad_arguments",
                f"Invalid regex pattern: {regex_err}",
            )
    try:
        candidates = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))
        paths = []
        for path in candidates:
            if not path.is_file():
                continue
            rel_path = path.relative_to(ws.resolve()).as_posix()
            resolved = _resolved_workspace_path(ws, rel_path, permission_profile)
            if resolved is not None and _authority_allows_path(authority_scope, resolved.relative_to(ws.resolve()).as_posix()):
                paths.append(rel_path)
        if native_rg:
            # Bound open descriptors, never the authorized search coverage.
            for offset in range(0, len(paths), 32):
                if explicit_limit is not None and len(matches) >= explicit_limit:
                    break
                with ExitStack() as inputs:
                    descriptors = {}
                    for rel_path in paths[offset : offset + 32]:
                        source = inputs.enter_context(open_workspace_file(ws, rel_path))
                        descriptors[f"/dev/fd/{source.fileno()}"] = (rel_path, source.fileno())
                    command = ["rg", "--with-filename", "--line-number", "--color", "never"]
                    if explicit_limit is not None:
                        command.extend(("--max-count", str(explicit_limit)))
                    # The model-authored pattern remains data, never an rg option.
                    command.extend(("--", pattern, *descriptors))
                    proc = subprocess.run(command, pass_fds=tuple(fd for _, fd in descriptors.values()),
                        capture_output=True, text=True, check=False)
                if proc.returncode not in (0, 1):
                    return _workspace_error(tool_name, "operation_failed", f"Grep search failed: {proc.stderr.strip()}", retryable=True)
                batch_matches = []
                for line in proc.stdout.splitlines():
                    descriptor, separator, match = line.partition(":")
                    if descriptor in descriptors:
                        batch_matches.append((descriptors[descriptor][0], int(match.partition(":")[0]), match))
                for rel_path, _line_number, match in sorted(batch_matches):
                    matches.append(f"{rel_path}:{match}")
                    if explicit_limit is not None and len(matches) >= explicit_limit:
                        break
        else:
            for rel_path in paths:
                if explicit_limit is not None and len(matches) >= explicit_limit:
                    break
                try:
                    with open_workspace_file(ws, rel_path) as source, io.TextIOWrapper(source, encoding="utf-8", errors="replace") as handle:
                        for idx, line in enumerate(handle, start=1):
                            if compiled.search(line):
                                matches.append(f"{rel_path}:{idx}:{line.strip()}")
                                if explicit_limit is not None and len(matches) >= explicit_limit:
                                    break
                except WorkspaceAuthorityError:
                    raise
                except Exception as _read_err:
                    logger.debug("[Workspace] grep: skipped file %s: %s", rel_path, _read_err)
                    continue
    except WorkspaceAuthorityError as exc:
        return _workspace_storage_error(tool_name, exc)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Grep search failed: {e}", retryable=True)

    if not matches:
        return f"🔎 No matches for '{pattern}'"
    lines = [f"🔎 Grep results for '{pattern}' ({len(matches)} match(es)):"]
    lines.extend(f"- {match}" for match in matches)
    return "\n".join(lines)


async def _tool_search(ws: Path, query: str = "", agent_id: uuid.UUID | str | None = None, *, authority_scope=None) -> str:
    from app.services.agent_tools import discoverable_tool_names_for_query
    from app.services.tool_search_manifest import build_tool_search_manifest, render_tool_search_manifest_sections

    def source_allowed(path):
        try:
            relative = path.resolve().relative_to(ws.resolve()).as_posix()
        except ValueError:
            return False
        return _authority_allows_path(authority_scope, relative)

    registry = _build_skill_registry(ws, source_authorizer=source_allowed) if authority_scope is not None else _build_skill_registry(ws)
    normalized = query.strip().lower()
    matching_skills = [
        skill
        for skill in (registry.resolve(name) for name in registry.names())
        if not normalized
        or normalized in skill.metadata.name.lower()
        or normalized in skill.metadata.description.lower()
        or any(normalized in tool.lower() for tool in skill.metadata.declared_tools)
    ]
    capture = getattr(authority_scope, "skill_inputs", None)
    if capture is not None:
        for skill in matching_skills:
            try:
                path = skill.file_path.resolve().relative_to(ws.resolve()).as_posix()
            except ValueError:
                capture.unbound.append("discovery_skill_workspace_unverified")
                continue
            capture.observe_file(path, skill.source_sha256)

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
    if capture is not None and manifest.get("subagent_candidates"):
        # Keep discovery usable while retaining unknown provenance for metadata
        # that does not yet have a versioned source reader.
        capture.unbound.append("discovery_subagent_sources_unverified")

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
        for skill in matching_skills:
            declared = (
                ", ".join(skill.metadata.declared_tools) if skill.metadata.declared_tools else "no declared tools"
            )
            lines.append(f"- {skill.metadata.name}: {skill.metadata.description} | declared tools: {declared}")
    if len(lines) == 1:
        return f"🔎 No delayed tools or skills matched '{query}'"
    return "\n".join(lines)


def _delete_file(ws: Path, rel_path: str, tool_name: str = "delete_file", authority_scope=None) -> str:
    normalized_path, authority_error = _authorize_workspace_mutation_path(
        ws,
        rel_path,
        authority_scope=authority_scope,
        action="delete",
        tool_name=tool_name,
    )
    if authority_error:
        return authority_error
    assert normalized_path is not None
    rel_path = normalized_path

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
    is_directory = file_path.is_dir()
    if is_directory:
        unauthorized = any(
            not _authority_allows_path(authority_scope, item.relative_to(ws).as_posix(), action="delete")
            for item in file_path.rglob("*")
            if item.is_file()
        )
        if unauthorized:
            return _workspace_error(tool_name, "auth_or_permission", "Directory contains another owner's resources.")
    elif not _authority_allows_path(authority_scope, rel_path, action="delete"):
        return _workspace_error(tool_name, "auth_or_permission", "Access denied for this resource owner.")

    try:
        delete_workspace_path(ws, rel_path, directory=is_directory)
        if is_directory:
            return f"✅ Deleted directory {rel_path}"
        return f"✅ Deleted {rel_path}"
    except WorkspaceAuthorityError as exc:
        return _workspace_storage_error(tool_name, exc, mutation=True)
    except Exception as e:
        return _workspace_error(tool_name, "operation_failed", f"Delete failed: {e}")
