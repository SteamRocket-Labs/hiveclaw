from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClaimStatus(StrEnum):
    VERIFIED = "verified"
    INFERRED = "inferred"
    CONTRADICTED = "contradicted"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


class SourceType(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DATASET = "dataset"
    REGULATORY = "regulatory"
    TECHNICAL = "technical"
    UNKNOWN = "unknown"


RunStatus = Literal["pending", "running", "completed", "failed", "killed"]


@dataclass(slots=True)
class SearchQuery:
    query: str
    lane_id: str = ""
    rationale: str = ""


@dataclass(slots=True)
class ResearchLane:
    lane_id: str
    label: str
    goal: str
    queries: list[SearchQuery] = field(default_factory=list)
    preferred_source_types: list[SourceType] = field(default_factory=list)


@dataclass(slots=True)
class ResearchPlan:
    question: str
    mode: str
    lanes: list[ResearchLane]
    scope: str = ""
    time_window: str = ""
    source_policy: str = "primary_preferred"
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ResearchRequest:
    question: str
    mode: str = "topic_deep_dive"
    scope: str = ""
    depth: str = "standard"
    source_policy: str = "primary_preferred"
    time_window: str = ""
    max_rounds: int = 4
    max_sources: int = 8
    concurrency: int = 4
    token_budget: int | None = None
    deadline_seconds: int | None = None
    output_format: str = "markdown"
    output_language: str = ""
    plan_confirmed: bool = False
    worker_topics: list[str] = field(default_factory=list)
    approved_plan: dict[str, Any] = field(default_factory=dict)
    controller_mode: bool = False

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> "ResearchRequest":
        question = str(arguments.get("question") or arguments.get("query") or "").strip()
        if not question:
            raise ValueError("deep research requires a non-empty question")
        depth = str(arguments.get("depth") or "standard").strip() or "standard"
        max_rounds = _coerce_int(arguments.get("max_rounds"), default=4, minimum=1, maximum=12)
        if depth in {"full", "flagship", "deep"}:
            max_rounds = max(max_rounds, 6)
        deadline_seconds = _coerce_optional_int(arguments.get("deadline_seconds"), minimum=10)
        if deadline_seconds is None:
            # P3: a per-worker timeout backstop so a stuck worker cannot hang the whole run.
            deadline_seconds = {
                "quick": 240,
                "light": 240,
                "standard": 360,
                "full": 600,
                "flagship": 720,
                "deep": 600,
            }.get(depth, 360)
        worker_topics = _coerce_topic_list(arguments.get("worker_topics"))
        approved_plan = _coerce_mapping(arguments.get("approved_plan") or arguments.get("runtime_contract"))
        if not worker_topics:
            worker_topics = _topics_from_approved_plan(approved_plan)
        output_format = (
            str(
                arguments.get("output_format") or _output_format_from_approved_plan(approved_plan) or "markdown"
            ).strip()
            or "markdown"
        )
        # F1 (RC1): the source budget must scale with depth, otherwise a deep run collapses
        # 60+ fetched sources down to a handful and starves most research lanes.
        max_sources_default = {
            "quick": 6,
            "light": 6,
            "standard": 12,
            "full": 30,
            "flagship": 40,
            "deep": 30,
        }.get(depth, 12)
        return cls(
            question=question,
            mode=str(arguments.get("mode") or "topic_deep_dive").strip() or "topic_deep_dive",
            scope=str(arguments.get("scope") or "").strip(),
            depth=depth,
            source_policy=str(arguments.get("source_policy") or "primary_preferred").strip() or "primary_preferred",
            time_window=str(arguments.get("time_window") or "").strip(),
            max_rounds=max_rounds,
            max_sources=_coerce_int(arguments.get("max_sources"), default=max_sources_default, minimum=1, maximum=60),
            concurrency=_coerce_int(arguments.get("concurrency"), default=4, minimum=1, maximum=12),
            token_budget=_coerce_optional_int(arguments.get("token_budget"), minimum=1),
            deadline_seconds=deadline_seconds,
            output_format=output_format,
            output_language=str(arguments.get("output_language") or "").strip(),
            plan_confirmed=bool(arguments.get("user_confirmed") or arguments.get("plan_confirmed") or False),
            worker_topics=worker_topics,
            approved_plan=approved_plan,
            controller_mode=bool(arguments.get("controller_mode") or False),
        )


@dataclass(slots=True)
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""
    lane_id: str = ""
    query: str = ""
    rank: int = 0
    discovery_only: bool = True


@dataclass(slots=True)
class SourceRecord:
    source_id: str
    url: str
    title: str
    publisher: str
    source_type: SourceType
    content: str
    fetched_at: str = field(default_factory=utc_now)
    published_at: str | None = None
    lane_id: str = ""
    query: str = ""
    fetch_tool: str = ""
    evidence_tier: str = ""
    evidence_grade: str = ""


@dataclass(slots=True)
class ClaimRecord:
    claim_id: str
    text: str
    status: ClaimStatus
    source_ids: list[str] = field(default_factory=list)
    evidence: str = ""
    notes: str = ""
    contradiction_group: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ResearchStep:
    step_id: str
    phase: str
    status: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class EvaluationResult:
    quality_gates: dict[str, str]
    gaps: list[str] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkerResult:
    topic: str
    intermediate_report: str
    sources: list[SourceRecord] = field(default_factory=list)
    citation_map: dict[str, str] = field(default_factory=dict)
    status: Literal["ok", "failed", "cancelled"] = "ok"
    error: str = ""
    tokens_used: int = 0
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class OrchestratorDecision:
    stop: bool
    topics: list[str] = field(default_factory=list)
    rationale: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ResearchArtifact:
    artifact_dir: str
    report_path: str
    sources_path: str
    claims_path: str
    steps_path: str
    final_path: str


@dataclass(slots=True)
class ResearchRun:
    run_id: str
    status: RunStatus
    summary: str = ""
    artifact_dir: str = ""
    report_path: str = ""
    sources_path: str = ""
    claims_path: str = ""
    steps_path: str = ""
    final_path: str = ""
    source_count: int = 0
    claim_count: int = 0
    quality_gates: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def to_jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


def _coerce_topic_list(raw: Any) -> list[str]:
    """Robustly coerce the ``worker_topics`` tool argument into a clean ``list[str]``.

    Model tool calls sometimes pass a JSON-serialized string (e.g. the plan lanes
    array) instead of a JSON array. Iterating a string yields one character per
    item, which silently corrupts the run — every worker then receives a single
    punctuation character ("[", "{", ...) as its topic. So: parse JSON strings,
    lift ``topic``/``label`` out of dict items, and drop blanks.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return [text]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        text = str(raw).strip()
        return [text] if text else []
    topics: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("topic") or item.get("label") or item.get("focus") or item.get("description") or ""
        else:
            value = item
        text = str(value or "").strip()
        if text:
            topics.append(text)
    return topics


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _topics_from_approved_plan(approved_plan: dict[str, Any]) -> list[str]:
    research = approved_plan.get("research") if isinstance(approved_plan.get("research"), dict) else {}
    lanes = research.get("lanes") if isinstance(research.get("lanes"), list) else []
    topics: list[str] = []
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        topic = str(lane.get("worker_topic") or lane.get("goal") or lane.get("label") or "").strip()
        if topic:
            topics.append(topic)
    return topics


def _output_format_from_approved_plan(approved_plan: dict[str, Any]) -> str:
    output = approved_plan.get("output") if isinstance(approved_plan.get("output"), dict) else {}
    primary = str(output.get("primary_format") or "").strip()
    if primary:
        return primary
    requested = output.get("requested_formats")
    if isinstance(requested, list) and requested:
        return str(requested[0] or "").strip()
    return ""


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_optional_int(value: Any, *, minimum: int) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, parsed)
