"""Tier 1-6: conditional soft routing reminder.

When an agent has `deep_research_pack` available, the user intent looks like deep
research / industry research / due diligence / source-backed report, and the agent
has already chained 3+ `web_search` calls in the session without invoking
`deep_research_run` or `deep_research_start`, this module appends a one-shot
advisory to the next `web_search` tool result. The agent sees the reminder in the
conversation history and can self-correct.

Tier 1 keeps this strictly soft — no hard reject. Tier 2 (T2-6) tightens this into
a governance-level block under the same conditions.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)

_DEEP_RESEARCH_TOOLS = frozenset({"deep_research_run", "deep_research_start"})
_TRIGGER_THRESHOLD = 3

_DEEP_RESEARCH_KEYWORDS_EN = (
    "deep research",
    "industry research",
    "due diligence",
    "source-backed report",
    "investor research",
    "market analysis",
    "competitive analysis",
    "market map",
)
_DEEP_RESEARCH_KEYWORDS_ZH = (
    "深度研究",
    "行业研究",
    "尽职调查",
    "竞品分析",
    "市场调研",
    "投资研究",
    "市场分析",
)

_REMINDER_TEXT = (
    "\n\n<routing-reminder>\n"
    "⚠️ Tier 1 advisory: this session has run web_search "
    "{web_search_count} times without invoking deep_research_run or "
    "deep_research_start. For industry research, due diligence, or "
    "source-backed reports, the deep_research_* tools produce a much "
    "stronger artifact (multi-lane evidence ledger, structured source "
    "notes, analyst-grade synthesis) than chaining web_search + write_file. "
    "Continuing to fan out manual web_search calls will likely fail the "
    "synthesis quality gate. Consider switching now.\n"
    "</routing-reminder>"
)


@dataclass
class _SessionTracker:
    web_search_count: int = 0
    deep_research_count: int = 0
    reminder_emitted: bool = False


_session_state: dict[str, _SessionTracker] = {}
_session_lock = threading.Lock()


def _tracker_for(session_id: str) -> _SessionTracker:
    with _session_lock:
        tracker = _session_state.get(session_id)
        if tracker is None:
            tracker = _SessionTracker()
            _session_state[session_id] = tracker
        return tracker


def _matches_intent(*hints: str) -> bool:
    text = " ".join((hint or "") for hint in hints).lower()
    if not text:
        return False
    for keyword in _DEEP_RESEARCH_KEYWORDS_EN:
        if keyword in text:
            return True
    for keyword in _DEEP_RESEARCH_KEYWORDS_ZH:
        if keyword in text:
            return True
    return False


def _pack_visible(available_tool_names: Iterable[str] | None) -> bool:
    if not available_tool_names:
        return False
    for name in available_tool_names:
        if str(name) in _DEEP_RESEARCH_TOOLS:
            return True
    return False


def maybe_inject_routing_reminder(
    tool_result: str,
    *,
    tool_name: str,
    session_id: str | None,
    available_tool_names: Iterable[str] | None = None,
    intent_hints: tuple[str, ...] = (),
) -> str:
    """Append the Tier 1 routing reminder to a web_search result when conditions hold.

    The reminder is one-shot per session_id and never fires when the agent has already
    used deep_research_run/start or when the deep_research_pack is not on the toolbelt.
    """
    if not session_id:
        return tool_result
    if tool_name in _DEEP_RESEARCH_TOOLS:
        tracker = _tracker_for(session_id)
        tracker.deep_research_count += 1
        return tool_result
    if tool_name != "web_search":
        return tool_result

    tracker = _tracker_for(session_id)
    tracker.web_search_count += 1

    if tracker.reminder_emitted:
        return tool_result
    if tracker.deep_research_count > 0:
        return tool_result
    if tracker.web_search_count < _TRIGGER_THRESHOLD:
        return tool_result
    if not _pack_visible(available_tool_names):
        return tool_result
    if not _matches_intent(*intent_hints):
        return tool_result

    tracker.reminder_emitted = True
    reminder = _REMINDER_TEXT.format(web_search_count=tracker.web_search_count)
    logger.info(
        "[DeepResearchRouting] Emitted Tier-1 reminder for session=%s after %d web_search calls",
        session_id,
        tracker.web_search_count,
    )
    return (tool_result or "") + reminder


def reset_session_state(session_id: str | None = None) -> None:
    """Test/lifecycle helper: clear counters for a session (or all)."""
    with _session_lock:
        if session_id is None:
            _session_state.clear()
        else:
            _session_state.pop(session_id, None)
