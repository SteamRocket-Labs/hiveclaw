"""Structured runtime query used by CCPlus activation routing.

`ActivationQuery` is the external-Q surface for memory, knowledge, skill, and
tool activation. It is a runtime data contract, not prompt text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

ACTIVATION_QUERY_SCHEMA = "hive.ccplus.activation_query.v1"
ACTIVATION_QUERY_REF_SCHEMA = "hive.ccplus.activation_query_ref.v1"
_URL_RE = re.compile(r"https?://[^\s<>\]\)）}，。；;]+")
_FILE_REF_RE = re.compile(
    r"(?<![\w/.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
    r"\.(?:md|py|ts|tsx|js|jsx|json|ya?ml|toml|sql|txt|csv|pdf|docx|xlsx|png|jpe?g)"
)
_TEMPORAL_TERMS = (
    "今天",
    "明天",
    "昨天",
    "刚刚",
    "现在",
    "上次",
    "之前",
    "today",
    "tomorrow",
    "yesterday",
    "now",
    "previous",
    "last time",
)
_HIGH_RISK_TERMS = (
    "delete",
    "drop",
    "rm -rf",
    "secret",
    "credential",
    "production",
    "prod",
    "泄漏",
    "删除",
    "生产",
    "密钥",
    "凭证",
)


def _text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(child) for child in value]
    return str(value)


def _dict_payload(value: Any) -> dict[str, Any]:
    return _json_safe(value) if isinstance(value, Mapping) else {}


def _dict_list(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(_dict_payload(item) for item in value if isinstance(item, Mapping))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list | tuple | set | frozenset):
        items = list(value)
    else:
        items = []
    return tuple(text for item in items if (text := _text(item)))


def activation_query_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable hash for a JSON-safe activation payload."""

    canonical = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def task_profile_to_activation_payload(profile: Any) -> dict[str, Any]:
    """Serialize a ContextBudget TaskProfile into the ActivationQuery shape."""

    if isinstance(profile, Mapping):
        name = profile.get("name")
        complexity = profile.get("complexity")
        execution_shape = profile.get("execution_shape")
        groups = profile.get("suggested_deferred_tool_group_names")
    else:
        name = getattr(profile, "name", "")
        complexity = getattr(profile, "complexity", "")
        execution_shape = getattr(profile, "execution_shape", "")
        groups = getattr(profile, "suggested_deferred_tool_group_names", ())
    return {
        "name": _text(name, fallback="general"),
        "complexity": _text(complexity, fallback="low"),
        "execution_shape": _text(execution_shape, fallback="direct"),
        "suggested_deferred_tool_group_names": list(_string_tuple(groups)),
    }


def _unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def parse_mechanical_activation_features(text: str) -> dict[str, Any]:
    """Extract zero-LLM Q features from the current prompt text."""

    prompt = str(text or "")
    urls = _unique_ordered([match.group(0).rstrip(".,;，。；") for match in _URL_RE.finditer(prompt)])
    files = _unique_ordered([match.group(0).rstrip(".,;，。；") for match in _FILE_REF_RE.finditer(prompt)])
    entities = [{"kind": "file", "value": value} for value in files]
    entities.extend({"kind": "url", "value": value} for value in urls)
    lowered = prompt.lower()
    temporal_hints = [
        {"kind": "relative", "value": term}
        for term in _TEMPORAL_TERMS
        if (term in prompt if any("\u4e00" <= char <= "\u9fff" for char in term) else term in lowered)
    ]
    risk_signals = [
        term for term in _HIGH_RISK_TERMS if (term in prompt if any("\u4e00" <= char <= "\u9fff" for char in term) else term in lowered)
    ]
    return {
        "entities": entities,
        "referenced_files": files,
        "temporal_hints": temporal_hints,
        "concepts": [],
        "risk_level": "high" if risk_signals else "low",
        "risk_signals": risk_signals,
    }


