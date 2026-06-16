"""Structured file-backed session continuity artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import get_settings


_FUTURE_PREFIXES = (
    "next ",
    "next i ",
    "i will ",
    "i'll ",
    "then ",
    "after that ",
    "下一步",
    "接下来",
    "然后",
    "之后",
    "我会",
    "我将",
)
_SESSION_MEMORY_VERSION = 2
_MAX_FILES_AND_FUNCTIONS = 12
_MAX_PENDING_WORK = 10
_MAX_WORKLOG_ITEMS = 20
_MAX_WORKLOG_ITEM_CHARS = 200
_SECTION_SPECS: tuple[tuple[str, str, str], ...] = (
    ("Session Title", "session_title", "text"),
    ("Current State", "current_state", "text"),
    ("Task Specification", "task_spec", "text"),
    ("Files and Functions", "important_files", "list"),
    ("Workflow", "workflow", "list"),
    ("Errors & Corrections", "errors_corrections", "list"),
    ("Key Results", "key_results", "list"),
    ("Pending Work", "pending_work", "list"),
    ("Worklog", "worklog", "list"),
)
_LEGACY_SECTION_ALIASES = {
    "Task Spec": "Task Specification",
    "Important Files / Artifacts": "Files and Functions",
    "Errors / Corrections": "Errors & Corrections",
}
_SECTION_PRIORITY = (
    "Current State",
    "Pending Work",
    "Key Results",
    "Task Specification",
    "Files and Functions",
    "Errors & Corrections",
    "Workflow",
    "Worklog",
    "Session Title",
)


@dataclass(slots=True)
class SessionMemoryPayload:
    session_id: str = ""
    source: str = ""
    session_title: str = ""
    current_state: str = ""
    task_spec: str = ""
    important_files: list[str] = field(default_factory=list)
    workflow: list[str] = field(default_factory=list)
    errors_corrections: list[str] = field(default_factory=list)
    key_results: list[str] = field(default_factory=list)
    pending_work: list[str] = field(default_factory=list)
    last_successful_step: str = ""
    worklog: list[str] = field(default_factory=list)
    updated_at: str | None = None
    compaction_count: int = 0
    last_compaction_at: str | None = None


def _resolve_data_root(data_root: str | Path | None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(get_settings().AGENT_DATA_DIR)


def _agent_dir(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    agent_dir = _resolve_data_root(data_root) / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


def _runtime_artifacts_dir(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    runtime_dir = _agent_dir(agent_id, data_root=data_root) / "runtime_artifacts"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _legacy_workspace_dir(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    return _agent_dir(agent_id, data_root=data_root) / "workspace"


def get_session_memory_path(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    return _runtime_artifacts_dir(agent_id, data_root=data_root) / "session_memory.md"


def get_compaction_summary_path(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    return _runtime_artifacts_dir(agent_id, data_root=data_root) / "compaction_summary.md"


def _legacy_session_memory_path(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    return _legacy_workspace_dir(agent_id, data_root=data_root) / "session_memory.md"


def _legacy_compaction_summary_path(agent_id: UUID, *, data_root: str | Path | None = None) -> Path:
    return _legacy_workspace_dir(agent_id, data_root=data_root) / "compaction_summary.md"


def _remove_legacy_runtime_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _render_text_block(text: str) -> str:
    return text.strip() if text and text.strip() else "- (none)"


def _normalize_list(items: list[str], *, limit: int | None = None, item_char_limit: int | None = None) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        if not item or not str(item).strip():
            continue
        normalized = str(item).strip()
        if item_char_limit is not None and len(normalized) > item_char_limit:
            normalized = normalized[:item_char_limit].rstrip()
        cleaned.append(normalized)
    if limit is not None:
        cleaned = cleaned[-limit:]
    return cleaned


def _render_list_block(items: list[str]) -> str:
    cleaned = _normalize_list(items)
    if not cleaned:
        return "- (none)"
    return "\n".join(f"- {item}" for item in cleaned)


def _derive_session_title(task_spec: str, current_state: str) -> str:
    candidate = task_spec.strip() or current_state.strip()
    if not candidate:
        return "Session Continuity"
    candidate = candidate.splitlines()[0].strip()
    return candidate[:96].rstrip()


def _render_frontmatter(payload: SessionMemoryPayload) -> str:
    updated_at = payload.updated_at or datetime.now(timezone.utc).isoformat()
    lines = [
        "---",
        f"version: {_SESSION_MEMORY_VERSION}",
        f"updated_at: {updated_at}",
        f"session_id: {payload.session_id or ''}",
        f"compaction_count: {payload.compaction_count}",
        f"last_compaction_at: {payload.last_compaction_at or ''}",
        f"source: {payload.source or ''}",
        "---",
        "",
    ]
    return "\n".join(lines)


def render_session_memory(payload: SessionMemoryPayload) -> str:
    lines = [_render_frontmatter(payload), "# Session Memory", ""]
    for title, attr_name, kind in _SECTION_SPECS:
        value = getattr(payload, attr_name)
        if kind == "list":
            body = _render_list_block(value)
        else:
            body = _render_text_block(value)
        lines.extend([f"## {title}", body, ""])
    if payload.last_successful_step.strip():
        lines.extend(["## Last Successful Step", _render_text_block(payload.last_successful_step), ""])
    return "\n".join(lines).rstrip() + "\n"


def render_session_memory_excerpt(payload: SessionMemoryPayload, *, budget_chars: int = 5000) -> str:
    if budget_chars <= 0:
        return ""
    blocks: dict[str, str] = {}
    for title, attr_name, kind in _SECTION_SPECS:
        value = getattr(payload, attr_name)
        body = _render_list_block(value) if kind == "list" else _render_text_block(value)
        if body == "- (none)":
            continue
        blocks[title] = f"## {title}\n{body}"
    if payload.last_successful_step.strip():
        blocks["Last Successful Step"] = f"## Last Successful Step\n{_render_text_block(payload.last_successful_step)}"

    parts: list[str] = []
    used = 0
    for title in _SECTION_PRIORITY:
        block = blocks.get(title)
        if not block:
            continue
        block_cost = len(block) + (2 if parts else 0)
        if used + block_cost > budget_chars:
            continue
        parts.append(block)
        used += block_cost
    return "\n\n".join(parts)


def update_session_memory(
    agent_id: UUID,
    payload: SessionMemoryPayload,
    *,
    data_root: str | Path | None = None,
) -> Path:
    path = get_session_memory_path(agent_id, data_root=data_root)
    normalized = SessionMemoryPayload(
        session_id=payload.session_id.strip(),
        source=payload.source.strip(),
        session_title=(payload.session_title or _derive_session_title(payload.task_spec, payload.current_state)).strip(),
        current_state=payload.current_state.strip(),
        task_spec=payload.task_spec.strip(),
        important_files=_normalize_list(payload.important_files, limit=_MAX_FILES_AND_FUNCTIONS),
        workflow=_normalize_list(payload.workflow),
        errors_corrections=_normalize_list(payload.errors_corrections),
        key_results=_normalize_list(payload.key_results),
        pending_work=_normalize_list(payload.pending_work, limit=_MAX_PENDING_WORK),
        last_successful_step=payload.last_successful_step.strip(),
        worklog=_normalize_list(
            payload.worklog,
            limit=_MAX_WORKLOG_ITEMS,
            item_char_limit=_MAX_WORKLOG_ITEM_CHARS,
        ),
        updated_at=payload.updated_at or datetime.now(timezone.utc).isoformat(),
        compaction_count=max(int(payload.compaction_count or 0), 0),
        last_compaction_at=(payload.last_compaction_at or "").strip() or None,
    )
    path.write_text(render_session_memory(normalized), encoding="utf-8")
    _remove_legacy_runtime_file(_legacy_session_memory_path(agent_id, data_root=data_root))
    return path


def write_compaction_summary(
    agent_id: UUID,
    summary: str,
    *,
    original_message_count: int | None = None,
    kept_message_count: int | None = None,
    data_root: str | Path | None = None,
) -> Path:
    path = get_compaction_summary_path(agent_id, data_root=data_root)
    lines = [
        "# Session Compaction Summary",
        f"Updated At: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    if original_message_count is not None or kept_message_count is not None:
        lines.append(
            f"- Original Messages: {original_message_count if original_message_count is not None else '?'}"
        )
        lines.append(f"- Kept Messages: {kept_message_count if kept_message_count is not None else '?'}")
        lines.append("")
    lines.append(summary.strip() or "(empty)")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    _remove_legacy_runtime_file(_legacy_compaction_summary_path(agent_id, data_root=data_root))
    return path


def _parse_section_map(content: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_title: str | None = None
    for raw_line in content.splitlines():
        if raw_line.startswith("## "):
            current_title = _LEGACY_SECTION_ALIASES.get(raw_line[3:].strip(), raw_line[3:].strip())
            sections[current_title] = []
            continue
        if current_title is not None:
            sections[current_title].append(raw_line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def _parse_list_block(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "- (none)":
            continue
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---\n"):
        return {}, content
    try:
        _, remainder = content.split("---\n", 1)
    except ValueError:
        return {}, content
    if "\n---\n" not in remainder:
        return {}, content
    frontmatter_text, body = remainder.split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def _legacy_updated_at(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("Updated At: "):
            return line.removeprefix("Updated At: ").strip()
    return None


def load_session_memory(agent_id: UUID, *, data_root: str | Path | None = None) -> SessionMemoryPayload | None:
    path = get_session_memory_path(agent_id, data_root=data_root)
    if not path.exists():
        legacy_path = _legacy_session_memory_path(agent_id, data_root=data_root)
        if legacy_path.exists():
            path = legacy_path
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(content)
    sections = _parse_section_map(body if metadata else content)
    updated_at = metadata.get("updated_at") or _legacy_updated_at(content.splitlines())
    return SessionMemoryPayload(
        session_id=metadata.get("session_id", "").strip(),
        source=metadata.get("source", "").strip(),
        session_title=sections.get("Session Title", "").replace("- (none)", "").strip(),
        current_state=sections.get("Current State", "").replace("- (none)", "").strip(),
        task_spec=sections.get("Task Specification", sections.get("Task Spec", "")).replace("- (none)", "").strip(),
        important_files=_normalize_list(
            _parse_list_block(sections.get("Files and Functions", sections.get("Important Files / Artifacts", ""))),
            limit=_MAX_FILES_AND_FUNCTIONS,
        ),
        workflow=_parse_list_block(sections.get("Workflow", "")),
        errors_corrections=_parse_list_block(
            sections.get("Errors & Corrections", sections.get("Errors / Corrections", ""))
        ),
        key_results=_parse_list_block(sections.get("Key Results", "")),
        pending_work=_parse_list_block(sections.get("Pending Work", "")),
        last_successful_step=sections.get("Last Successful Step", "").replace("- (none)", "").strip(),
        worklog=_normalize_list(
            _parse_list_block(sections.get("Worklog", "")),
            limit=_MAX_WORKLOG_ITEMS,
            item_char_limit=_MAX_WORKLOG_ITEM_CHARS,
        ),
        updated_at=updated_at,
        compaction_count=int(metadata.get("compaction_count") or 0),
        last_compaction_at=metadata.get("last_compaction_at") or None,
    )


def load_session_memory_text(agent_id: UUID, *, data_root: str | Path | None = None) -> str:
    payload = load_session_memory(agent_id, data_root=data_root)
    if payload is None:
        return ""
    return render_session_memory(payload)


def _looks_like_future_step(text: str) -> bool:
    normalized = text.strip().lower()
    return any(normalized.startswith(prefix.lower()) for prefix in _FUTURE_PREFIXES)


def build_session_memory_payload_from_messages(
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> SessionMemoryPayload:
    metadata = metadata or {}
    user_messages = [
        str(message.get("content", "")).strip()
        for message in messages
        if message.get("role") == "user" and str(message.get("content", "")).strip()
    ]
    assistant_messages = [
        str(message.get("content", "")).strip()
        for message in messages
        if message.get("role") == "assistant" and str(message.get("content", "")).strip()
    ]
    future_steps = [message for message in assistant_messages if _looks_like_future_step(message)]
    current_candidates = [message for message in assistant_messages if not _looks_like_future_step(message)]
    current_state = current_candidates[-1] if current_candidates else (assistant_messages[-1] if assistant_messages else "")
    workflow = [str(item).strip() for item in metadata.get("workflow", []) if str(item).strip()]
    key_results = [str(item).strip() for item in metadata.get("key_results", []) if str(item).strip()]
    worklog = [
        f"{str(message.get('role', 'unknown')).upper()}: {str(message.get('content', '')).strip()}"
        for message in messages
        if str(message.get("content", "")).strip()
    ]
    return SessionMemoryPayload(
        session_id=str(metadata.get("session_id") or metadata.get("conversation_id") or "").strip(),
        source=str(metadata.get("source") or metadata.get("event") or "").strip(),
        session_title=str(metadata.get("session_title") or _derive_session_title(user_messages[0] if user_messages else "", current_state)).strip(),
        current_state=current_state,
        task_spec=str(metadata.get("task_spec") or (user_messages[0] if user_messages else "")).strip(),
        important_files=_normalize_list(
            [str(item).strip() for item in metadata.get("important_files", []) if str(item).strip()],
            limit=_MAX_FILES_AND_FUNCTIONS,
        ),
        workflow=workflow,
        errors_corrections=_normalize_list(
            [str(item).strip() for item in metadata.get("errors_corrections", []) if str(item).strip()]
        ),
        key_results=key_results,
        pending_work=[
            str(item).strip()
            for item in (metadata.get("pending_work") or future_steps)
            if str(item).strip()
        ],
        last_successful_step=str(metadata.get("last_successful_step") or current_state).strip(),
        worklog=_normalize_list(worklog, limit=_MAX_WORKLOG_ITEMS, item_char_limit=_MAX_WORKLOG_ITEM_CHARS),
        compaction_count=int(metadata.get("compaction_count") or 0),
        last_compaction_at=str(metadata.get("last_compaction_at") or "").strip() or None,
    )
