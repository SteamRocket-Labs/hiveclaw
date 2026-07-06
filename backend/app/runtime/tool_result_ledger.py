"""Tool result context-effect classification."""

from __future__ import annotations

from typing import Any


_EXTERNAL_REF_TOOLS = {
    "web_search",
    "web_fetch",
    "exa_search",
    "tavily_search",
    "firecrawl_fetch",
    "xcrawl_scrape",
    "read_document",
    "read_mcp_resource",
}
_READ_TOOLS = {"read_file", "fs_read"}


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _source_refs(tool_args: dict[str, Any], trace_metadata: dict[str, Any] | None) -> list[str]:
    refs: list[str] = []
    for key in ("evidence_refs", "truth_evidence_refs"):
        value = (trace_metadata or {}).get(key)
        if isinstance(value, list | tuple):
            refs.extend(str(item) for item in value)
        elif isinstance(value, str):
            refs.append(value)
    for key in ("path", "url", "query"):
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(f"{key}:{value.strip()}")
    return _unique(refs)


def _classify(tool_name: str, status: str, side_effects: dict[str, Any]) -> tuple[str, str]:
    if status in {"blocked_by_hook", "blocked"}:
        return "blocked", "none"
    if status in {"error", "cancelled"}:
        return status, "none"
    if side_effects.get("terminal_signal"):
        return "terminal_signal", "terminal_signal"
    if side_effects.get("new_messages"):
        return "context_injection", "conversation_injection"
    if tool_name in _READ_TOOLS:
        return "evidence", "file_read"
    if tool_name in _EXTERNAL_REF_TOOLS:
        return "evidence", "external_reference"
    if tool_name == "load_skill":
        return "state_change", "skill_activation"
    if tool_name == "tool_search":
        return "state_change", "tool_schema_activation"
    if any(token in tool_name for token in ("write", "edit", "delete", "send", "create", "append", "update")):
        return "state_change", "side_effect"
    return "message", "none"


def build_tool_result_ledger_entry(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    result_text: str,
    status: str,
    trace_metadata: dict[str, Any] | None = None,
    side_effects: dict[str, Any] | None = None,
    followup_activation_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_tool_name = str(tool_name or "").strip()
    clean_status = str(status or "ok").strip()
    side_effect_payload = dict(side_effects or {})
    result_kind, context_effect = _classify(clean_tool_name, clean_status, side_effect_payload)
    return {
        "schema": "hive.tool_result_ledger.v1",
        "tool_name": clean_tool_name,
        "status": clean_status,
        "result_kind": result_kind,
        "context_effect": context_effect,
        "result_chars": len(str(result_text or "")),
        "source_refs": _source_refs(tool_args, trace_metadata),
        "side_effects": side_effect_payload,
        "followup_activation_events": list(followup_activation_events or []),
    }


def append_tool_result_ledger_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 50) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_tool_result(entry, limit=limit)
