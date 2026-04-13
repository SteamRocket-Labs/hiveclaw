"""Heartbeat service — proactive agent awareness loop.

Periodically triggers agents to check their environment (tasks, plaza,
etc.) and take autonomous actions. Inspired by OpenClaw's heartbeat
mechanism.

Runs as a background task inside the FastAPI process.
"""

import asyncio
import fcntl
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.events import get_redis
from app.memory.t2_store import load_incremental_t2_entries, load_t2_entries, render_t2_snapshot
from app.kernel.contracts import ExecutionIdentityRef
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.agent_tools import execute_tool
from app.services.session_service import create_chat_session, session_conversation_id

# Single source of truth: app/templates/HEARTBEAT.md
# No hardcoded instruction here — read from template file at runtime.
_HEARTBEAT_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
_HEARTBEAT_LEASE_TTL_SECONDS = 600
_heartbeat_leases: dict[uuid.UUID, datetime] = {}

_HEARTBEAT_PRIVACY_SUFFIX = """
## Plaza Posting Scope
When posting to the plaza during heartbeat, be selective:

- Focus on: general work insights, patterns you've learned, opinions on others' posts
- Avoid: private user conversations, confidential task details, literal excerpts from memory or workspace
- Per heartbeat: 1 new post + up to 2 comments max. Skip trivial or repetitive posts.
"""

_HEARTBEAT_STRATEGY_SUFFIX = """

## Strategy Logging Scope
evolution/lineage.md stores policy-level learning and durable strategy changes.
Keep entries focused: strategy choice, action, outcome, learning, and next focus.
Do NOT turn lineage into a raw task transcript.
Avoid raw task transcripts or tool-by-tool logs — those belong in T0.
"""


# ── KAIROS persistent session state ──
# Instead of creating a fresh invocation each tick, maintain conversation
# history across ticks so the agent has continuity of thought.
_heartbeat_contexts: dict[uuid.UUID, list[dict]] = {}
_heartbeat_session_ids: dict[uuid.UUID, uuid.UUID] = {}
_heartbeat_tick_counts: dict[uuid.UUID, int] = {}
_t2_mtimes: dict[uuid.UUID, dict[str, float]] = {}


def _reset_heartbeat_session(agent_id: uuid.UUID) -> None:
    """Reset heartbeat persistent session (called after dream, day change, or process restart)."""
    _heartbeat_contexts.pop(agent_id, None)
    _heartbeat_session_ids.pop(agent_id, None)
    _heartbeat_tick_counts.pop(agent_id, None)
    _t2_mtimes.pop(agent_id, None)
    logger.info("[Heartbeat] Session reset for %s", agent_id)


def _read_t2_full(agent_id: uuid.UUID) -> str:
    """Read all T2 learnings files (full content) for first tick initialization."""
    from app.config import get_settings

    entries, current_mtimes = load_t2_entries(Path(get_settings().AGENT_DATA_DIR), agent_id)
    _t2_mtimes[agent_id] = current_mtimes
    snapshot = render_t2_snapshot(entries)
    return snapshot or "(no learnings yet)"


def _read_t3_summary(agent_id: uuid.UUID) -> str:
    """Read T3 memory files summary (reference for dedup during curation)."""
    from app.config import get_settings

    memory_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory"
    if not memory_dir.exists():
        return "(no memory files)"

    parts: list[str] = []
    for fname in ["feedback.md", "knowledge.md", "strategies.md", "blocked.md", "user.md"]:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    # Truncate to first 500 chars per file for reference
                    parts.append(f"### {fname}\n{content[:500]}")
            except Exception as exc:
                logger.debug("[Heartbeat] Failed to read T3 %s: %s", fpath, exc)
    return "\n\n".join(parts) if parts else "(no memory files)"


def _read_incremental_t2(agent_id: uuid.UUID) -> str:
    """Read only new T2 entries since last tick (via mtime comparison)."""
    from app.config import get_settings

    entries, current_mtimes = load_incremental_t2_entries(
        Path(get_settings().AGENT_DATA_DIR),
        agent_id,
        _t2_mtimes.get(agent_id, {}),
    )
    _t2_mtimes[agent_id] = current_mtimes
    return render_t2_snapshot(entries)


def _get_default_heartbeat_instruction() -> str:
    """Read default heartbeat instruction from templates/HEARTBEAT.md (single source of truth)."""
    try:
        return _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "[Heartbeat] Check focus.md, do one useful thing, reply HEARTBEAT_OK if nothing needed."


def _compose_heartbeat_instruction(base_instruction: str) -> str:
    return base_instruction + _HEARTBEAT_STRATEGY_SUFFIX + _HEARTBEAT_PRIVACY_SUFFIX


def _try_acquire_heartbeat_lease(
    agent_id: uuid.UUID,
    *,
    now: datetime | None = None,
    ttl_seconds: int = _HEARTBEAT_LEASE_TTL_SECONDS,
) -> bool:
    """Acquire a per-agent heartbeat lease, expiring stale entries automatically."""
    current = now or datetime.now(timezone.utc)
    lease_started_at = _heartbeat_leases.get(agent_id)
    if lease_started_at is not None and (current - lease_started_at).total_seconds() < ttl_seconds:
        return False
    _heartbeat_leases[agent_id] = current
    return True


def _release_heartbeat_lease(agent_id: uuid.UUID) -> None:
    _heartbeat_leases.pop(agent_id, None)


async def _try_acquire_heartbeat_lease_async(
    agent_id: uuid.UUID,
    *,
    now: datetime | None = None,
    ttl_seconds: int = _HEARTBEAT_LEASE_TTL_SECONDS,
) -> bool:
    lease_key = f"heartbeat_lease:{agent_id}"
    try:
        redis = await get_redis()
        acquired = await redis.set(lease_key, (now or datetime.now(timezone.utc)).isoformat(), ex=ttl_seconds, nx=True)
        if acquired:
            _heartbeat_leases[agent_id] = now or datetime.now(timezone.utc)
        return bool(acquired)
    except Exception as exc:
        logger.debug("[Heartbeat] Redis lease unavailable, falling back to local lease: %s", exc)
        return _try_acquire_heartbeat_lease(agent_id, now=now, ttl_seconds=ttl_seconds)


