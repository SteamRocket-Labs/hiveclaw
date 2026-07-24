"""File-backed shared team memory namespace."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.memory.write_gate import MemoryWriteDecision, prepare_memory_write, prepare_memory_write_with_llm
from app.services.exact_secret_boundary import ExactSecretBoundary


class TeamMemoryWriteRejectedError(ValueError):
    """Raised when the Memory Control Plane rejects a team-memory write."""

    def __init__(self, decision: MemoryWriteDecision) -> None:
        super().__init__(f"{decision.sensitivity}: {decision.reason}")
        self.decision = decision


class SecretScanError(TeamMemoryWriteRejectedError):
    """Raised when team memory content appears to contain a secret."""


class TeamMemoryWriteHeldError(ValueError):
    """Raised when semantic review is unavailable and the candidate is preserved for retry."""

    def __init__(self, decision: MemoryWriteDecision) -> None:
        super().__init__(f"{decision.sensitivity}: {decision.reason}")
        self.decision = decision


class InvalidTeamMemoryModeError(ValueError):
    """Raised when a team-memory write mode is not supported."""


class TeamMemoryConflictError(ValueError):
    """Raised when a client writes against a stale team-memory revision."""

    def __init__(self, current_entry: "TeamMemoryEntry") -> None:
        super().__init__("Team memory entry revision conflict.")
        self.current_entry = current_entry


@dataclass(slots=True)
class TeamMemoryEntry:
    tenant_id: str
    workspace_key: str
    key: str
    title: str
    updated_at: str
    revision: int
    checksum: str
    updated_by: str
    path: str
    absolute_path: str
    snippet: str = ""
    content: str | None = None
    deleted: bool = False
    sync_token: str = ""


def _slugify(value: str, *, fallback: str = "entry") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    normalized = cleaned.strip(".-")
    if normalized in {"", ".", ".."}:
        return fallback
    return normalized


def _safe_component(value: str) -> str:
    return _slugify(value.replace("/", "-"), fallback="workspace")


def _render_entry_markdown(
    *,
    key: str,
    title: str,
    tenant_id: str,
    workspace_key: str,
    updated_at: str,
    revision: int,
    checksum: str,
    updated_by: str,
    body: str,
    deleted: bool = False,
) -> str:
    return (
        "---\n"
        f"key: {key}\n"
        f"title: {title}\n"
        f"tenant_id: {tenant_id}\n"
        f"workspace_key: {workspace_key}\n"
        f"updated_at: {updated_at}\n"
        f"revision: {revision}\n"
        f"checksum: {checksum}\n"
        f"updated_by: {updated_by}\n"
        f"deleted: {'true' if deleted else 'false'}\n"
        "---\n"
        f"# {title}\n\n"
        f"{body.strip()}\n"
    )


def _parse_entry_markdown(path: Path) -> tuple[dict[str, str], str]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return {}, content.strip()
    _, remainder = content.split("---\n", 1)
    if "\n---\n" not in remainder:
        return {}, content.strip()
    frontmatter_text, body = remainder.split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    body_text = body.strip()
    heading = f"# {metadata.get('title', '')}".strip()
    if heading != "#" and body_text.startswith(heading):
        body_text = body_text[len(heading) :].lstrip()
    return metadata, body_text


def _compute_checksum(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def _compute_sync_token(*, revision: int, checksum: str, deleted: bool) -> str:
    return f"tm:{revision}:{checksum}:{1 if deleted else 0}"


def _coerce_revision(metadata: dict[str, str]) -> int:
    try:
        return max(int(metadata.get("revision", "0") or 0), 0)
    except ValueError:
        return 0


def _coerce_deleted(metadata: dict[str, str]) -> bool:
    return str(metadata.get("deleted", "")).strip().lower() == "true"


class TeamMemoryStore:
    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        exact_secret_boundary: ExactSecretBoundary | None = None,
    ) -> None:
        self._root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
        self._exact_secret_boundary = exact_secret_boundary or ExactSecretBoundary.empty()

    def _workspace_dir(self, tenant_id: str, workspace_key: str) -> Path:
        base = (self._root / "shared_memory").resolve()
        path = (base / _safe_component(tenant_id) / _safe_component(workspace_key)).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Unsafe workspace_key for team memory storage.")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _entry_path(self, tenant_id: str, workspace_key: str, key: str) -> Path:
        workspace_dir = self._workspace_dir(tenant_id, workspace_key).resolve()
        path = (workspace_dir / f"{_slugify(key)}.md").resolve()
        if not path.is_relative_to(workspace_dir):
            raise ValueError("Unsafe key for team memory storage.")
        return path

    def _scan_for_secrets(self, text: str) -> None:
        exact = self._exact_secret_boundary.redact_text(text)
        if exact.redacted_count:
            decision = MemoryWriteDecision(
                original_content=text,
                content=exact.text,
                category="team_memory",
                sensitivity="PL4_credential",
                metadata={
                    "status": "rejected",
                    "decision_boundary": "exact_secret_authority",
                    "secret_evidence_refs": ",".join(exact.matched_refs),
                },
                rejected=True,
                reason="Team memory content contains an exact unauthorized credential binding.",
            )
            raise SecretScanError(decision)

    def _prepare_content_for_write(
        self,
        *,
        tenant_id: str,
        workspace_key: str,
        key: str,
        content: str,
        updated_by: str,
    ) -> str:
        decision = prepare_memory_write(
            content,
            category="team_memory",
            evidence_refs=[
                f"team_memory:{tenant_id}/{workspace_key}/{_slugify(key)}",
                f"updated_by:{updated_by}",
            ],
            enforce_form=False,
        )
        if decision.rejected:
            if decision.sensitivity == "PL4_credential":
                raise SecretScanError(decision)
            raise TeamMemoryWriteRejectedError(decision)
        if decision.held:
            raise TeamMemoryWriteHeldError(decision)
        return decision.content

    async def _prepare_content_for_write_with_llm(
        self,
        *,
        tenant_id: str,
        workspace_key: str,
        key: str,
        content: str,
        updated_by: str,
    ) -> str:
        decision = await prepare_memory_write_with_llm(
            content,
            category="team_memory",
            evidence_refs=[
                f"team_memory:{tenant_id}/{workspace_key}/{_slugify(key)}",
                f"updated_by:{updated_by}",
            ],
            enforce_form=False,
            tenant_id=tenant_id,
        )
        if decision.rejected:
            if decision.sensitivity == "PL4_credential":
                raise SecretScanError(decision)
            raise TeamMemoryWriteRejectedError(decision)
        if decision.held:
            raise TeamMemoryWriteHeldError(decision)
        return decision.content

    def upsert_entry(
        self,
        *,
        tenant_id: str,
        workspace_key: str,
        key: str,
        title: str,
        content: str,
        mode: str = "replace",
        updated_by: str = "system",
        base_revision: int | None = None,
        sync_token: str | None = None,
    ) -> TeamMemoryEntry:
        if mode not in {"replace", "append"}:
            raise InvalidTeamMemoryModeError(f"Unsupported team memory mode: {mode}")
        self._scan_for_secrets(f"{title}\n{content}")
        prepared_content = self._prepare_content_for_write(
            tenant_id=tenant_id,
            workspace_key=workspace_key,
            key=key,
            content=content,
            updated_by=updated_by,
        )
        return self._upsert_prepared_entry(
            tenant_id=tenant_id,
            workspace_key=workspace_key,
            key=key,
            title=title,
            prepared_content=prepared_content,
            mode=mode,
            updated_by=updated_by,
            base_revision=base_revision,
            sync_token=sync_token,
        )

    async def upsert_entry_async(
        self,
        *,
        tenant_id: str,
        workspace_key: str,
        key: str,
        title: str,
        content: str,
        mode: str = "replace",
        updated_by: str = "system",
        base_revision: int | None = None,
        sync_token: str | None = None,
    ) -> TeamMemoryEntry:
        if mode not in {"replace", "append"}:
            raise InvalidTeamMemoryModeError(f"Unsupported team memory mode: {mode}")
        self._scan_for_secrets(f"{title}\n{content}")
        prepared_content = await self._prepare_content_for_write_with_llm(
            tenant_id=tenant_id,
            workspace_key=workspace_key,
            key=key,
            content=content,
            updated_by=updated_by,
        )
        return self._upsert_prepared_entry(
            tenant_id=tenant_id,
            workspace_key=workspace_key,
            key=key,
            title=title,
            prepared_content=prepared_content,
            mode=mode,
            updated_by=updated_by,
            base_revision=base_revision,
            sync_token=sync_token,
        )

    def _upsert_prepared_entry(
        self,
        *,
        tenant_id: str,
        workspace_key: str,
        key: str,
        title: str,
        prepared_content: str,
        mode: str,
        updated_by: str,
        base_revision: int | None,
        sync_token: str | None,
    ) -> TeamMemoryEntry:
        path = self._entry_path(tenant_id, workspace_key, key)
        existing_body = ""
        existing_metadata: dict[str, str] = {}
        current_entry: TeamMemoryEntry | None = None
        if path.exists():
            existing_metadata, existing_body = _parse_entry_markdown(path)
            current_entry = self._entry_from_path(path, include_content=True, include_deleted=True)
        existing_revision = _coerce_revision(existing_metadata)
        sync_conflict = bool(sync_token and current_entry and sync_token != current_entry.sync_token)
        revision_conflict = bool(base_revision is not None and path.exists() and base_revision != existing_revision)
        if revision_conflict or sync_conflict:
            if current_entry is None:
                raise TeamMemoryConflictError(
                    TeamMemoryEntry(
                        tenant_id=tenant_id,
                        workspace_key=workspace_key,
                        key=_slugify(key),
                        title=title.strip() or key.strip(),
                        updated_at="",
                        revision=existing_revision,
                        checksum=existing_metadata.get("checksum", ""),
                        updated_by=existing_metadata.get("updated_by", ""),
                        path=str(path),
                        absolute_path=str(path.resolve()),
                        sync_token=_compute_sync_token(
                            revision=existing_revision,
                            checksum=existing_metadata.get("checksum", ""),
                            deleted=_coerce_deleted(existing_metadata),
                        ),
                    )
                )
            raise TeamMemoryConflictError(current_entry)
        if mode == "append" and existing_body.strip():
            body = f"{existing_body.strip()}\n\n---\n\n{prepared_content.strip()}"
        else:
            body = prepared_content.strip()
        updated_at = datetime.now(timezone.utc).isoformat()
        revision = existing_revision + 1 if path.exists() else 1
        checksum = _compute_checksum(body)
        path.write_text(
            _render_entry_markdown(
                key=_slugify(key),
                title=title.strip() or key.strip(),
                tenant_id=tenant_id,
                workspace_key=workspace_key,
                updated_at=updated_at,
                revision=revision,
                checksum=checksum,
                updated_by=updated_by,
                body=body,
            ),
            encoding="utf-8",
        )
        return self._entry_from_path(path, include_content=True)  # type: ignore[return-value]

    def _entry_from_path(
        self,
        path: Path,
        *,
        snippet: str = "",
        include_content: bool = False,
        include_deleted: bool = False,
    ) -> TeamMemoryEntry | None:
        metadata, body = _parse_entry_markdown(path)
        deleted = _coerce_deleted(metadata)
        if deleted and not include_deleted:
            return None
        return TeamMemoryEntry(
            tenant_id=metadata.get("tenant_id", ""),
            workspace_key=metadata.get("workspace_key", ""),
            key=metadata.get("key", path.stem),
            title=metadata.get("title", path.stem),
            updated_at=metadata.get("updated_at", ""),
            revision=_coerce_revision(metadata),
            checksum=metadata.get("checksum", ""),
            updated_by=metadata.get("updated_by", ""),
            path=str(path),
            absolute_path=str(path.resolve()),
            snippet=snippet or body[:160],
            content=body if include_content else None,
            deleted=deleted,
            sync_token=_compute_sync_token(
                revision=_coerce_revision(metadata),
                checksum=metadata.get("checksum", ""),
                deleted=deleted,
            ),
        )

    def list_entries(
        self, tenant_id: str, workspace_key: str, *, include_deleted: bool = False
    ) -> list[TeamMemoryEntry]:
        root = self._workspace_dir(tenant_id, workspace_key)
        entries = [
            entry
            for path in sorted(root.glob("*.md"))
            if (entry := self._entry_from_path(path, include_deleted=include_deleted)) is not None
        ]
        entries.sort(key=lambda entry: entry.updated_at, reverse=True)
        return entries

    def search_entries(
        self,
        tenant_id: str,
        workspace_key: str,
        query: str,
        *,
        include_deleted: bool = False,
    ) -> list[TeamMemoryEntry]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return self.list_entries(tenant_id, workspace_key, include_deleted=include_deleted)
        matches: list[TeamMemoryEntry] = []
        for path in sorted(self._workspace_dir(tenant_id, workspace_key).glob("*.md")):
            metadata, body = _parse_entry_markdown(path)
            if _coerce_deleted(metadata) and not include_deleted:
                continue
            haystack = "\n".join([metadata.get("title", ""), body]).lower()
            if normalized_query not in haystack:
                continue
            snippet_source = next(
                (line.strip() for line in body.splitlines() if normalized_query in line.lower()),
                body.strip(),
            )
            entry = self._entry_from_path(path, snippet=snippet_source[:200], include_deleted=include_deleted)
            if entry is not None:
                matches.append(entry)
        matches.sort(key=lambda entry: entry.updated_at, reverse=True)
        return matches

    def get_entry(
        self,
        tenant_id: str,
        workspace_key: str,
        key: str,
        *,
        include_deleted: bool = False,
    ) -> TeamMemoryEntry | None:
        path = self._entry_path(tenant_id, workspace_key, key)
        if not path.exists():
            return None
        return self._entry_from_path(path, include_content=True, include_deleted=include_deleted)

    def delete_entry(
        self,
        tenant_id: str,
        workspace_key: str,
        key: str,
        *,
        updated_by: str = "system",
        base_revision: int | None = None,
        sync_token: str | None = None,
    ) -> bool:
        path = self._entry_path(tenant_id, workspace_key, key)
        if not path.exists():
            return False
        metadata, body = _parse_entry_markdown(path)
        if _coerce_deleted(metadata):
            return False
        existing_revision = _coerce_revision(metadata)
        current_entry = self._entry_from_path(path, include_content=True, include_deleted=True)
        revision_conflict = base_revision is not None and base_revision != existing_revision
        sync_conflict = bool(sync_token and current_entry and sync_token != current_entry.sync_token)
        if revision_conflict or sync_conflict:
            if current_entry is not None:
                raise TeamMemoryConflictError(current_entry)
        updated_at = datetime.now(timezone.utc).isoformat()
        tombstone_body = body.strip() or f"Deleted by {updated_by}."
        path.write_text(
            _render_entry_markdown(
                key=metadata.get("key", _slugify(key)),
                title=metadata.get("title", key),
                tenant_id=metadata.get("tenant_id", tenant_id),
                workspace_key=metadata.get("workspace_key", workspace_key),
                updated_at=updated_at,
                revision=existing_revision + 1,
                checksum=_compute_checksum(tombstone_body),
                updated_by=updated_by,
                body=tombstone_body,
                deleted=True,
            ),
            encoding="utf-8",
        )
        return True
