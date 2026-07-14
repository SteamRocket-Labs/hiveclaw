"""Unified two-plane read surface for memory tools and read models (Part H).

Replaces the legacy flat-T3 entry manifest as the backing for
``search_memory(scope=facts)`` / ``load_memory`` and the memory UI read
model: profile-plane ``###`` entries (anchored by ``<!-- id: ... -->``) plus
knowledge-plane pages, resolved through the reference index (short ids,
tombstone chains, archived evidence).
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from app.memory.t3_platform_gate import LEGACY_T3_FILES, PROFILE_PLANE_TARGETS

logger = logging.getLogger(__name__)

_ENTRY_HEADER_RE = re.compile(r"^###\s+(?P<heading>.+)$")
_ENTRY_ID_RE = re.compile(r"<!--\s*id:\s*(?P<entry_id>[A-Za-z0-9:_.-]+)\s*-->")
_PREVIEW_CHARS = 160
_LEGACY_ID_PREFIX = "legacy_t3:"


def list_profile_entries(data_root: Path | str, agent_id: uuid.UUID | str) -> list[dict]:
    """Enumerate profile-plane entries across self + profiles files."""
    mem_root = Path(data_root) / str(agent_id)
    entries: list[dict] = []
    for target in PROFILE_PLANE_TARGETS:
        path = mem_root / target
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Profile plane read failed for %s: %s", path, exc)
            continue
        entries.extend(_parse_entries(content, source=target))
    return entries


def list_knowledge_pages(data_root: Path | str, agent_id: uuid.UUID | str) -> list[dict]:
    """Enumerate knowledge/milestone pages with title/status/preview."""
    mem_dir = Path(data_root) / str(agent_id) / "memory"
    pages: list[dict] = []
    for subdir, kind in (("knowledge", "knowledge"), ("milestones", "milestone")):
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pages.append(
                {
                    "id": path.stem,
                    "kind": kind,
                    "source": f"memory/{subdir}/{path.name}",
                    "title": _frontmatter_field(content, "title") or path.stem,
                    "status": _frontmatter_field(content, "status") or "active",
                    "aliases": _frontmatter_list(content, "aliases"),
                    "tags": _frontmatter_list(content, "tags"),
                    "lifecycle": _frontmatter_field(content, "lifecycle")
                    or _frontmatter_field(content, "status")
                    or "active",
                    "content": content,
                    "preview": _preview(content),
                }
            )
    return pages


def list_t3_memory_documents(data_root: Path | str, agent_id: uuid.UUID | str) -> dict[str, str]:
    """Return the accepted T3 substrate as documents for Soul/Dream review.

    Normal mode reads the two-plane layout only. If an agent has not yet been
    migrated and no two-plane documents exist, legacy flat-T3 files are exposed
    with a `migration_required/` key and an in-band warning so the read is
    observable rather than a silent revival of the retired layout.
    """
    profile_entries = list_profile_entries(data_root, agent_id)
    pages = list_knowledge_pages(data_root, agent_id)
    documents: dict[str, str] = {}
    grouped: dict[str, list[str]] = {}
    for entry in profile_entries:
        grouped.setdefault(str(entry["source"]), []).append(str(entry["content"]))
    for source, chunks in sorted(grouped.items()):
        documents[source] = "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    for page in pages:
        source = str(page.get("source") or "")
        content = str(page.get("content") or "")
        if source and content.strip():
            documents[source] = content
    if documents:
        return documents

    legacy = _read_legacy_flat_t3_files(data_root, agent_id)
    if not legacy:
        return {}
    logger.warning(
        "Legacy flat-T3 corpus present for %s but two-plane migration is not applied; "
        "serving observable migration_required fallback",
        agent_id,
    )
    return {
        f"migration_required/{source}": _legacy_migration_required_document(source=source, content=content)
        for source, content in legacy.items()
    }


def search_plane_facts(
    data_root: Path | str,
    agent_id: uuid.UUID | str,
    query: str,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Return ranked two-plane candidates without hiding their full semantics.

    Ranking signals are observations. A count limit is applied only when the
    calling model explicitly supplies one.
    """
    needle = (query or "").strip().lower()
    results: list[dict] = []
    try:
        from app.memory.relation_graph import KNOWLEDGE_PAGE_DIRS
        from app.memory.wiki_retrieval import DEFAULT_WIKI_METHOD, search_wiki_pages

        for hit in search_wiki_pages(
            Path(data_root),
            _uuid(agent_id),
            query,
            method=DEFAULT_WIKI_METHOD,
            limit=limit,
            page_dirs=KNOWLEDGE_PAGE_DIRS,
        ):
            results.append(
                {
                    "id": str(hit.get("page_id") or "").rsplit("/", 1)[-1],
                    "category": str(hit.get("kind") or "knowledge"),
                    "content": str(hit.get("content") or hit.get("preview") or ""),
                    "preview": str(hit.get("preview") or ""),
                    "timestamp": "",
                    "source": str(hit.get("source_ref") or ""),
                    "metadata": {"sensitivity": "PL1_public"},
                }
            )
    except Exception as exc:  # noqa: BLE001 - retrieval degrade must not break the tool
        logger.warning("Knowledge-plane fact search failed: %s", exc)

    if needle:
        for entry in list_profile_entries(data_root, agent_id):
            haystack = f"{entry['heading']} {entry['content']}".lower()
            terms = [term for term in re.split(r"\W+", needle) if term]
            if terms and any(term in haystack for term in terms):
                results.append(
                    {
                        "id": entry["id"],
                        "category": entry["source"].rsplit("/", 2)[-2] if "/" in entry["source"] else "self",
                        "content": entry["content"],
                        "preview": _preview(entry["content"]),
                        "timestamp": "",
                        "source": entry["source"],
                        "metadata": {"sensitivity": "PL1_public"},
                    }
                )
    if not results and not _has_two_plane_documents(data_root, agent_id):
        results.extend(_search_legacy_flat_t3(data_root, agent_id, query, limit=limit))
    return results if limit is None else results[:limit]