async def _release_heartbeat_lease_async(agent_id: uuid.UUID) -> None:
    lease_key = f"heartbeat_lease:{agent_id}"
    try:
        redis = await get_redis()
        await redis.delete(lease_key)
    except Exception as exc:
        logger.debug("[Heartbeat] Redis lease release skipped: %s", exc)
    finally:
        _release_heartbeat_lease(agent_id)


def _is_in_active_hours(active_hours: str, tz_name: str = "UTC") -> bool:
    """Check if current time is within the agent's active hours.

    Format: "HH:MM-HH:MM" (e.g., "09:00-18:00")
    Uses agent's configured timezone (defaults to UTC).
    """
    try:
        from zoneinfo import ZoneInfo

        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., "22:00-06:00")
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True  # Default to active if parsing fails


def _load_heartbeat_instruction(agent_id: uuid.UUID) -> str:
    """Read agent's HEARTBEAT.md, fallback to templates/HEARTBEAT.md (single source of truth)."""
    from app.config import get_settings

    settings = get_settings()

    for ws_root in [
        Path("/tmp/hive_workspaces") / str(agent_id),
        Path(settings.AGENT_DATA_DIR) / str(agent_id),
    ]:
        hb_file = ws_root / "HEARTBEAT.md"
        if not hb_file.exists():
            continue
        try:
            custom = hb_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            logger.debug(f"Failed to read HEARTBEAT.md from {hb_file}: {e}")
            custom = ""
        if not custom:
            break
        return _compose_heartbeat_instruction(custom)

    return _compose_heartbeat_instruction(_get_default_heartbeat_instruction())


def _parse_heartbeat_outcome(reply: str | None) -> tuple[str, int | None]:
    """Parse structured outcome from heartbeat reply.

    Expects LLM to output [OUTCOME:noop|action_taken|curated|failure] [SCORE:0-10].
    Falls back to heuristics if structured tags are missing.

    Returns (outcome_type, score).
    """
    if not reply:
        return "noop", None

    # Try structured tag first: [OUTCOME:action_taken]
    outcome_match = re.search(r"\[OUTCOME:\s*(noop|action_taken|curated|failure)\s*\]", reply, re.IGNORECASE)
    score_match = re.search(r"\[SCORE:\s*(\d+)\s*\]", reply)

    if outcome_match:
        outcome = outcome_match.group(1).lower()
        if outcome == "curated":
            outcome = "action_taken"
    else:
        # Fallback heuristics — only when structured tags are absent
        # Default to noop (not action_taken) to avoid inflating success rate
        is_action = any(kw in reply.upper() for kw in ("WROTE", "CREATED", "UPDATED", "POSTED", "SENT", "FIXED"))
        if is_action:
            outcome = "action_taken"
        else:
            outcome = "noop"

    if score_match:
        score = min(int(score_match.group(1)), 10)
    else:
        # Fallback score based on outcome type — prevents silent None that
        # breaks _write_evolution_to_memory and inflates scorecard counters.
        _OUTCOME_FALLBACK_SCORES = {"action_taken": 5, "failure": 2, "noop": 0}
        score = _OUTCOME_FALLBACK_SCORES.get(outcome, 0)

    return outcome, score


