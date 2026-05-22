"""Durable understanding store (§10.3, §16.1).

A first-class home for relationship-shaped knowledge:

    subject — relation_type → object

with evidence_refs, confidence, last_confirmed_at, contradiction_history,
open_questions, and boundaries. Entries persist as a YAML-fronted
`understandings.md` so owners can review them alongside T3 memory files.

This module is intentionally storage-light: it owns the file format and the
in-memory contradiction graph, but does not yet wire into retriever or
dream. Those integrations are gradual; the store is the substrate.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ENTRY_RE = re.compile(
    r"<!-- understanding\n(?P<frontmatter>.*?)\n-->\n(?P<body>.*?)(?=(?:\n<!-- understanding\n)|\Z)",
    re.DOTALL,
)

_DEFAULT_DECAY_DAYS = 30
_DEFAULT_DECAY_RATE_PER_DAY = 0.01


@dataclass(slots=True)
class UnderstandingEntry:
    entry_id: str
    subject: str
    object_: str
    relation_type: str
    current_understanding: str
    evidence_refs: list[str]
    confidence: float
    last_confirmed_at: datetime
    contradiction_history: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)


class UnderstandingStore:
    """Markdown-backed understanding store.

    `record()` creates a new entry. `contradict()` introduces a contradicting
    entry while keeping the original visible (down-ranked confidence + linked
    via `contradiction_history`). `query()` filters by subject / object /
    relation. `decayed_confidence()` returns a stale-window-adjusted view of
    confidence without mutating the persisted value.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._base_dir / "understandings.md"
        self._entries: dict[str, UnderstandingEntry] = {}
        self._load()

    def record(
        self,
        *,
        subject: str,
        object_: str,
        relation_type: str,
        current_understanding: str,
        evidence_refs: list[str] | None = None,
        confidence: float = 0.5,
        open_questions: list[str] | None = None,
        boundaries: list[str] | None = None,
    ) -> UnderstandingEntry:
        entry = UnderstandingEntry(
            entry_id=uuid.uuid4().hex,
            subject=subject.strip(),
            object_=object_.strip(),
            relation_type=relation_type.strip(),
            current_understanding=current_understanding.strip(),
            evidence_refs=list(evidence_refs or []),
            confidence=float(confidence),
            last_confirmed_at=datetime.now(timezone.utc),
            open_questions=list(open_questions or []),
            boundaries=list(boundaries or []),
        )
        self._entries[entry.entry_id] = entry
        self._flush()
        return entry

    def get(self, entry_id: str) -> UnderstandingEntry | None:
        return self._entries.get(entry_id)

    def contradict(
        self,
        *,
        entry_id: str,
        new_understanding: str,
        evidence_refs: list[str] | None = None,
        confidence: float = 0.5,
    ) -> UnderstandingEntry:
        original = self._entries.get(entry_id)
        if original is None:
            raise KeyError(f"Unknown understanding entry: {entry_id}")
        new_entry = UnderstandingEntry(
            entry_id=uuid.uuid4().hex,
            subject=original.subject,
            object_=original.object_,
            relation_type=original.relation_type,
            current_understanding=new_understanding.strip(),
            evidence_refs=list(evidence_refs or []),
            confidence=float(confidence),
            last_confirmed_at=datetime.now(timezone.utc),
            contradiction_history=[original.entry_id],
        )
        original.contradiction_history = list({*original.contradiction_history, new_entry.entry_id})
        original.confidence = max(0.0, original.confidence * 0.5)
        self._entries[new_entry.entry_id] = new_entry
        self._flush()
        return new_entry

    def query(
        self,
        *,
        subject: str | None = None,
        object_: str | None = None,
        relation_type: str | None = None,
    ) -> list[UnderstandingEntry]:
        results: list[UnderstandingEntry] = []
        for entry in self._entries.values():
            if subject and entry.subject != subject:
                continue
            if object_ and entry.object_ != object_:
                continue
            if relation_type and entry.relation_type != relation_type:
                continue
            results.append(entry)
        return sorted(results, key=lambda e: e.last_confirmed_at, reverse=True)

    def decayed_confidence(
        self,
        entry_id: str,
        *,
        now: datetime,
        threshold_days: int = _DEFAULT_DECAY_DAYS,
        rate_per_day: float = _DEFAULT_DECAY_RATE_PER_DAY,
    ) -> float:
        entry = self._entries.get(entry_id)
        if entry is None:
            return 0.0
        age = now - entry.last_confirmed_at
        if age <= timedelta(days=threshold_days):
            return entry.confidence
        excess_days = (age - timedelta(days=threshold_days)).total_seconds() / 86400
        decayed = entry.confidence - rate_per_day * excess_days
        return max(0.0, decayed)

    def _flush(self) -> None:
        chunks: list[str] = []
        for entry in self._entries.values():
            chunks.append(_format_entry(entry))
        self._path.write_text("\n".join(chunks), encoding="utf-8")

    def _load(self) -> None:
        if not self._path.exists():
            return
        text = self._path.read_text(encoding="utf-8")
        for match in _ENTRY_RE.finditer(text):
            frontmatter = match.group("frontmatter")
            body = match.group("body").strip()
            fields = _parse_frontmatter(frontmatter)
            try:
                entry = UnderstandingEntry(
                    entry_id=fields["entry_id"],
                    subject=fields["subject"],
                    object_=fields["object"],
                    relation_type=fields["relation_type"],
                    current_understanding=body,
                    evidence_refs=_parse_list(fields.get("evidence_refs", "")),
                    confidence=float(fields.get("confidence", "0.5")),
                    last_confirmed_at=datetime.fromisoformat(fields["last_confirmed_at"]),
                    contradiction_history=_parse_list(fields.get("contradiction_history", "")),
                    open_questions=_parse_list(fields.get("open_questions", "")),
                    boundaries=_parse_list(fields.get("boundaries", "")),
                )
            except (KeyError, ValueError):
                continue
            self._entries[entry.entry_id] = entry


def _format_entry(entry: UnderstandingEntry) -> str:
    data = asdict(entry)
    body = data.pop("current_understanding")
    last = data.pop("last_confirmed_at")
    lines = [
        "<!-- understanding",
        f"entry_id: {data['entry_id']}",
        f"subject: {data['subject']}",
        f"object: {data['object_']}",
        f"relation_type: {data['relation_type']}",
        f"confidence: {data['confidence']}",
        f"last_confirmed_at: {_isoformat(last)}",
        f"evidence_refs: {_serialize_list(data['evidence_refs'])}",
        f"contradiction_history: {_serialize_list(data['contradiction_history'])}",
        f"open_questions: {_serialize_list(data['open_questions'])}",
        f"boundaries: {_serialize_list(data['boundaries'])}",
        "-->",
        body,
    ]
    return "\n".join(lines) + "\n"


def _isoformat(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_frontmatter(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _serialize_list(values: list[str]) -> str:
    return ",".join(value.replace(",", " ") for value in values)


def _parse_list(value: str) -> list[str]:
    if not value:
        return []
    return [chunk.strip() for chunk in value.split(",") if chunk.strip()]
