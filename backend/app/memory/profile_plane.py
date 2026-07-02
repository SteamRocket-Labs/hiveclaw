"""Profile-plane resident memory reader (Part A read side, spec §4.2/§3.2).

The profile plane (self + profiles + soul) is small and always relevant, so it
is loaded WHOLE into the prompt — no retrieval, no per-entry scoring, no LLM,
and NEVER trimmed here: an over-budget resident block is a write-side
convergence failure (工序 4 signal), so the reader raises a one-shot
``memory_resident_over_budget`` alert and keeps the text intact.

This module also owns the self.md structure contract shared with the write
side (Part D): ``## 失败模式`` entries carry a lifecycle state
(active / 规避中 / 已根除) either on the ``###`` heading or on a ``- 状态:``
line; active entries float to the very top of the resident block.

soul.md itself is injected by the identity/agent-context prompt section and is
intentionally NOT duplicated here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.memory.explicit_overlay import load_explicit_overlay_entries

logger = logging.getLogger(__name__)

RESIDENT_SECTION_FILES: tuple[tuple[str, str], ...] = (
    ("self", "self/self.md"),
    ("profiles/owner", "profiles/owner.md"),
    ("profiles/collaborators", "profiles/collaborators.md"),
    ("profiles/domain", "profiles/domain.md"),
)
DEFAULT_RESIDENT_BUDGET_CHARS = 12_000.0

_FAILURE_SECTION_RE = re.compile(r"^##\s+失败模式\s*$")
_SECTION_HEADER_RE = re.compile(r"^##\s+")
_ENTRY_HEADER_RE = re.compile(r"^###\s+")
_STATE_LINE_RE = re.compile(r"^-\s*状态\s*[:：]\s*(?P<state>\S+)")


@dataclass(frozen=True, slots=True)
class ResidentMemory:
    text: str
    chars: int
    budget_chars: float
    over_budget: bool
    sections: tuple[str, ...]
    active_failure_modes: str


def load_resident_memory(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    budget_chars: float = DEFAULT_RESIDENT_BUDGET_CHARS,
    include_overlay: bool = True,
) -> ResidentMemory:
    """Load the resident profile plane whole. Zero LLM, zero trimming."""

    mem_dir = Path(data_root) / str(agent_id) / "memory"
    sections: list[str] = []
    blocks: list[str] = []
    active_failures = ""

    for section_name, rel_path in RESIDENT_SECTION_FILES:
        path = mem_dir / rel_path
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("Resident memory skipped unreadable %s: %s", path, exc)
            continue
        if not content:
            continue
        sections.append(section_name)
        if section_name == "self":
            active_failures = extract_active_failure_modes(content)
        blocks.append(f"### {section_name} ({rel_path})\n{content}")

    if include_overlay:
        overlay_lines: list[str] = []
        for entry in load_explicit_overlay_entries(Path(data_root), agent_id):
            if entry.status != "active":
                continue
            # Sensitive entries never ride the always-on resident block; they
            # remain reachable through retrieval + activation gating (§4.5).
            if str(entry.sensitivity or "").startswith(("PL3", "PL4")):
                continue
            content = entry.content.strip()
            if content:
                overlay_lines.append(f"- [{entry.category}] {content} ({entry.entry_id})")
        if overlay_lines:
            sections.append("explicit_overlay")
            blocks.append("### Explicit Overlay (active)\n" + "\n".join(overlay_lines))

    if not blocks:
        return ResidentMemory(
            text="",
            chars=0,
            budget_chars=float(budget_chars),
            over_budget=False,
            sections=(),
            active_failure_modes="",
        )

    parts = ["[Profile Plane — Resident Memory]"]
    if active_failures:
        parts.append("### ⚠ Active Failure Modes (self)\n" + active_failures)
    parts.extend(blocks)
    text = "\n\n".join(parts)
    return ResidentMemory(
        text=text,
        chars=len(text),
        budget_chars=float(budget_chars),
        over_budget=len(text) > float(budget_chars),
        sections=tuple(sections),
        active_failure_modes=active_failures,
    )


def extract_active_failure_modes(self_md: str) -> str:
    """Pull ``###`` entries with lifecycle state ``active`` out of ``## 失败模式``."""

    lines = (self_md or "").splitlines()
    in_failures = False
    entries: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if _FAILURE_SECTION_RE.match(stripped):
            in_failures = True
            continue
        if in_failures and _SECTION_HEADER_RE.match(stripped):
            break
        if not in_failures:
            continue
        if _ENTRY_HEADER_RE.match(stripped):
            current = [line]
            entries.append(current)
        elif current is not None:
            current.append(line)

    active_blocks: list[str] = []
    for entry_lines in entries:
        heading = entry_lines[0].strip()
        state = ""
        for line in entry_lines[1:]:
            match = _STATE_LINE_RE.match(line.strip())
            if match:
                state = match.group("state").strip().lower()
                break
        heading_state = heading.rsplit("—", 1)[-1].strip().lower() if "—" in heading else ""
        # The `- 状态:` line is authoritative; the heading suffix is the fallback.
        is_active = state.startswith("active") if state else heading_state.startswith("active")
        if is_active:
            active_blocks.append("\n".join(entry_lines).strip())
    return "\n\n".join(active_blocks)


async def check_resident_budget(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    resident: ResidentMemory,
) -> bool:
    """One-shot over-budget alert; marker clears on recovery. Returns alerted."""

    marker_path = Path(data_root) / str(agent_id) / "memory" / "control" / "resident_budget.json"
    previous_alerted_at = ""
    if marker_path.exists():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
            previous_alerted_at = str(previous.get("over_budget_alerted_at") or "").strip()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Resident budget marker unreadable for %s, rebuilding: %s", agent_id, exc)

    now = datetime.now(UTC).isoformat()
    alerted = False
    alerted_at = ""
    if resident.over_budget:
        if previous_alerted_at:
            alerted_at = previous_alerted_at
        else:
            payload = {
                "chars": resident.chars,
                "budget_chars": resident.budget_chars,
                "sections": list(resident.sections),
            }
            try:
                from app.services.audit_logger import write_audit_log

                await write_audit_log("memory_resident_over_budget", payload, agent_id=_parse_uuid(agent_id))
            except Exception as exc:  # noqa: BLE001 - alerting must not break prompt assembly
                logger.warning("Resident over-budget audit alert failed for %s: %s", agent_id, exc)
            alerted_at = now
            alerted = True
    elif previous_alerted_at:
        logger.info("Resident memory back under budget for agent %s", agent_id)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "schema": "resident_budget.v1",
                "agent_id": str(agent_id),
                "generated_at": now,
                "chars": resident.chars,
                "budget_chars": resident.budget_chars,
                "over_budget": resident.over_budget,
                "over_budget_alerted_at": alerted_at,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return alerted


def _parse_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