async def _build_evolution_context(agent_id: uuid.UUID, recent_activities: list) -> str:
    """Build structured evolution context from activity logs and workspace evolution files.

    This is the server-side pattern analysis that feeds into the heartbeat prompt,
    giving the agent pre-computed metrics instead of raw activity logs.
    """
    from collections import Counter

    parts: list[str] = []

    # 1. Read evolution files from canonical workspace (H7: single source of truth)
    ws_root = _get_canonical_workspace(agent_id)
    if ws_root:
        for filename in ["evolution/scorecard.md", "evolution/blocklist.md"]:
            fpath = ws_root / filename
            if fpath.exists():
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        parts.append(content)
                except Exception as e:
                    logger.debug(f"Failed to read evolution file {fpath}: {e}")

        # Read lineage tail — keep enough history for long-term pattern recognition
        lineage_path = ws_root / "evolution" / "lineage.md"
        if lineage_path.exists():
            try:
                full = lineage_path.read_text(encoding="utf-8", errors="replace").strip()
                lines = full.split("\n")
                if len(lines) > 80:
                    parts.append("\n".join(lines[:5] + ["...(earlier entries omitted)..."] + lines[-70:]))
                else:
                    parts.append(full)
            except Exception as e:
                logger.debug(f"Failed to read evolution lineage: {e}")

        # Read compaction summary — context the agent lost during mid-loop compression
        compaction_path = ws_root / "workspace" / "compaction_summary.md"
        if compaction_path.exists():
            try:
                compaction = compaction_path.read_text(encoding="utf-8", errors="replace").strip()
                if compaction:
                    parts.append(f"\n---\n## Last Session Compaction Summary\n{compaction[:2000]}")
            except Exception as e:
                logger.debug(f"Failed to read compaction summary: {e}")

        # No fallback needed — _get_canonical_workspace already resolved the right path

    # 2. Compute pattern summary from activity logs
    if recent_activities:
        error_count = sum(1 for a in recent_activities if a.action_type == "error")
        heartbeat_count = sum(1 for a in recent_activities if a.action_type == "heartbeat")
        tool_count = sum(1 for a in recent_activities if a.action_type == "tool_call")
        total = len(recent_activities)

        # Detect repeated failure patterns
        error_summaries = [a.summary[:80] for a in recent_activities if a.action_type == "error"]
        repeated_errors = [
            f"  - '{err}' (x{count})" for err, count in Counter(error_summaries).most_common(3) if count > 1
        ]

        # Tool usage frequency
        tool_names = []
        for a in recent_activities:
            if a.action_type == "tool_call" and a.detail_json:
                tool_name = a.detail_json.get("tool", "")
                if tool_name:
                    tool_names.append(tool_name)
        top_tools = [f"  - {name} (x{count})" for name, count in Counter(tool_names).most_common(5)]

        # Include error details (not just summaries) for learning
        error_details = []
        for a in recent_activities:
            if a.action_type == "error" and a.detail_json:
                detail = a.detail_json.get("error", "") or a.detail_json.get("message", "")
                if detail:
                    error_details.append(f"  - {str(detail)[:300]}")
        error_details = error_details[:5]  # Top 5 most recent errors

        pattern_section = (
            f"\n---\n## Activity Pattern Analysis (auto-computed, last {total} activities)\n"
            f"- Errors: {error_count} ({error_count * 100 // max(total, 1)}%)\n"
            f"- Heartbeats logged: {heartbeat_count}\n"
            f"- Tool calls: {tool_count}\n"
        )
        if repeated_errors:
            pattern_section += (
                "- **Repeated failures** (MUST NOT retry these approaches):\n" + "\n".join(repeated_errors) + "\n"
            )
        if error_details:
            pattern_section += "- **Recent error details** (learn from these):\n" + "\n".join(error_details) + "\n"
        if top_tools:
            pattern_section += "- Top tools used:\n" + "\n".join(top_tools) + "\n"

        parts.append(pattern_section)

        # 4. Skill creation hint — detect repeated tool-use patterns worth codifying
        _SKILL_THRESHOLD = 3  # same tool combo used 3+ times → suggest skill
        if top_tools and tool_count >= 6:
            # Check if any tool appears frequently enough to be worth a skill
            frequent_tools = [
                name
                for name, count in Counter(tool_names).most_common(3)
                if count >= _SKILL_THRESHOLD
                and name not in ("read_file", "write_file", "list_files", "edit_file", "save_memory", "search_memory")
            ]
            if frequent_tools:
                parts.append(
                    "\n---\n## Skill Creation Opportunity\n"
                    f"You have used these tools repeatedly: {', '.join(frequent_tools)}.\n"
                    "Consider whether the workflow around them is worth saving as a reusable skill:\n"
                    "1. Use `load_skill` or inspect your existing skills/ directory to check for duplicates\n"
                    "2. If no matching skill exists, call `save_skill` instead of writing skill files manually\n"
                    "3. Include: name, description, reusable step-by-step instructions, verification, and declared tools/packs only when stable\n"
                    "4. A good skill captures the *workflow* (multiple tools in sequence), not a single tool or one-off note\n"
                    "This counts as a high-value heartbeat action (score 7+)."
                )

    # 3. Cold start bootstrap — guide new agents through first heartbeats
    non_heartbeat_activities = [a for a in recent_activities if a.action_type != "heartbeat"]
    is_cold_start = len(non_heartbeat_activities) < 3

    if is_cold_start:
        # Detect repeated bootstrap failures — use sliding window (not consecutive-only)
        # to catch intermittent failure patterns like [ok, fail, ok, fail, fail]
        recent_heartbeats = [a for a in recent_activities if a.action_type == "heartbeat"]
        total_failures = sum(
            1 for hb in recent_heartbeats[:6] if (hb.detail_json or {}).get("outcome_type", "") in ("crash", "failure")
        )

        if total_failures >= 5:
            # M-19: Hard cap — stop retrying bootstrap (5 of 6 recent heartbeats failed)
            parts.append(
                "\n---\n## Bootstrap Exhausted (10 failures)\n"
                "Bootstrap has failed repeatedly. Stop attempting bootstrap actions.\n"
                "Proceed directly with normal heartbeat: read focus.md and do one small task.\n"
                "Output: [OUTCOME:noop] [SCORE:1]"
            )
        elif total_failures >= 3:
            # Auto-seed evolution files server-side to break the cycle
            _auto_seed_evolution(agent_id)
            parts.append(
                "\n---\n## Bootstrap Recovery (auto-seeded)\n"
                "Your previous bootstrap attempts failed. Evolution files have been\n"
                "auto-seeded with initial values. Skip bootstrapping and proceed with\n"
                "the normal 4-phase heartbeat protocol.\n"
                "Focus on ONE simple action: read focus.md and do something small.\n"
                "Output: [OUTCOME:action_taken] [SCORE:3]"
            )
        else:
            parts.append(
                "\n---\n## Bootstrap Mode (first heartbeats)\n"
                "You have very little activity history. This is normal for a new agent.\n"
                "Instead of the normal heartbeat protocol, do these bootstrapping steps:\n"
                "1. **Read soul.md** — understand your identity and role\n"
                "2. **Read focus.md** — check if initial tasks were set during creation\n"
                "3. **List and read your skills/** — understand your capabilities\n"
                "4. **If focus.md is empty**: write an initial focus based on your role from soul.md\n"
                "5. **Write to evolution/lineage.md** with your bootstrap observations\n"
                "6. Output: [OUTCOME:action_taken] [SCORE:3]\n\n"
                "After bootstrapping, future heartbeats will follow the normal 4-phase protocol."
            )

    return "\n\n".join(parts) if parts else ""


_LINEAGE_ARCHIVE_MAX = 500


def _archive_lineage_entries(evo_dir: Path, discarded_segments: list[str], agent_id: uuid.UUID) -> None:
    """Archive rotated lineage entries to lineage_archive.json before they are lost.

    Extracts date/strategy/outcome/score from each entry as compact summaries.
    Keeps last _LINEAGE_ARCHIVE_MAX entries in the archive file.
    """
    archive_path = evo_dir / "lineage_archive.json"
    existing: list[dict] = []
    if archive_path.exists():
        try:
            existing = json.loads(archive_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError) as load_err:
            logger.debug("[Heartbeat] Failed to load lineage archive: %s", load_err)

    for segment in discarded_segments:
        entry: dict[str, str | int | None] = {}
        date_match = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}:\d{2})", segment)
        if date_match:
            entry["date"] = date_match.group(1)
        for line in segment.splitlines():
            line = line.strip()
            if line.startswith("- Source:"):
                entry["source"] = line[9:].strip()[:50]
            if line.startswith("- Strategy:"):
                entry["strategy"] = line[11:].strip()[:150]
            elif line.startswith("- Outcome:"):
                entry["outcome"] = line[10:].strip()[:50]
            elif line.startswith("- Score:"):
                try:
                    entry["score"] = int(line[8:].strip().split()[0])
                except (ValueError, IndexError) as parse_err:
                    logger.debug("[Heartbeat] Failed to parse score: %s", parse_err)
        if entry.get("date") or entry.get("strategy"):
            existing.append(entry)

    # Cap archive size
    existing = existing[-_LINEAGE_ARCHIVE_MAX:]
    try:
        archive_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("[Heartbeat] Archived %d rotated lineage entries for %s", len(discarded_segments), agent_id)
    except Exception as write_err:
        logger.debug("[Heartbeat] Failed to write lineage archive: %s", write_err)


def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    fd_closed = False
    try:
        os.write(tmp_fd, content.encode("utf-8"))
        os.close(tmp_fd)
        fd_closed = True
        os.replace(tmp_path, str(path))
    except BaseException:
        if not fd_closed:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except OSError as unlink_exc:
            logger.debug("[Heartbeat] Failed to clean up temp file %s: %s", tmp_path, unlink_exc)
        raise


