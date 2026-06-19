"""Explicit Memory Overlay for user-commanded `remember this` facts.

Overlay entries are T3-level intent with immediate activation, but they are not
accepted T3 truth until the T3 consolidation loop absorbs them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from app.memory.md_store import MEMORY_DEDUP_THRESHOLD, jaccard_similarity
from app.memory.types import MEMORY_CATEGORIES
from app.memory.write_gate import prepare_memory_write_with_llm

_MAX_CONTENT_CHARS = 2000
_SEARCH_PREVIEW_CHARS = 180


@dataclass(frozen=True, slots=True)
class ExplicitMemoryOverlayResult:
    status: str  # active | duplicate | rejected
    category: str
    entry_id: str = ""
    path: str = ""
    reason: str = ""
    sensitivity: str = ""
    target_hint: str = "unknown"
    content: str = ""


@dataclass(frozen=True, slots=True)
class ExplicitMemoryOverlayEntry:
    entry_id: str
    status: str
    category: str
    target_hint: str
    content: str
    source_refs: tuple[str, ...]
    sensitivity: str
    path: Path
    created_at: str
    metadata: dict[str, str]


async def write_explicit_memory_overlay(
    agent_id: uuid.UUID,
    *,
    category: str,
    content: str,
    source_refs: list[str] | tuple[str, ...] | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    data_root: Path | str,
    target_hint: str | None = None,
    origin: str = "explicit_user_request",
    extra_metadata: dict[str, str] | None = None,
) -> ExplicitMemoryOverlayResult:
    normalized_category = (category or "general").strip().lower()
    if normalized_category not in MEMORY_CATEGORIES:
        normalized_category = "general"
    trimmed = (content or "").strip()[:_MAX_CONTENT_CHARS]
    if not trimmed:
        return ExplicitMemoryOverlayResult(status="rejected", category=normalized_category, reason="empty content")

    refs = _normalize_refs(source_refs)
    decision = await prepare_memory_write_with_llm(
        trimmed,
        category=normalized_category,
        evidence_refs=refs,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    if decision.rejected:
        return ExplicitMemoryOverlayResult(
            status="rejected",
            category=decision.category,
            reason=decision.reason,
            sensitivity=decision.sensitivity,
        )

    root = Path(data_root)
    overlay_dir = explicit_overlay_dir(root, agent_id)
    entries_dir = overlay_dir / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    created_at = _now()
    resolved_target_hint = target_hint if target_hint in _TARGET_HINTS else _target_hint_for_category(decision.category)
    duplicate = _find_similar_active_overlay(
        data_root=root,
        agent_id=agent_id,
        category=decision.category,
        target_hint=resolved_target_hint,
        content=decision.content,
    )
    if duplicate is not None:
        return ExplicitMemoryOverlayResult(
            status="duplicate",
            category=decision.category,
            entry_id=duplicate.entry_id,
            path=f"memory/explicit/entries/{duplicate.entry_id}.md",
            reason="similar explicit memory already exists",
            sensitivity=decision.sensitivity,
            target_hint=duplicate.target_hint,
            content=duplicate.content,
        )

    entry_id = _explicit_entry_id(created_at=created_at, category=decision.category, content=decision.content)
    path = entries_dir / f"{entry_id}.md"
    metadata = {
        **decision.metadata,
        **{str(key): str(value) for key, value in (extra_metadata or {}).items() if value is not None},
        "id": entry_id,
        "origin": origin,
        "category": decision.category,
        "target_hint": resolved_target_hint,
        "status": "active",
        "sensitivity": decision.sensitivity,
        "created_at": created_at,
        "source_refs": ",".join(refs),
    }
    path.write_text(
        _render_entry_md(
            entry_id=entry_id,
            origin=origin,
            category=decision.category,
            target_hint=resolved_target_hint,
            status="active",
            sensitivity=decision.sensitivity,
            created_at=created_at,
            source_refs=refs,
            user_words=decision.content,
            normalized_memory=decision.content,
            why_it_matters="User explicitly asked this to be remembered across sessions.",
            extra_metadata={str(key): str(value) for key, value in (extra_metadata or {}).items() if value is not None},
        ),
        encoding="utf-8",
    )
    _append_manifest(overlay_dir / "manifest.jsonl", metadata)
    _rebuild_memory_index(root, agent_id)
    return ExplicitMemoryOverlayResult(
        status="active",
        category=decision.category,
        entry_id=entry_id,
        path=f"memory/explicit/entries/{entry_id}.md",
        sensitivity=decision.sensitivity,
        target_hint=resolved_target_hint,
        content=decision.content,
    )


def explicit_overlay_dir(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "explicit"


def load_explicit_overlay_entries(data_root: Path, agent_id: uuid.UUID | str) -> list[ExplicitMemoryOverlayEntry]:
    overlay_dir = explicit_overlay_dir(Path(data_root), agent_id)
    manifest_path = overlay_dir / "manifest.jsonl"
    if not manifest_path.exists():
        return []
    latest: dict[str, dict[str, str]] = {}
    for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        entry_id = str(record.get("id") or "").strip()
        if entry_id:
            latest[entry_id] = {str(key): str(value) for key, value in record.items() if value is not None}

    entries: list[ExplicitMemoryOverlayEntry] = []
    for entry_id, metadata in latest.items():
        path = overlay_dir / "entries" / f"{entry_id}.md"
        if not path.exists():
            continue
        content = _extract_tag(path.read_text(encoding="utf-8", errors="replace"), "normalized_memory")
        refs = tuple(ref for ref in metadata.get("source_refs", "").split(",") if ref)
        entries.append(
            ExplicitMemoryOverlayEntry(
                entry_id=entry_id,
                status=metadata.get("status", "active"),
                category=metadata.get("category", "general"),
                target_hint=metadata.get("target_hint", "unknown"),
                content=content,
                source_refs=refs,
                sensitivity=metadata.get("sensitivity", "PL1_public"),
                path=path,
                created_at=metadata.get("created_at", ""),
                metadata=metadata,
            )
        )
    return entries


def load_explicit_overlay_entries_by_ids(
    data_root: Path,
    agent_id: uuid.UUID | str,
    ids: list[str] | tuple[str, ...],
) -> list[ExplicitMemoryOverlayEntry]:
    wanted = {str(entry_id).strip() for entry_id in ids if str(entry_id).strip()}
    if not wanted:
        return []
    return [entry for entry in load_explicit_overlay_entries(data_root, agent_id) if entry.entry_id in wanted]


def search_explicit_overlay_entries(
    data_root: Path,
    agent_id: uuid.UUID | str,
    query: str,
    *,
    limit: int = 5,
) -> list[dict]:
    needle = (query or "").strip()
    entries = [entry for entry in load_explicit_overlay_entries(data_root, agent_id) if entry.status == "active"]
    if needle:
        scored = [
            (_simple_score(needle, f"{entry.category} {entry.target_hint} {entry.content}"), entry)
            for entry in entries
        ]
        scored = [(score, entry) for score, entry in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)
        entries = [entry for _score, entry in scored]
    return [
        {
            "id": entry.entry_id,
            "content": entry.content,
            "preview": _preview(entry.content),
            "category": entry.category,
            "target_hint": entry.target_hint,
            "source": f"memory/explicit/entries/{entry.entry_id}.md",
            "source_type": "explicit_overlay",
            "timestamp": entry.created_at,
            "sensitivity": entry.sensitivity,
        }
        for entry in entries[:limit]
    ]


def update_explicit_overlay_status(
    data_root: Path,
    agent_id: uuid.UUID | str,
    entry_id: str,
    *,
    status: str,
    reason: str = "",
    accepted_blocks: list[str] | None = None,
) -> bool:
    entries = {entry.entry_id: entry for entry in load_explicit_overlay_entries(data_root, agent_id)}
    entry = entries.get(entry_id)
    if entry is None:
        return False
    record = {
        **entry.metadata,
        "id": entry_id,
        "status": status,
        "status_reason": reason,
        "accepted_blocks": ",".join(accepted_blocks or []),
        "updated_at": _now(),
    }
    _append_manifest(explicit_overlay_dir(Path(data_root), agent_id) / "manifest.jsonl", record)
    _rebuild_memory_index(Path(data_root), agent_id)
    return True


_TARGET_HINTS = frozenset({"user", "worker", "capabilities", "episodes", "unknown"})


def _target_hint_for_category(category: str) -> str:
    normalized = (category or "general").strip().lower()
    if normalized in {"user", "feedback"}:
        return "user"
    if normalized in {"constraint", "blocked_pattern", "worker"}:
        return "worker"
    if normalized in {"strategy", "project", "reference", "general", "capability"}:
        return "capabilities"
    if normalized == "episode":
        return "episodes"
    return "unknown"


def _find_similar_active_overlay(
    *,
    data_root: Path,
    agent_id: uuid.UUID | str,
    category: str,
    target_hint: str,
    content: str,
) -> ExplicitMemoryOverlayEntry | None:
    """Find an active overlay entry that would create duplicate T3 input noise."""
    best_score = 0.0
    best: ExplicitMemoryOverlayEntry | None = None
    for entry in load_explicit_overlay_entries(data_root, agent_id):
        if entry.status != "active":
            continue
        if entry.category != category or entry.target_hint != target_hint:
            continue
        score = jaccard_similarity(content, entry.content)
        if score >= MEMORY_DEDUP_THRESHOLD and score > best_score:
            best_score = score
            best = entry
    return best


def _render_entry_md(
    *,
    entry_id: str,
    origin: str,
    category: str,
    target_hint: str,
    status: str,
    sensitivity: str,
    created_at: str,
    source_refs: list[str],
    user_words: str,
    normalized_memory: str,
    why_it_matters: str,
    extra_metadata: dict[str, str] | None = None,
) -> str:
    refs_yaml = "\n".join(f"  - {ref}" for ref in source_refs) or "  - tool:save_memory"
    refs_xml = "\n".join(f"    <source_ref>{escape(ref)}</source_ref>" for ref in source_refs)
    extra_yaml = "".join(f"{key}: {value}\n" for key, value in (extra_metadata or {}).items())
    return f"""---