def load_plane_entries(data_root: Path | str, agent_id: uuid.UUID | str, ids: list[str]) -> list[dict]:
    """Load full entries/pages by id: profile entry ids (tombstone-resolved),
    knowledge/milestone slugs, t2- evidence refs (even archived)."""
    from app.memory.reference_index import resolve_entry_id, resolve_memory_ref

    wanted = [str(entry_id).strip() for entry_id in ids if str(entry_id).strip()]
    if not wanted:
        return []
    root = Path(data_root)
    profile_entries = {entry["id"]: entry for entry in list_profile_entries(root, agent_id)}
    loaded: list[dict] = []
    for raw_id in wanted:
        resolved_id = resolve_entry_id(agent_id=agent_id, data_root=root, entry_id=raw_id)
        entry = profile_entries.get(resolved_id) or profile_entries.get(raw_id)
        if entry is not None:
            loaded.append(
                {
                    "id": entry["id"],
                    "category": "profile_plane",
                    "content": entry["content"],
                    "source": entry["source"],
                    "timestamp": "",
                }
            )
            continue
        legacy = _load_legacy_flat_t3_entry(root, agent_id, raw_id)
        if legacy is not None:
            loaded.append(legacy)
            continue
        ref = resolve_memory_ref(agent_id=agent_id, data_root=root, ref=raw_id)
        if ref is None or ref.kind == "explicit_entry":
            # explicit overlay entries ride their own lane (load/update/retire
            # explicit paths); resolving them here would misroute corrections.
            continue
        target = root / str(agent_id) / ref.path
        content = ""
        if target.is_dir():
            summary = target / "summary.md"
            content = summary.read_text(encoding="utf-8", errors="replace") if summary.exists() else ""
        elif target.exists():
            content = target.read_text(encoding="utf-8", errors="replace")
        if content:
            loaded.append(
                {
                    "id": raw_id,
                    "category": ref.kind,
                    "content": content,
                    "source": ref.path,
                    "timestamp": ref.archived_at,
                }
            )
    return loaded


def _has_two_plane_documents(data_root: Path | str, agent_id: uuid.UUID | str) -> bool:
    return bool(list_profile_entries(data_root, agent_id) or list_knowledge_pages(data_root, agent_id))


def _read_legacy_flat_t3_files(data_root: Path | str, agent_id: uuid.UUID | str) -> dict[str, str]:
    root = Path(data_root) / str(agent_id)
    found: dict[str, str] = {}
    for relative in LEGACY_T3_FILES:
        path = root / relative
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("Legacy flat-T3 read failed for %s: %s", path, exc)
            continue
        if content:
            found[relative] = content
    return found


def _legacy_migration_required_document(*, source: str, content: str) -> str:
    return (
        '<migration_required source="legacy_flat_t3">\n'
        f"The legacy flat-T3 corpus at {source} has not been migrated to the two-plane layout. "
        "Use this content only as compatibility evidence and schedule "
        "`python -m app.scripts.migrate_memory_two_planes --apply --confirm` before treating it as normal T3.\n"
        "</migration_required>\n\n"
        f"{content}"
    )


def _search_legacy_flat_t3(
    data_root: Path | str,
    agent_id: uuid.UUID | str,
    query: str,
    *,
    limit: int | None,
) -> list[dict]:
    terms = [term for term in re.split(r"\W+", (query or "").strip().lower()) if term]
    if not terms:
        return []
    hits: list[dict] = []
    for source, content in _read_legacy_flat_t3_files(data_root, agent_id).items():
        for index, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            haystack = line.lower()
            if not any(term in haystack for term in terms):
                continue
            hits.append(
                {
                    "id": f"{_LEGACY_ID_PREFIX}{source}#{index}",
                    "category": "legacy_flat_t3",
                    "content": line,
                    "preview": _preview(line),
                    "timestamp": "",
                    "source": source,
                    "metadata": {
                        "sensitivity": "PL1_public",
                        "legacy_t3": True,
                        "migration_required": True,
                    },
                }
            )
            if limit is not None and len(hits) >= limit:
                return hits
    return hits