@dataclass(frozen=True, slots=True)
class ActivationQuery:
    """Versioned structured Q for per-turn activation.

    Fields intentionally stay broad and JSON-compatible because the query is
    shared by memory, retrieval, skill, and tool rankers.
    """

    SCHEMA: ClassVar[str] = ACTIVATION_QUERY_SCHEMA

    raw_prompt: str
    session_id: str
    turn_id: str
    intent_id: str
    agent_id: str = ""
    agent_role: str = ""
    owner_context: dict[str, Any] = field(default_factory=dict)
    task_profile: dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    entities: tuple[dict[str, Any], ...] = ()
    concepts: tuple[str, ...] = ()
    temporal_hints: tuple[dict[str, Any], ...] = ()
    referenced_files: tuple[str, ...] = ()
    risk_level: str = "unknown"
    memory_need: str = "unknown"
    knowledge_need: str = "unknown"
    skill_need: str = "unknown"
    tool_need: str = "unknown"
    candidate_lanes: tuple[str, ...] = ()
    budget_policy: dict[str, Any] = field(default_factory=dict)
    parse_trace: tuple[dict[str, Any], ...] = ()
    query_id: str = ""

    def __post_init__(self) -> None:
        normalized = {
            "raw_prompt": _text(self.raw_prompt),
            "session_id": _text(self.session_id),
            "turn_id": _text(self.turn_id),
            "intent_id": _text(self.intent_id),
            "agent_id": _text(self.agent_id),
            "agent_role": _text(self.agent_role),
            "owner_context": _dict_payload(self.owner_context),
            "task_profile": _dict_payload(self.task_profile),
            "intent": _text(self.intent),
            "entities": _dict_list(self.entities),
            "concepts": _string_tuple(self.concepts),
            "temporal_hints": _dict_list(self.temporal_hints),
            "referenced_files": _string_tuple(self.referenced_files),
            "risk_level": _text(self.risk_level, fallback="unknown"),
            "memory_need": _text(self.memory_need, fallback="unknown"),
            "knowledge_need": _text(self.knowledge_need, fallback="unknown"),
            "skill_need": _text(self.skill_need, fallback="unknown"),
            "tool_need": _text(self.tool_need, fallback="unknown"),
            "candidate_lanes": _string_tuple(self.candidate_lanes),
            "budget_policy": _dict_payload(self.budget_policy),
            "parse_trace": _dict_list(self.parse_trace),
        }
        for key, value in normalized.items():
            object.__setattr__(self, key, value)
        if not _text(self.query_id):
            object.__setattr__(self, "query_id", f"aq:{activation_query_hash(normalized)[:24]}")
        else:
            object.__setattr__(self, "query_id", _text(self.query_id))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_QUERY_SCHEMA,
            "query_id": self.query_id,
            "raw_prompt": self.raw_prompt,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "owner_context": dict(self.owner_context),
            "task_profile": dict(self.task_profile),
            "intent": self.intent,
            "entities": [dict(item) for item in self.entities],
            "concepts": list(self.concepts),
            "temporal_hints": [dict(item) for item in self.temporal_hints],
            "referenced_files": list(self.referenced_files),
            "risk_level": self.risk_level,
            "memory_need": self.memory_need,
            "knowledge_need": self.knowledge_need,
            "skill_need": self.skill_need,
            "tool_need": self.tool_need,
            "candidate_lanes": list(self.candidate_lanes),
            "budget_policy": dict(self.budget_policy),
            "parse_trace": [dict(item) for item in self.parse_trace],
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationQuery":
        if manifest.get("schema") != ACTIVATION_QUERY_SCHEMA:
            raise ValueError(f"Unsupported activation query schema: {manifest.get('schema')!r}")
        return cls(
            query_id=_text(manifest.get("query_id")),
            raw_prompt=_text(manifest.get("raw_prompt")),
            session_id=_text(manifest.get("session_id")),
            turn_id=_text(manifest.get("turn_id")),
            intent_id=_text(manifest.get("intent_id")),
            agent_id=_text(manifest.get("agent_id")),
            agent_role=_text(manifest.get("agent_role")),
            owner_context=_dict_payload(manifest.get("owner_context")),
            task_profile=_dict_payload(manifest.get("task_profile")),
            intent=_text(manifest.get("intent")),
            entities=_dict_list(manifest.get("entities")),
            concepts=_string_tuple(manifest.get("concepts")),
            temporal_hints=_dict_list(manifest.get("temporal_hints")),
            referenced_files=_string_tuple(manifest.get("referenced_files")),
            risk_level=_text(manifest.get("risk_level"), fallback="unknown"),
            memory_need=_text(manifest.get("memory_need"), fallback="unknown"),
            knowledge_need=_text(manifest.get("knowledge_need"), fallback="unknown"),
            skill_need=_text(manifest.get("skill_need"), fallback="unknown"),
            tool_need=_text(manifest.get("tool_need"), fallback="unknown"),
            candidate_lanes=_string_tuple(manifest.get("candidate_lanes")),
            budget_policy=_dict_payload(manifest.get("budget_policy")),
            parse_trace=_dict_list(manifest.get("parse_trace")),
        )


def build_activation_query_ref(query: ActivationQuery | Mapping[str, Any]) -> dict[str, Any]:
    activation_query = query if isinstance(query, ActivationQuery) else ActivationQuery.from_manifest(query)
    manifest = activation_query.to_manifest()
    return {
        "schema": ACTIVATION_QUERY_REF_SCHEMA,
        "kind": "activation_query",
        "query_id": activation_query.query_id,
        "session_id": activation_query.session_id,
        "turn_id": activation_query.turn_id,
        "intent_id": activation_query.intent_id,
        "content_hash": activation_query_hash(manifest),
    }


__all__ = [
    "ACTIVATION_QUERY_REF_SCHEMA",
    "ACTIVATION_QUERY_SCHEMA",
    "ActivationQuery",
    "activation_query_hash",
    "build_activation_query_ref",
    "parse_mechanical_activation_features",
    "task_profile_to_activation_payload",
]