id: {entry_id}
origin: {origin}
category: {category}
{extra_yaml}principal_scope: user
status: {status}
target_hint: {target_hint}
sensitivity: {sensitivity}
created_at: {created_at}
source_refs:
{refs_yaml}
---

<explicit_memory id="{entry_id}" schema_version="explicit_memory.v1">
  <user_words>{escape(user_words)}</user_words>
  <normalized_memory>{escape(normalized_memory)}</normalized_memory>
  <why_it_matters>{escape(why_it_matters)}</why_it_matters>
  <source_refs>
{refs_xml}
  </source_refs>
</explicit_memory>
"""


def _append_manifest(path: Path, record: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _rebuild_memory_index(data_root: Path, agent_id: uuid.UUID | str) -> None:
    overlay_dir = explicit_overlay_dir(data_root, agent_id)
    overlay_dir.mkdir(parents=True, exist_ok=True)
    entries = load_explicit_overlay_entries(data_root, agent_id)
    lines = [
        "# Explicit Memory Overlay",
        "",
        "Immediate user-commanded memories awaiting T3 consolidation. Not accepted T3 truth.",
        "",
        "| ID | Status | Target | Category | Created | Preview |",
        "|----|--------|--------|----------|---------|---------|",
    ]
    for entry in entries:
        lines.append(
            f"| {entry.entry_id} | {entry.status} | {entry.target_hint} | {entry.category} | "
            f"{entry.created_at[:10] or '-'} | {_escape_table(_preview(entry.content, 90))} |"
        )
    (overlay_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_refs(raw: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    else:
        items = list(raw)
    return [str(item).strip() for item in items if str(item).strip()]


def _explicit_entry_id(*, created_at: str, category: str, content: str) -> str:
    digest = hashlib.sha256(f"{created_at}\0{category}\0{content}".encode("utf-8")).hexdigest()[:16]
    return f"explicit_{digest}"


def _extract_tag(markdown: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", markdown or "", re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _preview(content: str, max_chars: int = _SEARCH_PREVIEW_CHARS) -> str:
    compact = re.sub(r"\s+", " ", content or "").strip()
    return compact if len(compact) <= max_chars else compact[: max(20, max_chars - 3)].rstrip() + "..."


def _simple_score(query: str, content: str) -> float:
    query_terms = _term_set(query)
    content_terms = _term_set(content)
    if not query_terms or not content_terms:
        return 0.0
    return len(query_terms & content_terms) / len(query_terms)


def _term_set(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = {token for token in re.split(r"\W+", lowered) if token}
    cjk = {lowered[i : i + 2] for i in range(max(0, len(lowered) - 1)) if "\u4e00" <= lowered[i] <= "\u9fff"}
    return tokens | cjk


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _now() -> str:
    return datetime.now(UTC).isoformat()