def _load_legacy_flat_t3_entry(root: Path, agent_id: uuid.UUID | str, raw_id: str) -> dict | None:
    if not raw_id.startswith(_LEGACY_ID_PREFIX):
        return None
    payload = raw_id[len(_LEGACY_ID_PREFIX) :]
    source, _sep, line_raw = payload.partition("#")
    if not source or not line_raw.isdigit():
        return None
    path = root / str(agent_id) / source
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    line_number = int(line_raw)
    if line_number < 1 or line_number > len(lines):
        return None
    content = lines[line_number - 1].strip()
    if not content:
        return None
    return {
        "id": raw_id,
        "category": "legacy_flat_t3",
        "content": content,
        "source": source,
        "timestamp": "",
        "metadata": {
            "legacy_t3": True,
            "migration_required": True,
        },
    }


def _parse_entries(content: str, *, source: str) -> list[dict]:
    entries: list[dict] = []
    lines = (content or "").splitlines()
    current_heading: str | None = None
    current_start: int | None = None
    current_id: str | None = None
    for index, line in enumerate(lines + ["## _end"]):
        stripped = line.strip()
        header = _ENTRY_HEADER_RE.match(stripped)
        boundary = stripped.startswith("## ") or header is not None or index == len(lines)
        if boundary and current_heading is not None and current_id:
            body = "\n".join(lines[current_start:index]).strip()
            entry_metadata = _entry_activation_metadata(body)
            entries.append(
                {
                    "id": current_id,
                    "heading": current_heading,
                    "content": body,
                    "source": source,
                    "aliases": entry_metadata["aliases"],
                    "tags": entry_metadata["tags"],
                    "lifecycle": entry_metadata["lifecycle"],
                }
            )
            current_heading, current_start, current_id = None, None, None
        if header is not None:
            current_heading = header.group("heading").strip()
            current_start = index
            current_id = None
        elif current_heading is not None and current_id is None:
            id_match = _ENTRY_ID_RE.search(stripped)
            if id_match:
                current_id = id_match.group("entry_id")
    return entries


def _frontmatter_field(content: str, field: str) -> str:
    if not content.startswith("---") or content.count("---") < 2:
        return ""
    frontmatter = content.split("---", 2)[1]
    match = re.search(rf"^{re.escape(field)}:\s*(?P<value>.+)$", frontmatter, re.MULTILINE)
    return match.group("value").strip() if match else ""


def _frontmatter_list(content: str, field: str) -> list[str]:
    return _parse_list_value(_frontmatter_field(content, field))


def _entry_activation_metadata(body: str) -> dict[str, object]:
    fields: dict[str, str] = {}
    for line in (body or "").splitlines():
        match = re.match(r"^\s*-?\s*(aliases|tags|lifecycle):\s*(?P<value>.+?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group("value")
    return {
        "aliases": _parse_list_value(fields.get("aliases", "")),
        "tags": _parse_list_value(fields.get("tags", "")),
        "lifecycle": (fields.get("lifecycle") or "active").strip() or "active",
    }


def _parse_list_value(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item for part in raw.split(",") if (item := part.strip().strip("\"'"))]


def _preview(content: str) -> str:
    body = content
    if body.startswith("---") and body.count("---") >= 2:
        body = body.split("---", 2)[2]
    return " ".join(body.split())[:_PREVIEW_CHARS]


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def mark_profile_entry_promoted(
    data_root: Path | str,
    agent_id: uuid.UUID | str,
    *,
    entry_id: str,
    promoted_to: str,
    target: str,
) -> bool:
    """Stamp the 工序 6 two-way link on a profile entry after promotion.

    Platform bookkeeping (same class as the gate's retire marker and the T2
    absorbed stamp): appends `- 已固化 → [[<kind>-<target>]]` under the entry
    so the candidate marker stops qualifying while the narrative stays.
    """
    mem_root = Path(data_root) / str(agent_id)
    stamp = f"- 已固化 → [[{promoted_to}-{target}]]"
    for target_file in PROFILE_PLANE_TARGETS:
        path = mem_root / target_file
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        entries = _parse_entries(content, source=target_file)
        for entry in entries:
            if entry["id"] != entry_id:
                continue
            if stamp in entry["content"]:
                return True
            updated_entry = entry["content"].rstrip() + "\n" + stamp
            path.write_text(content.replace(entry["content"], updated_entry), encoding="utf-8")
            return True
    return False
