from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.services.officecli_adapter import OfficeCLIAdapter


SUPPORTED_OFFICE_KINDS: frozenset[str] = frozenset({"docx", "xlsx", "pptx"})
SUPPORTED_OFFICE_SUFFIXES: dict[str, str] = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}


class OfficeDocumentError(RuntimeError):
    error_code = "office_document_error"


class OfficeDocumentPathError(OfficeDocumentError):
    error_code = "auth_or_permission"


class OfficeDocumentActiveSessionError(OfficeDocumentError):
    error_code = "active_editor_session"


class OfficeDocumentExistsError(OfficeDocumentError):
    error_code = "already_exists"


class OfficeDocumentNotFoundError(OfficeDocumentError):
    error_code = "not_found"


class OfficeDocumentService:
    """Workspace-scoped document manager for Office artifacts and sidecars."""

    def __init__(self, workspace: Path, *, adapter: OfficeCLIAdapter | None = None) -> None:
        self.workspace = workspace.resolve()
        self.adapter = adapter or OfficeCLIAdapter()

    def resolve_document_path(self, rel_path: str) -> Path:
        normalized = self._normalize_rel_path(rel_path)
        target = (self.workspace / normalized).resolve()
        self._assert_under_workspace(target)
        return target

    def manifest_path(self, rel_path: str) -> Path:
        normalized = self._normalize_rel_path(rel_path)
        digest = sha256(normalized.encode("utf-8")).hexdigest()
        return self.workspace / ".office_meta" / digest / "manifest.json"

    def create_document(
        self,
        rel_path: str,
        *,
        kind: str,
        template_path: str | None = None,
    ) -> dict[str, Any]:
        target = self.resolve_document_path(rel_path)
        normalized = self._normalize_rel_path(rel_path)
        kind = (kind or "").strip().lower()
        if kind not in SUPPORTED_OFFICE_KINDS:
            raise ValueError(f"Unsupported office document kind: {kind}")
        if target.suffix.lower() != f".{kind}":
            raise ValueError(f"Document path suffix must match kind .{kind}")
        if target.exists():
            raise OfficeDocumentExistsError(f"Document already exists: {normalized}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if template_path:
            template = self.resolve_document_path(template_path)
            if not template.is_file():
                raise OfficeDocumentNotFoundError(f"Template not found: {template_path}")
            self._atomic_copy(template, target)
        else:
            self._create_blank_document(target, kind)

        manifest = self._load_manifest(normalized)
        manifest.update(
            {
                "path": normalized,
                "kind": kind,
                "created_at": self._now(),
                "updated_at": self._now(),
                "current_version": 0,
            }
        )
        self._save_manifest(normalized, manifest)
        return {"path": normalized, "kind": kind, "size": target.stat().st_size}

    def set_active_editor_session(self, rel_path: str, *, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        normalized = self._normalize_rel_path(rel_path)
        manifest = self._load_manifest(normalized)
        session = {
            "session_id": session_id,
            "user_id": user_id,
            "started_at": self._now(),
        }
        manifest["active_editor_session"] = session
        manifest["updated_at"] = self._now()
        self._save_manifest(normalized, manifest)
        return session

    def clear_active_editor_session(self, rel_path: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        normalized = self._normalize_rel_path(rel_path)
        manifest = self._load_manifest(normalized)
        session = manifest.get("active_editor_session")
        if not session:
            return None
        if session_id and session.get("session_id") != session_id:
            return session
        manifest["active_editor_session"] = None
        manifest["updated_at"] = self._now()
        self._save_manifest(normalized, manifest)
        return None

    def get_active_editor_session(self, rel_path: str) -> dict[str, Any] | None:
        manifest = self._load_manifest(self._normalize_rel_path(rel_path))
        session = manifest.get("active_editor_session")
        return session if isinstance(session, dict) else None

    def atomic_save_bytes(
        self,
        rel_path: str,
        content: bytes,
        *,
        reason: str,
        require_no_active_editor: bool = True,
    ) -> dict[str, Any]:
        normalized = self._normalize_rel_path(rel_path)
        target = self.resolve_document_path(normalized)
        manifest = self._load_manifest(normalized)
        if require_no_active_editor and manifest.get("active_editor_session"):
            raise OfficeDocumentActiveSessionError(f"Active editor session exists for {normalized}")

        next_version = int(manifest.get("current_version") or 0) + 1
        revision_entry: dict[str, Any] | None = None
        if target.exists():
            revision_entry = self._write_revision(normalized, target, version=next_version, reason=reason)

        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_bytes(target, content)

        revisions = list(manifest.get("revisions") or [])
        if revision_entry:
            revisions.append(revision_entry)
        manifest.update(
            {
                "path": normalized,
                "kind": self._kind_for_path(target),
                "current_version": next_version,
                "updated_at": self._now(),
                "revisions": revisions,
            }
        )
        self._save_manifest(normalized, manifest)
        return {"path": normalized, "version": next_version, "size": len(content)}

    def run_view(self, rel_path: str, *, mode: str = "outline", page: int | None = None) -> dict[str, Any]:
        target = self._require_existing_file(rel_path)
        options: dict[str, Any] = {"mode": mode}
        if page is not None:
            options["page"] = page
        return self.adapter.run("view", target, options=options, cwd=self.workspace)

    def run_query(self, rel_path: str, *, selector: str, depth: int | None = None) -> dict[str, Any]:
        target = self._require_existing_file(rel_path)
        options: dict[str, Any] = {"selector": selector}
        if depth is not None:
            options["depth"] = depth
        return self.adapter.run("query", target, options=options, cwd=self.workspace)

    def run_validate(self, rel_path: str) -> dict[str, Any]:
        target = self._require_existing_file(rel_path)
        return self.adapter.run("validate", target, cwd=self.workspace)

    def run_dump(self, rel_path: str) -> dict[str, Any]:
        target = self._require_existing_file(rel_path)
        return self.adapter.run("get", target, cwd=self.workspace)

    def run_apply(
        self,
        rel_path: str,
        *,
        operations: list[dict[str, Any]],
        output_path: str | None = None,
        require_no_active_editor: bool = True,
    ) -> dict[str, Any]:
        normalized = self._normalize_rel_path(rel_path)
        target = self._require_existing_file(normalized)
        manifest = self._load_manifest(normalized)
        if require_no_active_editor and manifest.get("active_editor_session"):
            raise OfficeDocumentActiveSessionError(f"Active editor session exists for {normalized}")

        op_file = self._write_operation(normalized, operations)
        options: dict[str, Any] = {"operations": op_file}
        if output_path:
            options["output"] = self.resolve_document_path(output_path)
        payload = self.adapter.run("batch", target, options=options, cwd=self.workspace)

        manifest = self._load_manifest(normalized)
        manifest["updated_at"] = self._now()
        manifest.setdefault("operations", []).append(
            {
                "file": Path(op_file).name,
                "created_at": self._now(),
                "operation_count": len(operations),
                "output_path": output_path,
            }
        )
        self._save_manifest(normalized, manifest)
        return payload

    def _require_existing_file(self, rel_path: str) -> Path:
        target = self.resolve_document_path(rel_path)
        if not target.is_file():
            raise OfficeDocumentNotFoundError(f"Document not found: {rel_path}")
        return target

    def _normalize_rel_path(self, rel_path: str) -> str:
        raw = (rel_path or "").strip()
        if not raw:
            raise OfficeDocumentPathError("Document path is required")
        path = Path(raw)
        if path.is_absolute():
            raise OfficeDocumentPathError("Absolute document paths are not allowed")
        if any(part in {"..", ""} for part in path.parts):
            raise OfficeDocumentPathError("Document path cannot escape the workspace")
        if path.parts and path.parts[0] == ".office_meta":
            raise OfficeDocumentPathError("Office sidecar metadata is not a document path")
        return path.as_posix()

    def _assert_under_workspace(self, target: Path) -> None:
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise OfficeDocumentPathError("Document path cannot escape the workspace") from exc

    def _load_manifest(self, rel_path: str) -> dict[str, Any]:
        path = self.manifest_path(rel_path)
        if not path.exists():
            return {
                "path": rel_path,
                "kind": self._kind_for_path(Path(rel_path)),
                "current_version": 0,
                "created_at": self._now(),
                "updated_at": self._now(),
                "active_editor_session": None,
                "revisions": [],
                "operations": [],
            }
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OfficeDocumentError(f"Invalid office manifest JSON for {rel_path}") from exc

    def _save_manifest(self, rel_path: str, manifest: dict[str, Any]) -> None:
        path = self.manifest_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def _write_revision(self, rel_path: str, target: Path, *, version: int, reason: str) -> dict[str, Any]:
        meta_dir = self.manifest_path(rel_path).parent
        revisions_dir = meta_dir / "revisions"
        revisions_dir.mkdir(parents=True, exist_ok=True)
        revision_name = f"{version:06d}.bin"
        revision_path = revisions_dir / revision_name
        shutil.copyfile(target, revision_path)
        return {
            "version": version,
            "file": revision_name,
            "reason": reason,
            "created_at": self._now(),
            "size": revision_path.stat().st_size,
        }

    def _write_operation(self, rel_path: str, operations: list[dict[str, Any]]) -> str:
        meta_dir = self.manifest_path(rel_path).parent
        operations_dir = meta_dir / "operations"
        operations_dir.mkdir(parents=True, exist_ok=True)
        operation_name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
        operation_path = operations_dir / operation_name
        self._atomic_write_text(operation_path, json.dumps({"operations": operations}, ensure_ascii=False, indent=2))
        return str(operation_path)

    def _create_blank_document(self, target: Path, kind: str) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=target.suffix, dir=target.parent) as handle:
            tmp_path = Path(handle.name)
        try:
            if kind == "docx":
                from docx import Document

                document = Document()
                document.add_paragraph("")
                document.save(tmp_path)
            elif kind == "xlsx":
                from openpyxl import Workbook

                workbook = Workbook()
                workbook.save(tmp_path)
            elif kind == "pptx":
                from pptx import Presentation

                presentation = Presentation()
                presentation.slides.add_slide(presentation.slide_layouts[6])
                presentation.save(tmp_path)
            else:
                raise ValueError(f"Unsupported office document kind: {kind}")
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _atomic_copy(self, source: Path, target: Path) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=target.suffix, dir=target.parent) as handle:
            tmp_path = Path(handle.name)
        try:
            shutil.copyfile(source, tmp_path)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(delete=False, dir=path.parent) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
        try:
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
        try:
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _kind_for_path(path: Path) -> str | None:
        return SUPPORTED_OFFICE_SUFFIXES.get(path.suffix.lower())

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
