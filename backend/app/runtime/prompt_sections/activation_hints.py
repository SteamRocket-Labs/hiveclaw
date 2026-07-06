"""§ Activation hints — lightweight actions from selected non-memory candidates."""

from __future__ import annotations

from typing import Any


def _manifest(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_manifest"):
        value = value.to_manifest()
    return dict(value) if isinstance(value, dict) else {}


def _top_candidates(router_output: Any) -> list[dict[str, Any]]:
    output = _manifest(router_output)
    raw = output.get("top_activation_candidates") if output else router_output
    if not isinstance(raw, list | tuple):
        return []
    return [_manifest(candidate) for candidate in raw if _manifest(candidate)]


def _first_feature(candidate: dict[str, Any], key: str, fallback: str = "") -> str:
    features = candidate.get("key_features") if isinstance(candidate.get("key_features"), dict) else {}
    value = features.get(key)
    if isinstance(value, str):
        text = value.strip()
        return text or fallback
    if isinstance(value, list | tuple):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    return fallback


def _value_pointer(candidate: dict[str, Any]) -> dict[str, Any]:
    value = candidate.get("value_pointer")
    return dict(value) if isinstance(value, dict) else {}


def _candidate_label(candidate: dict[str, Any], fallback: str) -> str:
    return _first_feature(candidate, "name", fallback=fallback)


def _skill_hint(candidate: dict[str, Any]) -> str:
    pointer = _value_pointer(candidate)
    name = str(pointer.get("name") or _candidate_label(candidate, "skill")).strip()
    return f"- Skill `{name}`: use `load_skill` with `{name}` when the turn needs this method."


def _tool_hint(candidate: dict[str, Any]) -> str:
    pointer = _value_pointer(candidate)
    name = str(pointer.get("tool_name") or _candidate_label(candidate, "tool")).strip()
    selector = str(pointer.get("selector") or f"select:{name}").strip()
    return f"- Tool `{name}`: use `tool_search` with `{selector}` before calling the deferred schema."


def _subagent_hint(candidate: dict[str, Any]) -> str:
    pointer = _value_pointer(candidate)
    name = _candidate_label(candidate, "subagent")
    subagent_type = str(pointer.get("subagent_type") or "general-purpose").strip()
    definition = str(pointer.get("definition_name") or "").strip()
    suffix = f" definition `{definition}`" if definition else ""
    return f"- Subagent `{name}`: use `spawn_subagent` type `{subagent_type}`{suffix}."


def build_activation_hints_section(router_output: Any, *, max_hints: int = 12) -> str:
    lines: list[str] = []
    for candidate in _top_candidates(router_output):
        kind = str(candidate.get("candidate_kind") or "").strip()
        pointer = _value_pointer(candidate)
        loader = str(pointer.get("loader") or "").strip()
        if kind == "skill" or loader == "load_skill":
            lines.append(_skill_hint(candidate))
        elif kind == "tool" or loader == "tool_search":
            lines.append(_tool_hint(candidate))
        elif kind == "subagent" or loader == "spawn_subagent":
            lines.append(_subagent_hint(candidate))
        if len(lines) >= max(1, int(max_hints or 12)):
            break
    if not lines:
        return ""
    return "## Activation Hints\n\n" + "\n".join(lines)


__all__ = ["build_activation_hints_section"]
