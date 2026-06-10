"""Heartbeat service — platform-managed memory/evolution maintenance loop.

Periodically curates recent learning into long-term memory and runs internal
self-evolution maintenance. User-facing autonomous patrols belong to triggers
and wake policies; heartbeat itself is always-on platform infrastructure.

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
from app.database import enter_rls_bypass, tenant_scoped_session
from app.memory.t2_store import (
    load_incremental_t2_entries,
    load_t2_entries,
    mark_t2_entries_absorbed,
    render_t2_snapshot,
)
from app.kernel.contracts import ExecutionIdentityRef
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.agent_tools import execute_tool
from app.services.heartbeat_policy import managed_heartbeat_interval_minutes
from app.services.runtime_task_service import create_runtime_task_record, update_runtime_task_record
from app.services.tenant_resolver import resolve_tenant_for_agent

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
# Persistent SessionContext per agent — allows the kernel to reuse the
# frozen prompt prefix across heartbeat ticks instead of rebuilding it
# every 45 minutes (saves DB queries + string rendering + enables
# Anthropic prompt cache hits within multi-round ticks).
_heartbeat_session_ctxs: dict[uuid.UUID, "SessionContext"] = {}

_HEARTBEAT_MESSAGE_MAX_CHARS = 24_000
_HEARTBEAT_CONTEXT_MAX_CHARS = 80_000
_HEARTBEAT_RECENT_MESSAGE_COUNT = 8
_HEARTBEAT_T2_FULL_MAX_CHARS = 24_000
_HEARTBEAT_T2_INCREMENTAL_MAX_CHARS = 16_000
_HEARTBEAT_T3_MAX_CHARS = 8_000
_HEARTBEAT_EVOLUTION_CONTEXT_MAX_CHARS = 16_000
_HEARTBEAT_COMPACT_SUMMARY_MAX_CHARS = 6_000
_HEARTBEAT_MAX_TOOL_ROUNDS = 40
_HEARTBEAT_CHECKPOINT_FILENAME = "heartbeat_checkpoint.json"


def _format_heartbeat_exception(exc: BaseException) -> str:
    message = str(exc).strip()
    exc_type = type(exc).__name__
    return f"{exc_type}: {message}" if message else exc_type


def _heartbeat_content_chars(message: dict) -> int:
    content = message.get("content")
    return len(content) if isinstance(content, str) else 0


def _heartbeat_context_chars(messages: list[dict]) -> int:
    return sum(_heartbeat_content_chars(message) for message in messages)


def _truncate_heartbeat_text(text: str, max_chars: int, label: str) -> str:
    """Trim a single heartbeat section while preserving its opening and latest tail."""
    if len(text) <= max_chars:
        return text

    marker = (
        f"\n\n[... {label} truncated to fit heartbeat context budget; omitted {len(text) - max_chars:,} chars ...]\n\n"
    )
    if max_chars <= len(marker) + 200:
        return text[: max(0, max_chars - len(marker))] + marker[:max_chars]

    head_chars = max(100, int((max_chars - len(marker)) * 0.6))
    tail_chars = max_chars - len(marker) - head_chars
    return text[:head_chars] + marker + text[-tail_chars:]


def _cap_heartbeat_message(message: dict, max_chars: int = _HEARTBEAT_MESSAGE_MAX_CHARS) -> dict:
    content = message.get("content")
    if not isinstance(content, str):
        return dict(message)
    capped = dict(message)
    capped["content"] = _truncate_heartbeat_text(content, max_chars, f"{message.get('role', 'message')} message")
    return capped


def _build_heartbeat_context_summary(messages: list[dict]) -> dict:
    lines = [
        "[Heartbeat context compacted]",
        f"Compacted {len(messages)} older heartbeat messages before invocation.",
        "This preserves continuity without replaying the full raw heartbeat transcript.",
    ]
    for idx, message in enumerate(messages[-12:], start=max(1, len(messages) - 11)):
        content = str(message.get("content") or "").replace("\n", " ").strip()
        if len(content) > 260:
            content = content[:260] + "..."
        lines.append(f"- #{idx} {message.get('role', 'unknown')}: {content}")
    return {
        "role": "system",
        "content": _truncate_heartbeat_text(
            "\n".join(lines),
            _HEARTBEAT_COMPACT_SUMMARY_MAX_CHARS,
            "heartbeat context summary",
        ),
    }


def _compact_heartbeat_runtime_messages(messages: list[dict]) -> list[dict]:
    """Keep heartbeat continuity bounded before it reaches the kernel.

    Kernel compaction handles multi-message LLM loops, but heartbeat can create
    a single very large initialization message from T2/T3 files. This guard caps
    both single-message payloads and accumulated KAIROS history deterministically.

    C1 (docs/agent-lifecycle-cc-alignment.md 主题 C): full fidelity first —
    when the whole context fits the total budget, NOTHING is trimmed (the
    curator decides on complete input). Per-message caps and compaction engage
    only once the total exceeds the budget (mechanical only as observable
    fallback, same philosophy as compaction P0).
    """
    if not messages:
        return []

    raw = [dict(message) for message in messages]
    if _heartbeat_context_chars(raw) <= _HEARTBEAT_CONTEXT_MAX_CHARS:
        return raw

    capped = [_cap_heartbeat_message(message) for message in messages]
    if _heartbeat_context_chars(capped) <= _HEARTBEAT_CONTEXT_MAX_CHARS:
        return capped

    keep_recent = min(_HEARTBEAT_RECENT_MESSAGE_COUNT, max(0, len(capped) - 1))
    first = [_cap_heartbeat_message(capped[0])]
    recent = capped[-keep_recent:] if keep_recent else []
    middle = capped[1:-keep_recent] if keep_recent else capped[1:]
    summary = _build_heartbeat_context_summary(middle)
    compacted = first + [summary] + recent

    if _heartbeat_context_chars(compacted) <= _HEARTBEAT_CONTEXT_MAX_CHARS:
        return compacted

    remaining = max(_HEARTBEAT_CONTEXT_MAX_CHARS - _heartbeat_content_chars(summary), 1)
    first_budget = min(_HEARTBEAT_MESSAGE_MAX_CHARS, max(4_000, remaining // 3))
    recent_budget = max(1_000, (remaining - first_budget) // max(len(recent), 1))
    compacted = [_cap_heartbeat_message(first[0], first_budget), summary]
    compacted.extend(_cap_heartbeat_message(message, recent_budget) for message in recent)

    # Final defensive pass for pathological inputs.
    while _heartbeat_context_chars(compacted) > _HEARTBEAT_CONTEXT_MAX_CHARS:
        largest_idx = max(range(len(compacted)), key=lambda idx: _heartbeat_content_chars(compacted[idx]))
        largest = compacted[largest_idx]
        largest_chars = _heartbeat_content_chars(largest)
        if largest_chars <= 1_000:
            break
        overflow = _heartbeat_context_chars(compacted) - _HEARTBEAT_CONTEXT_MAX_CHARS
        compacted[largest_idx] = _cap_heartbeat_message(largest, max(1_000, largest_chars - overflow - 200))

    return compacted


async def _create_heartbeat_runtime_task(agent_id: uuid.UUID) -> str | None:
    try:
        return await create_runtime_task_record(
            task_id=uuid.uuid4().hex,
            task_type="heartbeat",
            status="running",
            parent_agent_id=agent_id,
            prompt="Heartbeat self-evolution tick",
            metadata_json={"source": "heartbeat", "agent_id": str(agent_id)},
        )
    except Exception as exc:
        logger.warning("[Heartbeat] Failed to create RuntimeTask for {}: {}", agent_id, exc)
        return None


async def _update_heartbeat_runtime_task(
    runtime_task_id: str | None,
    *,
    status: str,
    result_summary: str,
    session_id: str | None = None,
    metadata_json: dict | None = None,
) -> None:
    if not runtime_task_id:
        return
    fields = {
        "status": status,
        "result_summary": result_summary[:2000],
        "metadata_json": metadata_json or {},
    }
    if session_id:
        fields["child_session_id"] = session_id
    try:
        await update_runtime_task_record(runtime_task_id, **fields)
    except Exception as exc:
        logger.warning("[Heartbeat] Failed to update RuntimeTask {}: {}", runtime_task_id, exc)


async def _skip_heartbeat_runtime_task(
    runtime_task_id: str | None,
    *,
    skip_reason: str,
    result_summary: str,
    metadata_json: dict | None = None,
) -> None:
    metadata = {"skip_reason": skip_reason}
    metadata.update(metadata_json or {})
    await _update_heartbeat_runtime_task(
        runtime_task_id,
        status="skipped",
        result_summary=result_summary,
        metadata_json=metadata,
    )


def _reset_heartbeat_session(agent_id: uuid.UUID) -> None:
    """Reset heartbeat persistent session (called after dream, day change, or process restart)."""
    _heartbeat_contexts.pop(agent_id, None)
    _heartbeat_session_ids.pop(agent_id, None)
    _heartbeat_tick_counts.pop(agent_id, None)
    _t2_mtimes.pop(agent_id, None)
    _heartbeat_session_ctxs.pop(agent_id, None)
    _clear_heartbeat_checkpoint(agent_id)
    logger.info("[Heartbeat] Session reset for {}", agent_id)


def _has_complete_heartbeat_session_state(agent_id: uuid.UUID) -> bool:
    has_context = agent_id in _heartbeat_contexts
    has_session_id = agent_id in _heartbeat_session_ids
    if has_context and has_session_id:
        return True
    if _restore_heartbeat_checkpoint(agent_id):
        return True
    if has_context or has_session_id:
        logger.warning("[Heartbeat] Incomplete persistent session state for {}; resetting cache", agent_id)
        _reset_heartbeat_session(agent_id)
    return False


def _get_or_create_heartbeat_session_ctx(agent_id: uuid.UUID, session_id: uuid.UUID) -> "SessionContext":
    """Return a persistent SessionContext for heartbeat ticks.

    On first call for an agent, creates a new context; subsequent calls
    reuse it so the kernel's frozen prompt prefix cache carries across ticks.
    """
    ctx = _heartbeat_session_ctxs.get(agent_id)
    if ctx is not None:
        # Update session_id if it changed (e.g. after day-boundary reset)
        ctx.session_id = str(session_id)
        return ctx
    ctx = SessionContext(
        source="heartbeat",
        channel="heartbeat",
        session_id=str(session_id),
        metadata={"agent_id": str(agent_id)},
    )
    _heartbeat_session_ctxs[agent_id] = ctx
    return ctx


def _heartbeat_checkpoint_path(agent_id: uuid.UUID) -> Path:
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "memory" / _HEARTBEAT_CHECKPOINT_FILENAME


def _save_heartbeat_checkpoint(
    agent_id: uuid.UUID,
    *,
    session_id: uuid.UUID,
    tick_count: int,
    runtime_messages: list[dict],
    t2_mtimes: dict[str, float] | None = None,
) -> None:
    path = _heartbeat_checkpoint_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": str(session_id),
        "tick_count": max(0, int(tick_count)),
        "runtime_messages": _compact_heartbeat_runtime_messages(runtime_messages),
        "t2_mtimes": {str(key): float(value) for key, value in (t2_mtimes or {}).items()},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _restore_heartbeat_checkpoint(agent_id: uuid.UUID) -> bool:
    path = _heartbeat_checkpoint_path(agent_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[Heartbeat] Failed to restore checkpoint for {}: {}", agent_id, exc)
        return False

    try:
        session_id = uuid.UUID(str(payload.get("session_id")))
    except (TypeError, ValueError):
        logger.warning("[Heartbeat] Invalid checkpoint session_id for {}", agent_id)
        return False
    messages = payload.get("runtime_messages")
    if not isinstance(messages, list) or not messages:
        return False

    _heartbeat_session_ids[agent_id] = session_id
    _heartbeat_contexts[agent_id] = _compact_heartbeat_runtime_messages([m for m in messages if isinstance(m, dict)])
    if not _heartbeat_contexts[agent_id]:
        _reset_heartbeat_session(agent_id)
        return False
    try:
        _heartbeat_tick_counts[agent_id] = max(0, int(payload.get("tick_count", 0)))
    except (TypeError, ValueError):
        _heartbeat_tick_counts[agent_id] = 0
    mtimes = payload.get("t2_mtimes") or {}
    if isinstance(mtimes, dict):
        _t2_mtimes[agent_id] = {str(key): float(value) for key, value in mtimes.items()}
    logger.info("[Heartbeat] Restored KAIROS checkpoint for {}", agent_id)
    return True


def _clear_heartbeat_checkpoint(agent_id: uuid.UUID) -> None:
    try:
        _heartbeat_checkpoint_path(agent_id).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("[Heartbeat] Failed to clear checkpoint for {}: {}", agent_id, exc)


def _read_t2_full(agent_id: uuid.UUID) -> str:
    """Read all T2 learnings files (full content) for first tick initialization."""
    from app.config import get_settings

    entries, current_mtimes = load_t2_entries(Path(get_settings().AGENT_DATA_DIR), agent_id)
    _t2_mtimes[agent_id] = current_mtimes
    snapshot = render_t2_snapshot(entries)
    return _truncate_heartbeat_text(
        snapshot or "(no learnings yet)",
        _HEARTBEAT_T2_FULL_MAX_CHARS,
        "T2 full snapshot",
    )


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
                logger.debug("[Heartbeat] Failed to read T3 {}: {}", fpath, exc)
    return _truncate_heartbeat_text(
        "\n\n".join(parts) if parts else "(no memory files)",
        _HEARTBEAT_T3_MAX_CHARS,
        "T3 summary",
    )


def _read_incremental_t2(agent_id: uuid.UUID) -> str:
    """Read only new T2 entries since last tick (via mtime comparison)."""
    from app.config import get_settings

    entries, current_mtimes = load_incremental_t2_entries(
        Path(get_settings().AGENT_DATA_DIR),
        agent_id,
        _t2_mtimes.get(agent_id, {}),
    )
    _t2_mtimes[agent_id] = current_mtimes
    return _truncate_heartbeat_text(
        render_t2_snapshot(entries),
        _HEARTBEAT_T2_INCREMENTAL_MAX_CHARS,
        "incremental T2 snapshot",
    )


def _get_default_heartbeat_instruction() -> str:
    """Read default heartbeat instruction from templates/HEARTBEAT.md (single source of truth)."""
    try:
        return _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8").strip()
    except Exception as exc:
        # Template-read failure is a packaging bug — the whole T2→T3 curation SOP
        # silently degrades to a one-liner stub while the distiller keeps running.
        logger.error("[Heartbeat] HEARTBEAT.md template read failed, using stub SOP: {}", exc)
        return (
            "[Heartbeat] Review your recent work and memory, do one evidence-backed useful thing, "
            "reply HEARTBEAT_OK if nothing needed."
        )


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
        logger.debug("[Heartbeat] Redis lease unavailable, falling back to local lease: {}", exc)
        return _try_acquire_heartbeat_lease(agent_id, now=now, ttl_seconds=ttl_seconds)


async def _release_heartbeat_lease_async(agent_id: uuid.UUID) -> None:
    lease_key = f"heartbeat_lease:{agent_id}"
    try:
        redis = await get_redis()
        await redis.delete(lease_key)
    except Exception as exc:
        logger.debug("[Heartbeat] Redis lease release skipped: {}", exc)
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


_SKILL_OPPORTUNITY_COOLDOWN_TICKS = 5  # ~3.75 hours at 45-minute ticks
_SKILL_OPPORTUNITY_STATE_FILENAME = "skill_opportunity_cooldown.json"


def _load_skill_opportunity_state(ws_root) -> dict:
    import json

    if ws_root is None:
        return {}
    path = ws_root / "evolution" / _SKILL_OPPORTUNITY_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[Heartbeat] failed to read skill opportunity state: {}", exc)
        return {}


def _save_skill_opportunity_state(ws_root, *, tick: int, tools: list[str]) -> None:
    import json

    if ws_root is None:
        return
    try:
        evo_dir = ws_root / "evolution"
        evo_dir.mkdir(parents=True, exist_ok=True)
        (evo_dir / _SKILL_OPPORTUNITY_STATE_FILENAME).write_text(
            json.dumps({"tick": tick, "tools": sorted(tools)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("[Heartbeat] failed to write skill opportunity state: {}", exc)


def _skill_already_covers_tools(ws_root, frequent_tools: list[str]) -> str | None:
    """If an existing skill declares tools ⊇ frequent_tools, return its name (skip suggestion)."""
    if ws_root is None or not frequent_tools:
        return None
    try:
        from app.skills import SkillRegistry, WorkspaceSkillLoader

        loader = WorkspaceSkillLoader()
        registry = SkillRegistry()
        registry.register_many(loader.load_from_workspace(ws_root))
    except Exception as exc:
        logger.debug("[Heartbeat] skill coverage check failed: {}", exc)
        return None

    target = set(frequent_tools)
    for name in registry.names():
        parsed = registry.resolve(name)
        declared = set(parsed.metadata.declared_tools or ())
        if target.issubset(declared):
            return name
    return None


async def _build_evolution_context(
    agent_id: uuid.UUID,
    recent_activities: list,
    tick_count: int = 0,
    *,
    owner_id: uuid.UUID | str | None = None,
    owner_name: str | None = None,
    company_id: uuid.UUID | str | None = None,
    company_name: str | None = None,
) -> str:
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

        try:
            from app.services.agency_charter import build_default_accountability_context
            from app.services.proactive_employee_loop import build_proactive_employee_plan

            proactive_plan = build_proactive_employee_plan(
                agent_id=str(agent_id),
                accountability=build_default_accountability_context(
                    company_id=str(company_id or "heartbeat-company"),
                    company_name=company_name or "Company",
                    owner_id=str(owner_id or agent_id),
                    owner_name=owner_name or "Owner",
                    current_user_id=str(owner_id or agent_id),
                    current_user_name=owner_name or "Owner",
                ),
                recent_activities=recent_activities,
            )
            if proactive_plan.markdown:
                parts.append(proactive_plan.markdown)
        except Exception as exc:
            logger.warning("[Heartbeat] proactive steward context skipped for {}: {}", agent_id, exc)

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

            should_push = bool(frequent_tools)
            suppression_note: str | None = None

            if should_push:
                # Coverage check — skip if an existing skill already declares these tools.
                covered_by = _skill_already_covers_tools(ws_root, frequent_tools)
                if covered_by:
                    should_push = False
                    suppression_note = f"skill '{covered_by}' already covers tools {sorted(frequent_tools)}"

            if should_push:
                # Cooldown — skip if the same tool set was suggested recently.
                state = _load_skill_opportunity_state(ws_root)
                last_tick = int(state.get("tick", 0)) if isinstance(state.get("tick"), (int, float)) else 0
                last_tools = sorted(state.get("tools", []) or [])
                if (
                    last_tools == sorted(frequent_tools)
                    and tick_count
                    and tick_count - last_tick < _SKILL_OPPORTUNITY_COOLDOWN_TICKS
                ):
                    should_push = False
                    suppression_note = (
                        f"cooldown: same tools suggested at tick {last_tick} "
                        f"(<{_SKILL_OPPORTUNITY_COOLDOWN_TICKS} ticks ago)"
                    )

            if should_push and frequent_tools:
                parts.append(
                    "\n---\n## Skill Candidate Opportunity\n"
                    f"You have used these tools repeatedly: {', '.join(frequent_tools)}.\n"
                    "If the workflow around them is genuinely reusable, record it as a candidate "
                    "signal — you curate evidence; the skill distillation lane decides promotion:\n"
                    "1. FIRST call `tool_search` and `load_skill` to confirm no existing skill already covers this workflow\n"
                    '2. If none covers it, call `save_memory` with category="strategy", '
                    'container_candidate="skill_candidate", and a self-contained description of the '
                    "workflow (tools in sequence, when to use it, how to verify success)\n"
                    "3. Include `source_refs` pointing at the sessions/evidence where the workflow repeated\n"
                    "4. A good candidate captures the *workflow* (multiple tools in sequence), not a single tool or one-off note\n"
                    "This counts as a high-value heartbeat action (score 7+)."
                )
                if tick_count:
                    _save_skill_opportunity_state(ws_root, tick=tick_count, tools=list(frequent_tools))
            elif suppression_note:
                logger.debug(
                    "[Heartbeat] skill opportunity suppressed for {}: {}",
                    agent_id,
                    suppression_note,
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
                "Proceed directly with normal heartbeat: review your recent work and memory, then do one small evidence-backed task.\n"
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
                "Focus on ONE simple action: review your recent work and memory, then do something small with evidence.\n"
                "Output: [OUTCOME:action_taken] [SCORE:3]"
            )
        else:
            parts.append(
                "\n---\n## Bootstrap Mode (first heartbeats)\n"
                "You have very little activity history. This is normal for a new agent.\n"
                "Instead of the normal heartbeat protocol, do these bootstrapping steps:\n"
                "1. **Read soul.md** — understand your identity and role\n"
                "2. **List and read your skills/** — understand your capabilities\n"
                "3. **Write to evolution/lineage.md** with your bootstrap observations\n"
                "6. Output: [OUTCOME:action_taken] [SCORE:3]\n\n"
                "After bootstrapping, future heartbeats will follow the normal 4-phase protocol."
            )

    return _truncate_heartbeat_text(
        "\n\n".join(parts) if parts else "",
        _HEARTBEAT_EVOLUTION_CONTEXT_MAX_CHARS,
        "heartbeat evolution context",
    )


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
            logger.debug("[Heartbeat] Failed to load lineage archive: {}", load_err)

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
                    logger.debug("[Heartbeat] Failed to parse score: {}", parse_err)
        if entry.get("date") or entry.get("strategy"):
            existing.append(entry)

    # Cap archive size
    existing = existing[-_LINEAGE_ARCHIVE_MAX:]
    try:
        archive_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("[Heartbeat] Archived {} rotated lineage entries for {}", len(discarded_segments), agent_id)
    except Exception as write_err:
        logger.debug("[Heartbeat] Failed to write lineage archive: {}", write_err)


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
            logger.debug("[Heartbeat] Failed to clean up temp file {}: {}", tmp_path, unlink_exc)
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
        logger.debug("[Heartbeat] No workspace found for evolution writeback: {}", agent_id)
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
                    "[Heartbeat] Lineage entry {} already exists (agent-written), skipping server append", entry_marker
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
                        logger.info("[Heartbeat] Auto-blocked approach for agent {}: {}", agent_id, summary[:80])
            except Exception as bl_err:
                logger.debug("[Heartbeat] Blocklist auto-append failed: {}", bl_err)

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

        if tool_name == "save_skill":
            # Spec §12 P4: the Memory Curator records candidate signals only;
            # skill creation runs through the SkillDistiller candidate lane.
            return (
                "[BLOCKED] Heartbeat does not write skills directly. Record the evidence as a "
                'candidate signal instead: save_memory(category="strategy", '
                'container_candidate="skill_candidate", content="<the reusable workflow, '
                'self-contained>", source_refs=[...]). The skill distillation lane consumes it.'
            )

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


async def _touch_last_heartbeat(agent_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> None:
    """Update last_heartbeat_at even on early return to prevent infinite re-triggering."""
    try:
        from app.models.agent import Agent as _Agent

        async with tenant_scoped_session(tenant_id) as _db:
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
        logger.warning("[Heartbeat] Skill distillation failed for {}: {}", agent_id, exc)
        return None


def _maybe_run_skill_curator(workspace: Path) -> dict | None:
    """Run the skill curator decay pass for this agent's workspace.

    Counterpart to skill distillation: distillation only adds skills, the
    curator marks unused agent-authored skills stale and archives long-dormant
    ones (never deletes). Synchronous file IO; failures are logged, never
    propagated into the heartbeat tick.
    """
    try:
        from app.services.skill_curator import run_skill_curator_pass

        return run_skill_curator_pass(workspace)
    except Exception as exc:
        logger.warning("[Heartbeat] Skill curator pass failed for {}: {}", workspace, exc)
        return None


async def _execute_heartbeat(agent_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None, lease_acquired: bool = False):
    """Execute a single heartbeat for an agent.

    Creates a Reflection Session (like trigger_daemon) so tool calls and
    the final reply are persisted and visible in the UI.

    ``tenant_id`` is threaded from ``_heartbeat_tick`` (which already filtered
    on it) so every session here can pin the RLS GUC — under enforced
    (non-owner) RLS a bare session would fail-closed even on the agent's own
    rows. Falls back to an audited bypass read when omitted (e.g. an isolated
    re-invocation without the tick's tenant in scope).
    """
    if tenant_id is None:
        tenant_id = await resolve_tenant_for_agent(agent_id)
    runtime_task_id: str | None = None
    heartbeat_session_id: str | None = None
    lease_held = lease_acquired
    if not lease_held:
        lease_held = await _try_acquire_heartbeat_lease_async(agent_id)
        if not lease_held:
            logger.info("[Heartbeat] Skip duplicate in-flight heartbeat for {}", agent_id)
            runtime_task_id = await _create_heartbeat_runtime_task(agent_id)
            await _skip_heartbeat_runtime_task(
                runtime_task_id,
                skip_reason="duplicate_in_flight",
                result_summary="Skipped heartbeat because another heartbeat is already in flight.",
            )
            return

    runtime_task_id = await _create_heartbeat_runtime_task(agent_id)

    import json as _json

    try:
        from app.models.agent import Agent
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.models.llm import LLMModel
        from app.models.participant import Participant

        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"[Heartbeat] Agent {agent_id} not found in DB — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_found",
                    result_summary=f"Skipped heartbeat because agent {agent_id} was not found.",
                )
                return

            # Set execution identity — autonomous heartbeat action
            from app.core.execution_context import set_agent_bot_identity

            set_agent_bot_identity(agent_id, agent.name, source="heartbeat")

            if not (agent.primary_model_id or agent.fallback_model_id):
                logger.warning(f"[Heartbeat] Agent {agent.name} ({agent_id}) has no model configured — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="no_model",
                    result_summary=f"Skipped heartbeat because agent {agent.name} has no primary or fallback model configured.",
                )
                return

            model = None
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_result.scalar_one_or_none()

            fallback_model = None
            if agent.fallback_model_id:
                fallback_result = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
                )
                fallback_model = fallback_result.scalar_one_or_none()

            if model and agent.tenant_id:
                from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

                default_runtime_model = await resolve_default_model_for_tenant(
                    db,
                    agent.tenant_id,
                    exclude_model_id=model.id,
                )
                model, fallback_model = choose_runtime_model_pair(model, fallback_model, default_runtime_model)
            elif fallback_model:
                from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

                model = fallback_model
                fallback_model = None
                default_runtime_model = None
                if agent.tenant_id:
                    default_runtime_model = await resolve_default_model_for_tenant(
                        db,
                        agent.tenant_id,
                        exclude_model_id=model.id,
                    )
                model, fallback_model = choose_runtime_model_pair(model, fallback_model, default_runtime_model)

            if not model:
                logger.warning(f"[Heartbeat] Model for agent {agent.name} ({agent_id}) not found — skipping")
                await _touch_last_heartbeat(agent_id, tenant_id)
                await _skip_heartbeat_runtime_task(
                    runtime_task_id,
                    skip_reason="model_not_found",
                    result_summary=f"Skipped heartbeat because configured model was not found for {agent.name}.",
                    metadata_json={
                        "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                        "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
                    },
                )
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
            except Exception as e:
                logger.warning(f"Failed to fetch recent activities for heartbeat: {e}")
                recent_activities = []

            # ── KAIROS persistent session: first tick vs subsequent tick ──
            has_persistent_session = _has_complete_heartbeat_session_state(agent_id)
            tick_count = _heartbeat_tick_counts.get(agent_id, 0) + 1
            _heartbeat_tick_counts[agent_id] = tick_count

            try:
                evolution_context = await _build_evolution_context(
                    agent_id,
                    recent_activities,
                    tick_count=tick_count,
                    owner_id=agent.creator_id,
                    company_id=agent.tenant_id,
                )
            except Exception as e:
                logger.warning(f"Failed to build evolution context for heartbeat: {e}")
                evolution_context = ""

            # Resolve participant for DB session
            p_result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = p_result.scalar_one_or_none()
            agent_participant_id = agent_participant.id if agent_participant else None

            if not has_persistent_session:
                # ═══ First tick: full initialization ═══
                heartbeat_instruction = _load_heartbeat_instruction(agent_id)
                if evolution_context:
                    heartbeat_instruction += "\n\n" + evolution_context

                # Inject T2 learnings (full) + T3 memory (reference for dedup)
                t2_content = _read_t2_full(agent_id)
                t3_summary = _read_t3_summary(agent_id)
                heartbeat_instruction += f"\n\n## Current T2 Learnings\n{t2_content}"
                heartbeat_instruction += f"\n\n## Current T3 Memory (reference — don't duplicate these)\n{t3_summary}"

                runtime_messages = _compact_heartbeat_runtime_messages(
                    [{"role": "user", "content": heartbeat_instruction}]
                )

                # Create new DB session (only on first tick)
                session = ChatSession(
                    agent_id=agent_id,
                    user_id=agent.creator_id,
                    participant_id=agent_participant_id,
                    source_channel="heartbeat",
                    title=f"💓 Heartbeat: {agent.name}"[:200],
                )
                db.add(session)
                await db.flush()
                session_id = session.id
                _heartbeat_session_ids[agent_id] = session_id
                heartbeat_session_id = str(session_id)

                # Save heartbeat instruction as first message
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=str(session_id),
                        role="user",
                        content=heartbeat_instruction[:4000],
                        user_id=agent.creator_id,
                        participant_id=agent_participant_id,
                    )
                )
                await db.commit()
                _save_heartbeat_checkpoint(
                    agent_id,
                    session_id=session_id,
                    tick_count=tick_count,
                    runtime_messages=runtime_messages,
                    t2_mtimes=_t2_mtimes.get(agent_id, {}),
                )
                logger.info("[Heartbeat] Tick #{} (full init) for {}", tick_count, agent.name)
            else:
                # ═══ Subsequent tick: <tick> + incremental T2 ═══
                new_t2 = _read_incremental_t2(agent_id)
                if not new_t2:
                    # Idle protection: no new T2 entries → skip this tick
                    logger.info("[Heartbeat] Skip tick #{} for {}: no new T2 entries", tick_count, agent.name)
                    await _release_heartbeat_lease_async(agent_id)
                    await _touch_last_heartbeat(agent_id, tenant_id)
                    return

                session_id = _heartbeat_session_ids[agent_id]
                heartbeat_session_id = str(session_id)
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
                _save_heartbeat_checkpoint(
                    agent_id,
                    session_id=session_id,
                    tick_count=tick_count,
                    runtime_messages=runtime_messages,
                    t2_mtimes=_t2_mtimes.get(agent_id, {}),
                )
                logger.info(
                    "[Heartbeat] Tick #{} (incremental, {} new entries) for {}",
                    tick_count,
                    new_t2.count("\n") + 1,
                    agent.name,
                )

            runtime_messages = _compact_heartbeat_runtime_messages(runtime_messages)

            # Tool call persistence callback
            async def _on_tool_call(data: dict) -> None:
                if data.get("status") != "done":
                    return
                try:
                    async with tenant_scoped_session(tenant_id) as _tc_db:
                        _tc_db.add(
                            ChatMessage(
                                agent_id=agent_id,
                                tenant_id=tenant_id,
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
                        fallback_model=fallback_model,
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
                        session_context=_get_or_create_heartbeat_session_ctx(agent_id, session_id),
                        on_tool_call=_on_tool_call,
                        tool_executor=_build_heartbeat_tool_executor(agent_id, agent.creator_id),
                        core_tools_only=False,
                        max_tool_rounds=_HEARTBEAT_MAX_TOOL_ROUNDS,
                    )
                ),
                timeout=_HEARTBEAT_TIMEOUT_SECONDS,
            )
            reply = result.content

            # KAIROS: append assistant response to persistent context
            runtime_messages.append({"role": "assistant", "content": reply or ""})
            if _heartbeat_session_ids.get(agent_id) == session_id:
                _heartbeat_contexts[agent_id] = _compact_heartbeat_runtime_messages(runtime_messages)
                _save_heartbeat_checkpoint(
                    agent_id,
                    session_id=session_id,
                    tick_count=tick_count,
                    runtime_messages=_heartbeat_contexts[agent_id],
                    t2_mtimes=_t2_mtimes.get(agent_id, {}),
                )
            else:
                logger.info(
                    "[Heartbeat] Session cache for {} was reset during execution; not restoring stale context",
                    agent_id,
                )

            try:
                from app.config import get_settings

                data_root = Path(get_settings().AGENT_DATA_DIR)
                absorbed = mark_t2_entries_absorbed(
                    data_root,
                    agent_id,
                    filenames=list((_t2_mtimes.get(agent_id) or {}).keys()) or None,
                )
                if absorbed:
                    _entries, current_mtimes = load_t2_entries(data_root, agent_id)
                    _t2_mtimes[agent_id] = current_mtimes
                    if _heartbeat_session_ids.get(agent_id) == session_id:
                        _save_heartbeat_checkpoint(
                            agent_id,
                            session_id=session_id,
                            tick_count=tick_count,
                            runtime_messages=_heartbeat_contexts.get(agent_id, runtime_messages),
                            t2_mtimes=current_mtimes,
                        )
                    logger.info("[Heartbeat] Marked {} T2 entries absorbed for {}", absorbed, agent_id)
            except Exception as _t2_absorb_err:
                logger.warning("[Heartbeat] Failed to mark T2 entries absorbed for {}: {}", agent_id, _t2_absorb_err)

            # Save assistant reply to Reflection Session
            async with tenant_scoped_session(tenant_id) as db2:
                db2.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=tenant_id,
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
            async with tenant_scoped_session(tenant_id) as db3:
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
                # P0-1b: skip skill distillation when tenant cannot be resolved.
                # Distiller writes persistent skill files; without tenant context
                # we cannot enforce capability policy. Surface as observability
                # signal rather than failing the heartbeat tick.
                if runtime_config.tenant_resolution_error:
                    logger.warning(
                        "[Heartbeat] Skipping skill distillation for {} — tenant resolution failed: {}",
                        agent_id,
                        runtime_config.tenant_resolution_error,
                    )
                else:
                    workspace = await ensure_workspace(
                        agent_id, tenant_id=str(agent.tenant_id) if agent.tenant_id else None
                    )
                    await _maybe_run_skill_distillation(
                        agent_id=agent_id,
                        workspace=workspace,
                        tenant_id=agent.tenant_id,
                        runtime_config=runtime_config,
                        model=model,
                        current_session_id=str(session_id),
                    )
                    # Negative pressure: decay/archive unused agent-authored
                    # skills so the catalog doesn't grow unbounded.
                    _maybe_run_skill_curator(workspace)
            except Exception as _distill_err:
                logger.warning("[Heartbeat] Skill distillation setup failed for {}: {}", agent_id, _distill_err)

            # Scene/wiki curation (spec §12 P5): consolidate new knowledge/
            # strategy entries into scene narratives and wiki concept pages.
            # Cursor-gated, candidate-first, never breaks the tick.
            try:
                from app.services.memory_curation import run_scene_wiki_curation_tick

                curation_summary = await run_scene_wiki_curation_tick(agent_id, agent.tenant_id)
                if curation_summary.get("status") == "ran":
                    logger.info("[Heartbeat] Scene/wiki curation for {}: {}", agent_id, curation_summary)
            except Exception as _curation_err:
                logger.warning("[Heartbeat] Scene/wiki curation failed for {}: {}", agent_id, _curation_err)

            # Count PRODUCTIVE heartbeats toward the auto-dream gate so agents
            # with low user-chat but real autonomous output still distill —
            # idle ticks (OUTCOME:noop) are not activity.
            try:
                from app.services.auto_dream import record_dream_activity, should_dream, run_dream

                record_dream_activity(agent_id, outcome_type)
                if should_dream(agent_id) and agent.tenant_id:
                    asyncio.create_task(run_dream(agent_id, agent.tenant_id))
                    logger.info("[Heartbeat] Auto-dream triggered for agent {}", agent_id)
            except Exception as _dream_err:
                logger.debug("[Heartbeat] Auto-dream check failed: {}", _dream_err)

            # NOTE: Heartbeat outcomes are no longer written directly into long-term
            # memory here. Evolution files are the intermediate source; dream curates
            # durable entries into canonical markdown memory on the next cycle.

            # PR-9: scrub any T3 rows the LLM may have written off-spec so
            # dream's parser sees them. Must run BEFORE the T0 hook so the
            # normalization report rides along in the heartbeat MD.
            normalization_report: dict = {"fixed": 0, "warnings": [], "files_touched": []}
            try:
                from app.config import get_settings as _get_settings
                from app.memory.md_store import validate_and_normalize_t3

                normalization_report = validate_and_normalize_t3(Path(_get_settings().AGENT_DATA_DIR), agent_id)
                if normalization_report["fixed"] or normalization_report["warnings"]:
                    logger.info(
                        "[Heartbeat] T3 normalization for {}: fixed={} warnings={} files={}",
                        agent_id,
                        normalization_report["fixed"],
                        len(normalization_report["warnings"]),
                        normalization_report["files_touched"],
                    )
            except Exception as _nrm_err:
                logger.debug("[Heartbeat] T3 normalization failed (non-fatal): {}", _nrm_err)

            # Sync normalized T3 MD to Hindsight bank (no-op when backend=md).
            # Runs AFTER normalization so cursor mtime reflects the canonical state.
            try:
                from app.memory.hindsight_sync import sync_t3_to_hindsight

                synced = await sync_t3_to_hindsight(agent_id, agent.tenant_id)
                if synced:
                    logger.info(
                        "[Heartbeat] Hindsight sync: {} T3 items (agent={})",
                        synced,
                        agent_id,
                    )
            except Exception as _hs_err:
                # sync_t3_to_hindsight already has its own try/except and only
                # returns here on truly unexpected paths (import error, etc).
                # Warning-level so ops actually see these regressions.
                logger.warning("[Heartbeat] Hindsight sync outer guard tripped: {}", _hs_err)

            # Emit HEARTBEAT_TICK_END hook → T0 log
            try:
                from app.runtime.hooks import HookEvent, emit_hook

                # Derive `reasoning` from the last assistant text so the
                # T0 system/heartbeat-*.md log records WHY the tick chose
                # this outcome (PR-3: heartbeat decision audit trail).
                reasoning_text = ""
                for _msg in reversed(runtime_messages or []):
                    if not isinstance(_msg, dict):
                        continue
                    if _msg.get("role") != "assistant":
                        continue
                    _c = _msg.get("content")
                    if isinstance(_c, str) and _c.strip():
                        reasoning_text = _c.strip()
                        break
                    if isinstance(_c, list):
                        _texts = [str(p.get("text", "")) for p in _c if isinstance(p, dict) and p.get("type") == "text"]
                        if _texts:
                            reasoning_text = " ".join(_texts).strip()
                            break

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
                        "reasoning": reasoning_text,
                        "t3_normalization": normalization_report,
                    },
                )
            except Exception as _hook_err:
                logger.debug("[Heartbeat] HEARTBEAT_TICK_END hook failed (non-fatal): {}", _hook_err)

            # Bootstrap validation: verify key files exist regardless of outcome
            # (cold_start agents need validation even on failure/noop)
            _validate_bootstrap_completion(agent_id)

            score_str = f" score={heartbeat_score}" if heartbeat_score is not None else ""
            logger.info(f"💓 Heartbeat for {agent.name}: {outcome_type}{score_str} — {summary}")
            await _update_heartbeat_runtime_task(
                runtime_task_id,
                status="completed",
                result_summary=f"Heartbeat [{outcome_type}]: {summary}",
                session_id=heartbeat_session_id,
                metadata_json={"outcome": outcome_type, "score": heartbeat_score},
            )

    except Exception as e:
        error_text = _format_heartbeat_exception(e)
        logger.error(f"Heartbeat error for agent {agent_id}: {error_text}", exc_info=True)
        # CRITICAL: Update last_heartbeat_at even on failure to prevent
        # every-minute storm (if timestamp stays None, agent is always eligible)
        try:
            async with tenant_scoped_session(tenant_id) as _db:
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
                f"Heartbeat crash: {error_text[:80]}",
                detail={"outcome_type": "crash", "error": error_text[:300]},
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
                f"crash: {error_text[:60]}",
                source="heartbeat",
            )
        except Exception as _evo_crash_err:
            logger.debug(f"[Heartbeat] Evolution writeback on crash failed: {_evo_crash_err}")
        await _update_heartbeat_runtime_task(
            runtime_task_id,
            status="failed",
            result_summary=f"Heartbeat failed: {error_text[:500]}",
            session_id=heartbeat_session_id,
            metadata_json={"error": error_text[:1000]},
        )
    finally:
        if lease_held:
            await _release_heartbeat_lease_async(agent_id)


async def _heartbeat_tick():
    """One heartbeat tick: find agents due for heartbeat."""
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.audit_logger import write_audit_log

    now = datetime.now(timezone.utc)

    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="heartbeat tick — enumerate all running/idle agents across tenants"),
        ):
            result = await db.execute(
                select(Agent).where(
                    Agent.status.in_(["running", "idle"]),
                )
            )
            agents = result.scalars().all()

            # Workspace sync moved to _workspace_sync_loop (600s cadence).
            # Keeping it inline blocked the 60s heartbeat tick on Volume I/O.

            triggered = 0
            skipped_hours = 0
            skipped_interval = 0
            for agent in agents:
                if agent.tenant_id is None:
                    skipped_interval += 1
                    continue

                interval = timedelta(minutes=managed_heartbeat_interval_minutes())
                if agent.last_heartbeat_at and (now - agent.last_heartbeat_at) < interval:
                    skipped_interval += 1
                    continue

                # Fire heartbeat
                if not await _try_acquire_heartbeat_lease_async(agent.id, now=now):
                    logger.info(f"[Heartbeat] Agent {agent.name} already has an in-flight heartbeat")
                    continue
                logger.info(f"💓 Triggering heartbeat for {agent.name}")
                await write_audit_log("heartbeat_fire", {"agent_name": agent.name}, agent_id=agent.id)
                asyncio.create_task(_execute_heartbeat(agent.id, tenant_id=agent.tenant_id, lease_acquired=True))
                triggered += 1

            logger.info(
                f"[Heartbeat] tick: eligible={len(agents)}, triggered={triggered},"
                f" skipped_hours={skipped_hours}, skipped_interval={skipped_interval}"
            )

    except Exception as e:
        logger.error(f"Heartbeat tick error: {e}", exc_info=True)
        await write_audit_log("heartbeat_error", {"error": str(e)[:300]})

    # P1-W2-7: prune skills idle past TTL across all cached heartbeat
    # session contexts. Web-chat sessions are recreated per request, so
    # they don't accumulate; only the long-lived heartbeat ctxs need this.
    try:
        total_pruned = 0
        for ctx in list(_heartbeat_session_ctxs.values()):
            dropped = ctx.prune_expired_skills()
            total_pruned += len(dropped)
        if total_pruned:
            logger.info(f"[Heartbeat] Pruned {total_pruned} expired skill activations")
    except Exception as e:
        logger.warning(f"[Heartbeat] Skill prune failed (non-fatal): {e}")


async def _sync_one_tenant(tenant_id: uuid.UUID) -> None:
    """Run sync_all_for_tenant in an isolated session with one retry."""
    from app.services.workspace_sync import sync_all_for_tenant

    for attempt in range(2):
        try:
            async with tenant_scoped_session(tenant_id) as sync_db:
                await sync_all_for_tenant(sync_db, tenant_id)
            return
        except Exception as sync_err:
            if attempt == 0:
                logger.warning(f"Workspace sync failed for tenant {tenant_id}, retrying: {sync_err}")
                await asyncio.sleep(1)
            else:
                logger.warning(f"Workspace sync failed for tenant {tenant_id} after retry: {sync_err}")


async def _sync_one_agent(agent_id: uuid.UUID) -> None:
    """Re-render relationships.md for a single agent."""
    from app.services.workspace_sync import sync_agent_relationships

    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as sync_db:
            await sync_agent_relationships(sync_db, agent_id)
    except Exception as sync_err:
        logger.warning(f"Agent relationships sync failed for {agent_id}: {sync_err}")


async def _workspace_dirty_tick() -> None:
    """Drain dirty-flag set and re-sync only what changed. Cheap when nothing changed."""
    from app.services.workspace_sync_dirty import consume_dirty

    try:
        tenants, agents = await consume_dirty()
        if not tenants and not agents:
            return
        logger.info(f"[workspace-sync] dirty drain: tenants={len(tenants)}, agents={len(agents)}")
        for tenant_id in tenants:
            await _sync_one_tenant(tenant_id)
        for agent_id in agents:
            await _sync_one_agent(agent_id)
    except Exception as e:
        logger.error(f"Workspace dirty tick error: {e}", exc_info=True)


async def _workspace_full_sweep() -> None:
    """Safety net: sync every active tenant in case dirty events were lost."""
    from app.database import async_session
    from app.models.agent import Agent

    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="workspace full sweep — enumerate active tenants across all agents"),
        ):
            tenant_result = await db.execute(
                select(Agent.tenant_id)
                .where(
                    Agent.status.in_(["running", "idle"]),
                    Agent.tenant_id.is_not(None),
                )
                .distinct()
            )
            tenant_ids = {row[0] for row in tenant_result.all() if row[0]}

        logger.info(f"[workspace-sync] full sweep: {len(tenant_ids)} tenants")
        for tenant_id in tenant_ids:
            await _sync_one_tenant(tenant_id)
    except Exception as e:
        logger.error(f"Workspace full sweep error: {e}", exc_info=True)


async def _workspace_sync_loop():
    """Dirty-flag consumer: 60s tick, only syncs changed tenants/agents."""
    logger.info("📁 Workspace dirty-sync loop started (60s tick)")
    await asyncio.sleep(30)
    while True:
        await _workspace_dirty_tick()
        await asyncio.sleep(60)


async def _workspace_full_sweep_loop():
    """Safety net loop: full sync every 1h to recover from any lost dirty events."""
    logger.info("📁 Workspace full-sweep loop started (3600s interval)")
    await asyncio.sleep(120)
    while True:
        await _workspace_full_sweep()
        await asyncio.sleep(3600)


async def start_heartbeat():
    """Start background loops: heartbeat (60s) + workspace dirty-sync + full-sweep + dirty Redis listener."""
    from app.services.workspace_sync_dirty import start_redis_listener

    logger.info("💓 Agent heartbeat service started (60s tick)")
    await start_redis_listener()
    asyncio.create_task(_workspace_sync_loop())
    asyncio.create_task(_workspace_full_sweep_loop())
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)