def _update_evolution_files(
    agent_id: uuid.UUID,
    outcome_type: str,
    score: int | None,
    summary: str,
    *,
    source: str = "heartbeat",
) -> None:
    """Server-side writeback: update scorecard counters and append lineage entry.

    This closes the evolution feedback loop — the agent can see its real
    performance history on subsequent heartbeats instead of frozen seed values.

    Uses flock() to protect the read-modify-write cycle against concurrent
    heartbeat processes writing the same files.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H:%M")

    # Use canonical workspace to avoid double-counting across paths
    ws_root = _get_canonical_workspace(agent_id)
    if not ws_root:
        logger.debug("[Heartbeat] No workspace found for evolution writeback: %s", agent_id)
        return

    evo_dir = ws_root / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive lock for the entire read-modify-write cycle
    lock_path = evo_dir / ".evolution.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # ── Update scorecard counters ──
        scorecard_path = evo_dir / "scorecard.md"
        try:
            sc_text = scorecard_path.read_text(encoding="utf-8", errors="replace") if scorecard_path.exists() else ""
            counters = {
                "total_heartbeats": 0,
                "useful_heartbeats": 0,
                "total_trigger_runs": 0,
                "useful_trigger_runs": 0,
                "failed_runs": 0,
                "blocked_approaches": 0,
                "skills_created": 0,
                "strategies_evolved": 0,
            }
            for key in counters:
                match = re.search(rf"- {key}:\s*(\d+)", sc_text)
                if match:
                    counters[key] = int(match.group(1))
            legacy_failed_attempts = re.search(r"- failed_attempts:\s*(\d+)", sc_text)
            if legacy_failed_attempts:
                counters["failed_runs"] = max(counters["failed_runs"], int(legacy_failed_attempts.group(1)))

            if source == "heartbeat":
                counters["total_heartbeats"] += 1
                if outcome_type == "action_taken" and (score is None or score >= 5):
                    counters["useful_heartbeats"] += 1
            elif source == "trigger":
                counters["total_trigger_runs"] += 1
                if outcome_type == "action_taken" and (score is None or score >= 5):
                    counters["useful_trigger_runs"] += 1

            if outcome_type in ("failure", "crash"):
                counters["failed_runs"] += 1

            heartbeat_useful_rate = (
                round(counters["useful_heartbeats"] / counters["total_heartbeats"] * 100)
                if counters["total_heartbeats"] > 0
                else 0
            )
            trigger_useful_rate = (
                round(counters["useful_trigger_runs"] / counters["total_trigger_runs"] * 100)
                if counters["total_trigger_runs"] > 0
                else 0
            )
            trend = (
                f"- Heartbeat useful rate: {heartbeat_useful_rate}% "
                f"({counters['useful_heartbeats']}/{counters['total_heartbeats']})\n"
                f"- Trigger useful rate: {trigger_useful_rate}% "
                f"({counters['useful_trigger_runs']}/{counters['total_trigger_runs']})"
            )

            _atomic_write(
                scorecard_path,
                "# Evolution Scorecard\n\n## Metrics\n"
                + "".join(f"- {k}: {v}\n" for k, v in counters.items())
                + f"\n## Recent Trend\n{trend}\n"
                + f"Last updated: {now}\n",
            )
        except Exception as exc:
            logger.debug(f"[Heartbeat] Failed to update scorecard for {agent_id}: {exc}")

        # ── Append lineage entry (skip if agent already wrote one for this timestamp) ──
        lineage_path = evo_dir / "lineage.md"
        try:
            existing = lineage_path.read_text(encoding="utf-8", errors="replace") if lineage_path.exists() else ""
            if "(no entries yet)" in existing:
                existing = "# Evolution Lineage\n\n"

            entry_marker = f"{source.upper()}-{now}"
            if f"### {entry_marker}" in existing:
                logger.debug(
                    "[Heartbeat] Lineage entry %s already exists (agent-written), skipping server append", entry_marker
                )
            else:
                score_str = f", score={score}" if score is not None else ""
                entry = (
                    f"### {entry_marker}\n"
                    f"- Source: {source}\n"
                    f"- Outcome: {outcome_type}{score_str}\n"
                    f"- Summary: {summary}\n\n"
                )
                existing = existing.rstrip() + "\n\n" + entry

            new_content = existing

            # Rotate lineage: keep header + last 200 entries to prevent unbounded growth
            _LINEAGE_MAX_ENTRIES = 200
            segments = re.split(r"(?m)^### ", new_content)
            if len(segments) > _LINEAGE_MAX_ENTRIES + 1:  # +1 for header segment
                # B7 fix: archive rotated entries before discarding
                discarded = segments[1:-_LINEAGE_MAX_ENTRIES]  # Skip header segment
                if discarded:
                    _archive_lineage_entries(evo_dir, discarded, agent_id)

                header = "# Evolution Lineage\n\n"
                trimmed = header + "### ".join(segments[-_LINEAGE_MAX_ENTRIES:])
                _atomic_write(lineage_path, trimmed)
            else:
                _atomic_write(lineage_path, new_content)
        except Exception as exc:
            logger.debug(f"[Heartbeat] Failed to update lineage for {agent_id}: {exc}")

        # ── Auto-append blocklist on consecutive failures (F2 fix) ──
        # If last 3 lineage entries are all failures, add summary to blocklist.
        if outcome_type in ("failure", "crash") and (score is not None and score <= 2):
            try:
                lineage_text = (
                    lineage_path.read_text(encoding="utf-8", errors="replace") if lineage_path.exists() else ""
                )
                outcome_matches = re.findall(r"- Outcome:\s*(\w+)", lineage_text)
                last_3 = outcome_matches[-3:] if len(outcome_matches) >= 3 else []
                if len(last_3) == 3 and all(o in ("failure", "crash") for o in last_3):
                    blocklist_path = evo_dir / "blocklist.md"
                    bl_text = (
                        blocklist_path.read_text(encoding="utf-8", errors="replace")
                        if blocklist_path.exists()
                        else "# Blocklist\n"
                    )
                    date_str = now[:10]
                    entry = f"- [{date_str}] {summary[:150]} (3 consecutive failures)"
                    if summary[:60].lower() not in bl_text.lower():
                        _atomic_write(blocklist_path, bl_text.rstrip() + "\n" + entry + "\n")
                        logger.info("[Heartbeat] Auto-blocked approach for agent %s: %s", agent_id, summary[:80])
            except Exception as bl_err:
                logger.debug("[Heartbeat] Blocklist auto-append failed: %s", bl_err)

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    logger.info(f"[Heartbeat] Evolution files updated for agent {agent_id}: {outcome_type}")


def _auto_seed_evolution(agent_id: uuid.UUID) -> None:
    """Server-side emergency seed: write minimal evolution files to break bootstrap loop."""
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    for ws_root in [
        Path("/tmp/hive_workspaces") / str(agent_id),
        Path(settings.AGENT_DATA_DIR) / str(agent_id),
    ]:
        evo_dir = ws_root / "evolution"
        if ws_root.exists():
            evo_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H:%M")
            # Seed scorecard with initial counters
            scorecard = evo_dir / "scorecard.md"
            if not scorecard.exists() or "(updated each heartbeat)" in scorecard.read_text(
                encoding="utf-8", errors="replace"
            ):
                scorecard.write_text(
                    "# Evolution Scorecard\n\n## Metrics\n"
                    "- total_heartbeats: 3\n- useful_heartbeats: 0\n"
                    "- total_trigger_runs: 0\n- useful_trigger_runs: 0\n"
                    "- failed_runs: 3\n- blocked_approaches: 0\n"
                    "- skills_created: 0\n- strategies_evolved: 0\n\n"
                    "## Recent Trend\nBootstrap failures detected — auto-seeded.\n",
                    encoding="utf-8",
                )
            # Seed lineage with recovery record
            lineage = evo_dir / "lineage.md"
            lineage_content = lineage.read_text(encoding="utf-8", errors="replace") if lineage.exists() else ""
            if "(no entries yet)" in lineage_content or not lineage_content.strip():
                lineage.write_text(
                    "# Evolution Lineage\n\n"
                    f"### HEARTBEAT-{now} [auto-seed]\n"
                    "- Source: heartbeat\n"
                    "- Outcome: recovery\n"
                    "- Summary: 3 bootstrap failures detected, evolution files auto-seeded by server\n",
                    encoding="utf-8",
                )
            logger.info(f"[Heartbeat] Auto-seeded evolution files for agent {agent_id} after 3 bootstrap failures")
            return
    logger.warning(f"[Heartbeat] Cannot auto-seed evolution: no workspace found for agent {agent_id}")


def _validate_bootstrap_completion(agent_id: uuid.UUID) -> None:
    """Server-side validation that bootstrap produced expected files."""
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    for ws_root in [
        Path("/tmp/hive_workspaces") / str(agent_id),
        Path(settings.AGENT_DATA_DIR) / str(agent_id),
    ]:
        if not ws_root.exists():
            continue
        missing = []
        for required in ["focus.md", "evolution/lineage.md", "evolution/scorecard.md"]:
            fpath = ws_root / required
            if not fpath.exists() or fpath.stat().st_size < 10:
                missing.append(required)
        if missing:
            logger.info(f"[Heartbeat] Bootstrap incomplete for {agent_id}: missing {', '.join(missing)} — auto-seeding")
            _auto_seed_evolution(agent_id)
            # Seed focus.md if missing
            focus = ws_root / "focus.md"
            if not focus.exists() or focus.stat().st_size < 10:
                focus.write_text(
                    "# Focus\n\nBootstrap in progress — awaiting first heartbeat action.\n", encoding="utf-8"
                )
        return


def _get_canonical_workspace(agent_id: uuid.UUID) -> "Path | None":
    """Return the single canonical workspace path for an agent.

    Priority: AGENT_DATA_DIR (persistent) > /tmp (ephemeral).
    Syncs from /tmp → AGENT_DATA_DIR if /tmp has newer files.
    """
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    persistent = Path(settings.AGENT_DATA_DIR) / str(agent_id)
    ephemeral = Path("/tmp/hive_workspaces") / str(agent_id)

    # If persistent exists, it's canonical
    if persistent.exists():
        # Sync evolution files from ephemeral if they're newer
        if ephemeral.exists():
            for rel in ["evolution/scorecard.md", "evolution/lineage.md", "evolution/blocklist.md"]:
                eph_file = ephemeral / rel
                per_file = persistent / rel
                if eph_file.exists():
                    if not per_file.exists() or eph_file.stat().st_mtime > per_file.stat().st_mtime:
                        per_file.parent.mkdir(parents=True, exist_ok=True)
                        per_file.write_text(eph_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return persistent

    if ephemeral.exists():
        return ephemeral

    return None


def _build_heartbeat_tool_executor(agent_id: uuid.UUID, creator_id: uuid.UUID):
    """Build a tool executor with per-heartbeat plaza posting limits."""
    plaza_posts_made = 0
    plaza_comments_made = 0

    async def _executor(tool_name: str, args: dict) -> str:
        nonlocal plaza_posts_made, plaza_comments_made

        if tool_name == "plaza_create_post":
            if plaza_posts_made >= 1:
                return "[BLOCKED] You have already made 1 plaza post this heartbeat. Do not post again."
            plaza_posts_made += 1
        elif tool_name == "plaza_add_comment":
            if plaza_comments_made >= 2:
                return "[BLOCKED] You have already made 2 comments this heartbeat. Do not comment again."
            plaza_comments_made += 1

        return await execute_tool(tool_name, args, agent_id, creator_id)

    return _executor


async def _touch_last_heartbeat(agent_id: uuid.UUID) -> None:
    """Update last_heartbeat_at even on early return to prevent infinite re-triggering."""
    try:
        from app.database import async_session as _async_session
        from app.models.agent import Agent as _Agent

        async with _async_session() as _db:
            _result = await _db.execute(select(_Agent).where(_Agent.id == agent_id))
            _agent = _result.scalar_one_or_none()
            if _agent:
                _agent.last_heartbeat_at = datetime.now(timezone.utc)
                await _db.commit()
    except Exception as _exc:
        logger.debug(f"[Heartbeat] Failed to touch last_heartbeat_at for {agent_id}: {_exc}")


async def _maybe_run_skill_distillation(
    *,
    agent_id: uuid.UUID,
    workspace: Path,
    tenant_id: uuid.UUID | None,
    runtime_config,
    model,
    current_session_id: str | None,
) -> dict | None:
    if not getattr(runtime_config, "skill_candidate_loop_enabled", False):
        return None

    from app.services.skill_distiller import run_skill_distillation_cycle

    try:
        return await run_skill_distillation_cycle(
            agent_id=agent_id,
            workspace=workspace,
            tenant_id=tenant_id,
            runtime_config=runtime_config,
            model=model,
            current_session_id=current_session_id,
        )
    except Exception as exc:
        logger.warning("[Heartbeat] Skill distillation failed for %s: %s", agent_id, exc)
        return None


async def _execute_heartbeat(agent_id: uuid.UUID, *, lease_acquired: bool = False):
    """Execute a single heartbeat for an agent.

    Creates a Reflection Session (like trigger_daemon) so tool calls and
    the final reply are persisted and visible in the UI.
    """
    lease_held = lease_acquired
    if not lease_held:
        lease_held = await _try_acquire_heartbeat_lease_async(agent_id)
        if not lease_held:
            logger.info("[Heartbeat] Skip duplicate in-flight heartbeat for %s", agent_id)
            return

    import json as _json

    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.audit import ChatMessage
        from app.models.llm import LLMModel
        from app.models.participant import Participant

        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"[Heartbeat] Agent {agent_id} not found in DB — skipping")
                await _touch_last_heartbeat(agent_id)
                return

            # Set execution identity — autonomous heartbeat action
            from app.core.execution_context import set_agent_bot_identity

            set_agent_bot_identity(agent_id, agent.name, source="heartbeat")

            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                logger.warning(f"[Heartbeat] Agent {agent.name} ({agent_id}) has no model configured — skipping")
                await _touch_last_heartbeat(agent_id)
                return

            model_result = await db.execute(
                select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = model_result.scalar_one_or_none()
            if not model:
                logger.warning(f"[Heartbeat] Model {model_id} for agent {agent.name} ({agent_id}) not found — skipping")
                await _touch_last_heartbeat(agent_id)
                return

            # Fetch recent activity for evolution context
            from app.models.activity_log import AgentActivityLog

            try:
                recent_result = await db.execute(
                    select(AgentActivityLog)
                    .where(AgentActivityLog.agent_id == agent_id)
                    .where(
                        AgentActivityLog.action_type.in_(
                            [
                                "chat_reply",
                                "tool_call",
                                "task_created",
                                "task_updated",
                                "error",
                                "heartbeat",
                                "web_msg_sent",
                                "feishu_msg_sent",
                                "agent_msg_sent",
                                "file_written",
                                "schedule_run",
                                "plaza_post",
                            ]
                        )
                    )
                    .order_by(AgentActivityLog.created_at.desc())
                    .limit(50)
                )
                recent_activities = list(recent_result.scalars().all())
                evolution_context = await _build_evolution_context(agent_id, recent_activities)
            except Exception as e:
                logger.warning(f"Failed to build evolution context for heartbeat: {e}")
                evolution_context = ""

            # ── KAIROS persistent session: first tick vs subsequent tick ──
            tick_count = _heartbeat_tick_counts.get(agent_id, 0) + 1
            _heartbeat_tick_counts[agent_id] = tick_count

            # Resolve participant for DB session
            p_result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = p_result.scalar_one_or_none()
            agent_participant_id = agent_participant.id if agent_participant else None

            if agent_id not in _heartbeat_contexts:
                # ═══ First tick: full initialization ═══
                heartbeat_instruction = _load_heartbeat_instruction(agent_id)
                if evolution_context:
                    heartbeat_instruction += "\n\n" + evolution_context

                # Inject T2 learnings (full) + T3 memory (reference for dedup)
                t2_content = _read_t2_full(agent_id)
                t3_summary = _read_t3_summary(agent_id)
                heartbeat_instruction += f"\n\n## Current T2 Learnings\n{t2_content}"
                heartbeat_instruction += f"\n\n## Current T3 Memory (reference — don't duplicate these)\n{t3_summary}"

                runtime_messages = [{"role": "user", "content": heartbeat_instruction}]

                # Create new DB session (only on first tick)
                session = await create_chat_session(
                    db,
                    agent_id=agent_id,
                    user_id=agent.creator_id,
                    participant_id=agent_participant_id,
                    source_channel="heartbeat",
                    title=f"💓 Heartbeat: {agent.name}"[:200],
                )
                session_id = session.id
                _heartbeat_session_ids[agent_id] = session_id

                # Save heartbeat instruction as first message
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=session_conversation_id(session),
                        role="user",
                        content=heartbeat_instruction[:4000],
                        user_id=agent.creator_id,
                        participant_id=agent_participant_id,
                    )
                )
                await db.commit()
                logger.info("[Heartbeat] Tick #%d (full init) for %s", tick_count, agent.name)
            else:
                # ═══ Subsequent tick: <tick> + incremental T2 ═══
                new_t2 = _read_incremental_t2(agent_id)
                if not new_t2:
                    # Idle protection: no new T2 entries → skip this tick
                    logger.info("[Heartbeat] Skip tick #%d for %s: no new T2 entries", tick_count, agent.name)
                    await _release_heartbeat_lease_async(agent_id)
                    await _touch_last_heartbeat(agent_id)
                    return

                session_id = _heartbeat_session_ids[agent_id]
                runtime_messages = _heartbeat_contexts[agent_id]

                tick_msg = (
                    f"<tick>{datetime.now(timezone.utc).isoformat()} tick #{tick_count}</tick>\n\n"
                    f"## New T2 Entries\n{new_t2}"
                )
                runtime_messages.append({"role": "user", "content": tick_msg})

                # Save tick message to DB session
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=str(session_id),
                        role="user",
                        content=tick_msg[:4000],
                        user_id=agent.creator_id,
                        participant_id=agent_participant_id,
                    )
                )
                await db.commit()
                logger.info("[Heartbeat] Tick #%d (incremental, %d new entries) for %s", tick_count, new_t2.count("\n") + 1, agent.name)

            # Tool call persistence callback
            async def _on_tool_call(data: dict) -> None:
                if data.get("status") != "done":
                    return
                try:
                    async with async_session() as _tc_db:
                        _tc_db.add(
                            ChatMessage(
                                agent_id=agent_id,
                                conversation_id=str(session_id),
                                role="tool_call",
                                content=_json.dumps(
                                    {
                                        "name": data["name"],
                                        "args": data.get("args"),
                                        "status": "done",
                                        "result": str(data.get("result", ""))[:2000],
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                user_id=agent.creator_id,
                                participant_id=agent_participant_id,
                            )
                        )
                        await _tc_db.commit()
                except Exception as tc_err:
                    logger.debug(f"Failed to persist heartbeat tool call: {tc_err}")

            _HEARTBEAT_TIMEOUT_SECONDS = 300  # 5 min hard limit to prevent event loop deadlock
            result = await asyncio.wait_for(
                invoke_agent(
                    AgentInvocationRequest(
                        model=model,
                        messages=runtime_messages,
                        memory_messages=runtime_messages,
                        agent_name=agent.name,
                        role_description=agent.role_description or "",
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        execution_identity=ExecutionIdentityRef(
                            identity_type="agent_bot",
                            identity_id=agent_id,
                            label=f"Agent: {agent.name} (heartbeat)",
                        ),
                        session_context=SessionContext(
                            source="heartbeat",
                            channel="heartbeat",
                            session_id=str(session_id),
                            metadata={"agent_id": str(agent_id)},
                        ),
                        on_tool_call=_on_tool_call,
                        tool_executor=_build_heartbeat_tool_executor(agent_id, agent.creator_id),
                        core_tools_only=False,
                        max_tool_rounds=15,  # Pure curation: OBSERVE ~3 + CURATE ~8 + LOG ~4
                    )
                ),
                timeout=_HEARTBEAT_TIMEOUT_SECONDS,
            )
            reply = result.content

            # KAIROS: append assistant response to persistent context
            runtime_messages.append({"role": "assistant", "content": reply or ""})
            _heartbeat_contexts[agent_id] = runtime_messages

            # Save assistant reply to Reflection Session
            async with async_session() as db2:
                db2.add(
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=str(session_id),
                        role="assistant",
                        content=reply or "",
                        user_id=agent.creator_id,
                        participant_id=agent_participant_id,
                    )
                )
                await db2.commit()

            # Parse structured outcome from LLM reply
            outcome_type, heartbeat_score = _parse_heartbeat_outcome(reply)

            # Update last_heartbeat_at BEFORE activity logging (optimistic lock)
            # to prevent timestamp storm: if execution/logging takes long, the agent
            # won't be re-triggered because the timestamp is already advanced.
            async with async_session() as db3:
                a_result = await db3.execute(select(Agent).where(Agent.id == agent_id))
                a = a_result.scalar_one_or_none()
                if a:
                    a.last_heartbeat_at = datetime.now(timezone.utc)
                    await db3.commit()

            # M-20: Activity log MUST be written before evolution files
            # so evolution context sees the latest activity on next heartbeat
            from app.services.activity_logger import log_activity

            summary = reply[:80] if reply else "empty"
            await log_activity(
                agent_id,
                "heartbeat",
                f"Heartbeat [{outcome_type}]: {summary}",
                detail={
                    "reply": reply[:500] if reply else "",
                    "outcome_type": outcome_type,
                    "score": heartbeat_score,
                    "session_id": str(session_id),
                },
            )

            # Server-side evolution file writeback — closes the feedback loop
            # Runs in thread pool to avoid blocking the event loop (flock is blocking I/O)
            try:
                await asyncio.to_thread(
                    _update_evolution_files,
                    agent_id,
                    outcome_type,
                    heartbeat_score,
                    summary,
                    source="heartbeat",
                )
            except Exception as _evo_err:
                logger.warning(f"[Heartbeat] Evolution writeback failed for {agent_id}: {_evo_err}")

            try:
                from app.runtime.invoker import _resolve_runtime_config
                from app.tools.workspace import ensure_workspace

                runtime_config = await _resolve_runtime_config(agent_id)
                workspace = await ensure_workspace(agent_id, tenant_id=str(agent.tenant_id) if agent.tenant_id else None)
                await _maybe_run_skill_distillation(
                    agent_id=agent_id,
                    workspace=workspace,
                    tenant_id=agent.tenant_id,
                    runtime_config=runtime_config,
                    model=model,
                    current_session_id=str(session_id),
                )
            except Exception as _distill_err:
                logger.warning("[Heartbeat] Skill distillation setup failed for %s: %s", agent_id, _distill_err)

            # Count heartbeat as a session for auto-dream gate so agents with
            # low user-chat but high heartbeat activity still trigger distillation.
            try:
                from app.services.auto_dream import record_heartbeat_tick, record_session_end, should_dream, run_dream

                record_heartbeat_tick(agent_id)
                record_session_end(agent_id)
                if should_dream(agent_id) and agent.tenant_id:
                    asyncio.create_task(run_dream(agent_id, agent.tenant_id))
                    logger.info("[Heartbeat] Auto-dream triggered for agent %s", agent_id)
            except Exception as _dream_err:
                logger.debug("[Heartbeat] Auto-dream check failed: %s", _dream_err)

            # NOTE: Heartbeat outcomes are no longer written directly into long-term
            # memory here. Evolution files are the intermediate source; dream curates
            # durable entries into canonical markdown memory on the next cycle.

            # Emit HEARTBEAT_TICK_END hook → T0 log
            try:
                from app.runtime.hooks import HookEvent, emit_hook

                await emit_hook(
                    HookEvent.HEARTBEAT_TICK_END,
                    agent_id=agent_id,
                    session_id=str(session_id),
                    messages=runtime_messages,
                    source="heartbeat",
                    metadata={
                        "tick": tick_count,
                        "outcome": outcome_type,
                        "score": heartbeat_score,
                        "summary": summary[:200] if summary else "",
                        "action": summary[:100] if outcome_type == "action_taken" else "none",
                    },
                )
            except Exception as _hook_err:
                logger.debug("[Heartbeat] HEARTBEAT_TICK_END hook failed (non-fatal): %s", _hook_err)

            # Bootstrap validation: verify key files exist regardless of outcome
            # (cold_start agents need validation even on failure/noop)
            _validate_bootstrap_completion(agent_id)

            # G2: Auto-cancel triggers whose focus_ref items are completed in focus.md
            try:
                await _auto_cancel_completed_triggers(agent_id)
            except Exception as _ac_err:
                logger.debug("[Heartbeat] Auto-cancel triggers failed (non-fatal): %s", _ac_err)

            score_str = f" score={heartbeat_score}" if heartbeat_score is not None else ""
            logger.info(f"💓 Heartbeat for {agent.name}: {outcome_type}{score_str} — {summary}")

    except Exception as e:
        logger.error(f"Heartbeat error for agent {agent_id}: {e}", exc_info=True)
        # CRITICAL: Update last_heartbeat_at even on failure to prevent
        # every-minute storm (if timestamp stays None, agent is always eligible)
        try:
            from app.database import async_session as _async_session

            async with _async_session() as _db:
                from app.models.agent import Agent as _Agent

                _result = await _db.execute(select(_Agent).where(_Agent.id == agent_id))
                _agent = _result.scalar_one_or_none()
                if _agent:
                    _agent.last_heartbeat_at = datetime.now(timezone.utc)
                    await _db.commit()
        except Exception as db_err:
            logger.warning(f"Failed to update last_heartbeat_at after error: {db_err}")
        # Log crash to activity so evolution system can see it
        try:
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "heartbeat",
                f"Heartbeat crash: {str(e)[:80]}",
                detail={"outcome_type": "crash", "error": str(e)[:300]},
            )
        except Exception as log_err:
            logger.debug(f"Failed to log heartbeat crash to activity: {log_err}")
        # Update evolution files on crash too — closes the feedback loop
        try:
            await asyncio.to_thread(
                _update_evolution_files,
                agent_id,
                "crash",
                None,
                f"crash: {str(e)[:60]}",
                source="heartbeat",
            )
        except Exception as _evo_crash_err:
            logger.debug(f"[Heartbeat] Evolution writeback on crash failed: {_evo_crash_err}")
    finally:
        if lease_held:
            await _release_heartbeat_lease_async(agent_id)


async def _auto_cancel_completed_triggers(agent_id: uuid.UUID) -> None:
    """Disable triggers whose focus_ref items are marked [x] in focus.md.

    Runs at the end of each heartbeat to prevent stale triggers from
    wasting tokens on completed work.
    """
    ws_root = _get_canonical_workspace(agent_id)
    if not ws_root:
        return

    # Read focus.md and extract completed items
    focus_path = ws_root / "focus.md"
    if not focus_path.exists():
        return
    try:
        focus_text = focus_path.read_text(encoding="utf-8")
    except Exception as read_err:
        logger.debug("[Heartbeat] Failed to read focus.md for trigger auto-cancel: %s", read_err)
        return

    from app.services.focus_state import extract_completed_focus_refs

    completed_refs = extract_completed_focus_refs(focus_text)

    if not completed_refs:
        return

    # Find and disable matching triggers
    from app.database import async_session
    from app.models.trigger import AgentTrigger

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.is_enabled.is_(True),
                AgentTrigger.focus_ref.isnot(None),
            )
        )
        triggers = result.scalars().all()

        cancelled = 0
        for trigger in triggers:
            if trigger.focus_ref and trigger.focus_ref.strip().lower() in completed_refs:
                trigger.is_enabled = False
                cancelled += 1
                logger.info(
                    "[Heartbeat] Auto-cancelled trigger '%s' (focus_ref '%s' completed) for agent %s",
                    trigger.name,
                    trigger.focus_ref,
                    agent_id,
                )

        if cancelled > 0:
            await db.commit()


async def _heartbeat_tick():
    """One heartbeat tick: find agents due for heartbeat."""
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.audit_logger import write_audit_log
    from app.services.timezone_utils import get_agent_timezone_sync
    from app.models.tenant import Tenant

    now = datetime.now(timezone.utc)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.heartbeat_enabled.is_(True),
                    Agent.status.in_(["running", "idle"]),
                )
            )
            agents = result.scalars().all()

            # Periodic workspace sync — write DB data to files agents can read
            # Uses isolated sessions per tenant to prevent one failure from
            # poisoning the main session (InFailedSQLTransactionError cascade).
            synced_tenants: set[uuid.UUID] = set()
            from app.services.workspace_sync import sync_all_for_tenant

            for a in agents:
                if a.tenant_id and a.tenant_id not in synced_tenants:
                    for attempt in range(2):
                        try:
                            async with async_session() as sync_db:
                                await sync_all_for_tenant(sync_db, a.tenant_id)
                            synced_tenants.add(a.tenant_id)
                            break
                        except Exception as sync_err:
                            if attempt == 0:
                                logger.warning(f"Workspace sync failed for tenant {a.tenant_id}, retrying: {sync_err}")
                                await asyncio.sleep(1)
                            else:
                                logger.warning(
                                    f"Workspace sync failed for tenant {a.tenant_id} after retry: {sync_err}"
                                )

            # Pre-load tenants for timezone resolution
            tenant_ids = {a.tenant_id for a in agents if a.tenant_id}
            tenants_by_id = {}
            if tenant_ids:
                t_result = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
                tenants_by_id = {t.id: t for t in t_result.scalars().all()}

            triggered = 0
            skipped_hours = 0
            skipped_interval = 0
            for agent in agents:
                # Resolve timezone
                if agent.tenant_id is None:
                    skipped_interval += 1
                    continue
                tenant = tenants_by_id.get(agent.tenant_id)
                tz_name = get_agent_timezone_sync(agent, tenant)

                # Check active hours (in agent's timezone)
                if not _is_in_active_hours(agent.heartbeat_active_hours or "09:00-18:00", tz_name):
                    skipped_hours += 1
                    continue

                # Check interval
                interval = timedelta(minutes=agent.heartbeat_interval_minutes or 45)
                if agent.last_heartbeat_at and (now - agent.last_heartbeat_at) < interval:
                    skipped_interval += 1
                    continue

                # Fire heartbeat
                if not await _try_acquire_heartbeat_lease_async(agent.id, now=now):
                    logger.info(f"[Heartbeat] Agent {agent.name} already has an in-flight heartbeat")
                    continue
                logger.info(f"💓 Triggering heartbeat for {agent.name}")
                await write_audit_log("heartbeat_fire", {"agent_name": agent.name}, agent_id=agent.id)
                asyncio.create_task(_execute_heartbeat(agent.id, lease_acquired=True))
                triggered += 1

            logger.info(
                f"[Heartbeat] tick: eligible={len(agents)}, triggered={triggered},"
                f" skipped_hours={skipped_hours}, skipped_interval={skipped_interval}"
            )

    except Exception as e:
        logger.error(f"Heartbeat tick error: {e}", exc_info=True)
        await write_audit_log("heartbeat_error", {"error": str(e)[:300]})


async def start_heartbeat():
    """Start the background heartbeat loop. Call from FastAPI startup."""
    logger.info("💓 Agent heartbeat service started (60s tick)")
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)
