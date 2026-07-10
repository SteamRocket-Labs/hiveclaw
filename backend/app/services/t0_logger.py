"""Legacy T0 behavior/system log formatter.

Session T0 truth now lives in the append-only ledger under:

    memory/t0/sessions/{chat_session_id}/segments/{segment_id}/events.jsonl

with ``source.md`` as the deterministic Markdown/XML readable projection.

Runtime chat, trigger, delegation, heartbeat, and dream events must write that
ledger. This module remains only for legacy file imports, explicit manual
compatibility exports, and backfill adapters. It must not be used as a runtime
T0 writer.

Legacy layout:

    logs/YYYY-MM-DD/
        behavior/        ← legacy behavior logs / import compatibility
            chat-{HHmm}-{id}.md
            trigger-{HHmm}-{id}.md
            delegation-{HHmm}-{id}.md
        system/          ← legacy distiller self-trace (audit only, NOT consumed by T2)
            heartbeat-{HHmm}-{id}.md
            dream-{HHmm}-{id}.md

Chat backfill writes to the new session ledger. New runtime hooks must not
write ``logs/YYYY-MM-DD/**`` as primary T0 evidence.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, func, select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.memory.form_lint import lint_memory_form
from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events, seal_t0_session_segment
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.services.chat_transcript import CHAT_MESSAGE_ROLES
from app.services.privacy_layer import PrivacyLayer, PrivacyStore

logger = logging.getLogger(__name__)


_BEHAVIOR_TYPES: frozenset[str] = frozenset({"chat", "trigger", "delegation"})
_SYSTEM_TYPES: frozenset[str] = frozenset({"heartbeat", "dream"})

# PR-5: spill large tool_results into separate JSON files so a single
# behavior MD never balloons past a few KB. Threshold is per-message,
# measured against the rendered text content.
_ARTIFACT_THRESHOLD_CHARS = 8000
_ARTIFACT_PREVIEW_CHARS = 500
_ARTIFACT_REFERENCE_PREFIX = "[artifact: "

_T0_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_T0_LINT_SAMPLE_CHARS = 4000


def _apply_t0_privacy_gate(content: str) -> str:
    """Phase 10: mask credentials and annotate T0 frontmatter with sensitivity + form lint.

    T0 must not durably store PL4 credentials. PL2/PL3 content is preserved so
    the behavior log remains faithful for replay, but the frontmatter now
    carries `t0_sensitivity` so downstream extract/retriever can route by it,
    and `t0_form_warnings` flags pronoun/relative-time fragments worth
    rewriting before they reach T2.
    """
    if not content:
        return content
    layer = PrivacyLayer(PrivacyStore())
    decision = layer.classify_and_mask(content)
    text = decision.sanitized_text
    lint = lint_memory_form(text[:_T0_LINT_SAMPLE_CHARS])
    warnings = sorted({violation.code for violation in lint.violations if violation.code != "empty"})

    inject_lines = [f"t0_sensitivity: {decision.sensitivity.value}"]
    if warnings:
        inject_lines.append(f"t0_form_warnings: [{', '.join(warnings)}]")
    inject = "\n".join(inject_lines)

    match = _T0_FRONTMATTER_BLOCK_RE.match(text)
    if match:
        return f"---\n{match.group(1)}\n{inject}\n---\n{text[match.end() :]}"
    return f"---\n{inject}\n---\n\n{text}"


def _category_of(behavior_type: str) -> str:
    """Map a behavior_type to its storage subdirectory.

    This is the legacy logs layout. Portable T0 Memory evidence truth lives in
    memory/t0/sessions/**/events.jsonl. behavior/ holds legacy importable behavior
    snapshots; system/ holds legacy distiller traces. Unknown types fall back
    to system/ so they do not look like runtime session evidence.
    """
    if behavior_type in _BEHAVIOR_TYPES:
        return "behavior"
    return "system"


def _agent_logs_dir(agent_id: uuid.UUID) -> Path:
    """Return the logs/ directory for an agent."""
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "logs"


def _generate_filename(behavior_type: str, short_id: str = "") -> str:
    """Generate T0 filename: {type}-{HHmm}-{short_id}.md"""
    now = datetime.now(timezone.utc)
    hhmm = now.strftime("%H%M")
    if not short_id:
        short_id = uuid.uuid4().hex[:4]
    return f"{behavior_type}-{hhmm}-{short_id}.md"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            logger.debug("[T0] Unparseable timestamp %r: %s", value, exc)
            return None
    return None


def _date_dir(
    agent_id: uuid.UUID,
    when: datetime | None = None,
    *,
    behavior_type: str | None = None,
) -> Path:
    """Return a date directory under logs/, creating it if needed.

    If `behavior_type` is given the path includes a behavior/ or system/
    subdirectory so callers don't have to track the split themselves.
    Without it (legacy callers / cleanup) the bare date directory is returned.
    """
    target = when.astimezone(timezone.utc) if when else datetime.now(timezone.utc)
    date_label = target.strftime("%Y-%m-%d")
    d = _agent_logs_dir(agent_id) / date_label
    if behavior_type is not None:
        d = d / _category_of(behavior_type)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _yaml_frontmatter(fields: dict[str, Any]) -> str:
    """Format a dict as YAML frontmatter block."""
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(i) for i in v)}]")
        elif isinstance(v, datetime):
            lines.append(f"{k}: {v.isoformat()}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# ── Shared rendering helpers (used by behavior formatters) ──


def _extract_text_content(content: Any) -> str:
    """Normalize message content to a string.

    Handles plain strings, multi-part list[dict] (e.g. vision messages),
    and None. Non-text parts are skipped silently.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                parts.append(str(p["text"]))
            elif "content" in p and isinstance(p["content"], str):
                parts.append(p["content"])
        return " ".join(parts)
    return str(content)


def _index_tool_results(messages: list[dict]) -> dict[str, dict]:
    """Map tool_call_id → the corresponding `role=tool` message. Single pass."""
    index: dict[str, dict] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid:
            index[str(tcid)] = msg
    return index


def _render_tool_call_with_result(
    tc: dict,
    result_msg: dict | None,
    *,
    max_args: int,
    max_result: int,
) -> str:
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name = fn.get("name", "?") if isinstance(fn, dict) else "?"
    args = fn.get("arguments", "") if isinstance(fn, dict) else ""
    line = f"- `{name}({_truncate(str(args), max_args)})`"
    if result_msg is not None:
        result_text = _extract_text_content(result_msg.get("content"))
        if result_text:
            line += f"\n  → result: {_truncate(result_text, max_result)}"
    return line


def _render_messages_with_tools(
    messages: list[dict],
    *,
    max_text: int = 5000,
    max_args: int = 1500,
    max_result: int = 3000,
    start_with_user_turn: bool = True,
) -> str:
    """Render a thread including tool_calls AND their tool_results inline.

    Modern messages match by `tool_call_id`; legacy messages without that
    field fall back to positional pairing (next available tool result).
    """
    if not messages:
        return "(no messages)"

    result_index = _index_tool_results(messages)
    fallback_results = [m for m in messages if m.get("role") == "tool" and not m.get("tool_call_id")]
    fallback_idx = 0

    body_parts: list[str] = []
    turn_num = 0

    for msg in messages:
        role = msg.get("role", "")
        content_text = _extract_text_content(msg.get("content"))

        if role == "user":
            if not content_text:
                continue
            if start_with_user_turn:
                turn_num += 1
                body_parts.append(f"\n## Turn {turn_num}\n**User**: {_truncate(content_text, max_text)}")
            else:
                body_parts.append(f"**User**: {_truncate(content_text, max_text)}")
        elif role == "assistant":
            if content_text:
                body_parts.append(f"**Agent**: {_truncate(content_text, max_text)}")
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                tool_lines: list[str] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tcid = tc.get("id")
                    result = result_index.get(str(tcid)) if tcid else None
                    if result is None and fallback_idx < len(fallback_results):
                        result = fallback_results[fallback_idx]
                        fallback_idx += 1
                    tool_lines.append(
                        _render_tool_call_with_result(tc, result, max_args=max_args, max_result=max_result)
                    )
                if tool_lines:
                    body_parts.append("**Tools**:\n" + "\n".join(tool_lines))
        # tool messages are surfaced inline above; skip standalone rendering.

    return "\n".join(body_parts) if body_parts else "(no content)"


def _extract_errors_section(messages: list[dict]) -> str:
    """Surface error tool-results and assistant-side errors in one place.

    Returns an empty string when nothing matches so callers can simply
    append the result without checking.
    """
    errors: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            content = _extract_text_content(msg.get("content"))
            if not content:
                continue
            head = content[:60].lower()
            if content.startswith("[Error]") or "error" in head[:20]:
                tcid = msg.get("tool_call_id", "?")
                errors.append(f"- (call {tcid}) {_truncate(content, 500)}")
        elif role == "assistant":
            err = msg.get("error")
            if err:
                errors.append(f"- (assistant) {_truncate(str(err), 500)}")
    if not errors:
        return ""
    return "\n\n## Errors\n" + "\n".join(errors)


# ── Format functions for 5 behavior types ──


def _format_chat_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a chat session as T0 MD with full tool_call/tool_result chain."""
    started_at = _coerce_datetime(metadata.get("started_at")) or datetime.now(timezone.utc)
    source = metadata.get("source", "web")
    session_id = metadata.get("session_id", "unknown")
    user_name = metadata.get("user_name", "User")

    tools_used: list[str] = []
    turn_count = 0
    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            turn_count += 1
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {})
                name = fn.get("name", "") if isinstance(fn, dict) else ""
                if name and name not in tools_used:
                    tools_used.append(name)

    front = _yaml_frontmatter(
        {
            "type": "chat",
            "session_id": session_id,
            "source": source,
            "user": user_name,
            "started": started_at.isoformat(),
            "turns": turn_count,
            "tools": tools_used or [],
        }
    )

    body = _render_messages_with_tools(messages, start_with_user_turn=True)
    errors = _extract_errors_section(messages)

    return front + "\n" + body + errors + "\n"


def _format_trigger_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a trigger execution as T0 MD with full tool chain."""
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter(
        {
            "type": "trigger",
            "trigger_name": metadata.get("trigger_name", "unknown"),
            "trigger_type": metadata.get("trigger_type", "unknown"),
            "executed": now.isoformat(),
            "status": metadata.get("status", "unknown"),
            "duration_ms": metadata.get("duration_ms", 0),
        }
    )

    instruction = metadata.get("instruction", "")
    result = metadata.get("result", "")
    execution = _render_messages_with_tools(messages, start_with_user_turn=False)
    errors = _extract_errors_section(messages)

    body = f"""
## Instruction
{_truncate(instruction, 2000)}

## Execution
{execution}

## Result
{_truncate(result, 2000)}
{errors}
"""
    return front + body


def _format_delegation_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a delegation execution as T0 MD with full tool chain."""
    now = _coerce_datetime(metadata.get("delegated_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter(
        {
            "type": "delegation",
            "from": metadata.get("from_agent", "unknown"),
            "to": metadata.get("to_agent", "unknown"),
            "task": metadata.get("task", ""),
            "delegated": now.isoformat(),
            "status": metadata.get("status", "unknown"),
        }
    )

    task_text = metadata.get("task", "")
    result = metadata.get("result", "")
    execution = _render_messages_with_tools(messages, start_with_user_turn=False)
    errors = _extract_errors_section(messages)

    body = f"""
## Task
{_truncate(task_text, 2000)}

## Execution
{execution}

## Result
{_truncate(result, 2000)}
{errors}
"""
    return front + body


def _format_heartbeat_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a heartbeat tick as T0 MD with decision-reasoning audit trail.

    System-type log: NOT consumed by T2 extractor; written so operators can
    debug "why did this heartbeat choose noop / which T2 inputs did it
    consider".
    """
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter(
        {
            "type": "heartbeat",
            "tick": metadata.get("tick", 0),
            "session_started": metadata.get("session_started", now.isoformat()),
            "executed": now.isoformat(),
            "new_t2": metadata.get("new_t2", 0),
            "distilled": metadata.get("distilled", 0),
            "score": metadata.get("score", 0),
        }
    )

    t3_intake_candidates = metadata.get("t3_intake_candidates", metadata.get("new_t2_entries", []))
    distillation = metadata.get("distillation", [])
    action = metadata.get("action", "none")
    reasoning = (metadata.get("reasoning") or "").strip()
    t2_inputs = metadata.get("t2_inputs", []) or []
    t3_normalization = metadata.get("t3_normalization") or {}

    sections: list[str] = []
    if reasoning:
        sections.append(f"## Decision Reasoning\n{_truncate(reasoning, 2000)}")
    if t2_inputs:
        rendered_inputs = "\n".join(f"- {_truncate(str(item), 200)}" for item in t2_inputs)
        sections.append(f"## T3 Source Inputs Considered\n{rendered_inputs}")
    if messages:
        sections.append("## Tool Calls\n" + _render_messages_with_tools(messages, start_with_user_turn=False))

    t3_intake_section = "\n".join(f"- {e}" for e in t3_intake_candidates) if t3_intake_candidates else "(none)"
    distill_section = "\n".join(f"- {d}" for d in distillation) if distillation else "(none)"
    sections.append(f"## T3 Intake Candidates\n{t3_intake_section}")
    sections.append(f"## Distillation\n{distill_section}")
    sections.append(f"## Action\n{_truncate(str(action), 2000)}")

    # PR-9: heartbeat-run T3 format normalizer report.
    if isinstance(t3_normalization, dict) and (
        t3_normalization.get("fixed") or t3_normalization.get("warnings") or t3_normalization.get("files_touched")
    ):
        fixed = int(t3_normalization.get("fixed") or 0)
        warnings = t3_normalization.get("warnings") or []
        files = t3_normalization.get("files_touched") or []
        lines = [
            f"Fixed: {fixed}",
            f"Warnings: {len(warnings)}",
            f"Files touched: {', '.join(files) if files else '(none)'}",
        ]
        if warnings:
            lines.append("")
            lines.append("### Warnings")
            for w in warnings[:20]:
                lines.append(f"- {_truncate(str(w), 200)}")
        sections.append("## T3 Normalization\n" + "\n".join(lines))

    return front + "\n\n" + "\n\n".join(sections) + "\n"


def _format_dream_log(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Format a dream execution as T0 MD with structured decision audit.

    System-type log: NOT consumed by T2. Records WHY the dream consolidated
    or promoted what it did, so soul-evolution is traceable later.
    """
    now = _coerce_datetime(metadata.get("executed_at")) or datetime.now(timezone.utc)
    front = _yaml_frontmatter(
        {
            "type": "dream",
            "executed": now.isoformat(),
            "t3_processed": metadata.get("t3_processed", 0),
            "deduped": metadata.get("deduped", 0),
            "promoted_to_soul": metadata.get("promoted_to_soul", 0),
        }
    )

    dream_reasoning = (metadata.get("dream_reasoning") or "").strip()
    dedup_decisions = metadata.get("dedup_decisions") or []
    promotion_decisions = metadata.get("promotion_decisions") or []
    legacy_dedup_summary = metadata.get("dedup_summary", "")
    soul_candidate = metadata.get("soul_candidate")
    legacy_promotions = metadata.get("soul_promotions", []) or []
    cleanup = metadata.get("cleanup_summary", "")

    sections: list[str] = []
    if dream_reasoning:
        sections.append(f"## Reasoning\n{_truncate(dream_reasoning, 2000)}")

    # Dedup section: prefer structured decisions, fall back to legacy summary.
    if dedup_decisions:
        lines = []
        for d in dedup_decisions:
            if not isinstance(d, dict):
                continue
            file_name = str(d.get("file", "?"))
            kept = _truncate(str(d.get("kept", "")), 80)
            dropped_count = d.get("dropped_count", 0)
            reason = str(d.get("reason", ""))
            lines.append(f'- {file_name}: kept "{kept}" (dropped {dropped_count}: {reason})')
        sections.append("## Dedup Decisions\n" + ("\n".join(lines) if lines else "(none)"))
    else:
        sections.append(f"## Dedup\n{_truncate(legacy_dedup_summary, 2000) if legacy_dedup_summary else '(none)'}")

    # Soul candidate section: prefer the reviewed candidate package, fall back to legacy audit list.
    if isinstance(soul_candidate, dict):
        target = str(soul_candidate.get("target") or "soul.md")
        refs = ", ".join(str(ref) for ref in (soul_candidate.get("source_refs") or [])[:5])
        review = (
            soul_candidate.get("memory_gate_review")
            if isinstance(soul_candidate.get("memory_gate_review"), dict)
            else {}
        )
        recommendation = str(review.get("recommendation") or "unknown")
        sections.append(
            "## Soul Candidate\n"
            f"- target: {target}\n"
            f"- recommendation: {recommendation}\n"
            f"- source_refs: {refs or '(none)'}"
        )
    elif promotion_decisions:
        lines = []
        for p in promotion_decisions:
            if not isinstance(p, dict):
                continue
            excerpt = _truncate(str(p.get("soul_excerpt", "")), 120)
            source = str(p.get("source_t3_file", "?"))
            repetition = p.get("repetition_count", 0)
            reason = str(p.get("reason", ""))
            lines.append(f'- "{excerpt}" ← {source} (repeated {repetition}x: {reason})')
        sections.append("## Legacy Soul Promotion Audit\n" + ("\n".join(lines) if lines else "(none)"))
    else:
        promo_section = "\n".join(f"- {p}" for p in legacy_promotions) if legacy_promotions else "(none)"
        sections.append(f"## Legacy Soul Promotion Audit\n{promo_section}")

    sections.append(f"## Cleanup\n{_truncate(cleanup, 2000) if cleanup else '(none)'}")

    if messages:
        sections.append("## Tool Calls\n" + _render_messages_with_tools(messages, start_with_user_turn=False))

    return front + "\n\n" + "\n\n".join(sections) + "\n"


# ── Helpers ──


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


# ── Public API ──

_FORMATTERS: dict[str, Any] = {
    "chat": _format_chat_log,
    "trigger": _format_trigger_log,
    "delegation": _format_delegation_log,
    "heartbeat": _format_heartbeat_log,
    "dream": _format_dream_log,
}


def write_t0_log(
    agent_id: uuid.UUID,
    *,
    behavior_type: str,
    messages: list[dict] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    """Write a legacy raw log file. Returns the file path, or None on failure.

    This is retained for legacy import/manual compatibility only. Runtime T0
    events must use app.memory.t0.ledger.
    """
    formatter = _FORMATTERS.get(behavior_type)
    if not formatter:
        logger.warning("[T0] Unknown behavior type: %s", behavior_type)
        return None

    messages = messages or []
    metadata = metadata or {}

    try:
        short_id = str(metadata.get("session_id", uuid.uuid4().hex))[:4]
        filename = _generate_filename(behavior_type, short_id)
        occurred_at = _coerce_datetime(metadata.get("occurred_at") or metadata.get("started_at"))
        filepath = _date_dir(agent_id, occurred_at, behavior_type=behavior_type) / filename

        # PR-5: spill large tool_results into artifacts/ before formatter runs.
        artifact_dir = filepath.parent.parent / "artifacts"
        spilled_messages = _spillover_large_tool_results(
            messages,
            artifact_dir=artifact_dir,
            session_id=str(metadata.get("session_id", "")),
        )

        content = formatter(spilled_messages, metadata)
        content = _apply_t0_privacy_gate(content)
        filepath.write_text(content, encoding="utf-8")
        logger.info("[T0] Wrote %s for agent %s (%d bytes)", filepath.name, agent_id, len(content))
        return filepath
    except Exception as exc:
        logger.error("[T0] Failed to write %s log for agent %s: %s", behavior_type, agent_id, exc)
        return None


def _spillover_large_tool_results(
    messages: list[dict],
    *,
    artifact_dir: Path,
    session_id: str = "",
    threshold: int = _ARTIFACT_THRESHOLD_CHARS,
) -> list[dict]:
    """Replace oversized tool_result content with an artifact reference.

    Walks `messages`, identifies role=="tool" entries whose text content
    exceeds `threshold`, dumps the original content + matching tool_call
    metadata to `artifact_dir/{tool_call_id}-{tool_name}.json`, and
    returns a copy of the message list where those tool messages now
    carry the reference + a preview prefix.

    Reference format (matched by replay_messages_from_t0):
        [artifact: artifacts/{file}.json] preview: <first N chars>
    """
    if not messages:
        return messages

    # Build a tool_call_id → tool_name lookup so artifacts can be named.
    name_lookup: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            tcid = tc.get("id")
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if tcid and isinstance(fn, dict):
                name_lookup[str(tcid)] = str(fn.get("name", "tool"))

    output: list[dict] = []
    artifact_dir_created = False

    for msg in messages:
        if msg.get("role") != "tool":
            output.append(msg)
            continue
        text = _extract_text_content(msg.get("content"))
        if len(text) <= threshold:
            output.append(msg)
            continue

        tcid = str(msg.get("tool_call_id") or uuid.uuid4().hex[:8])
        tool_name = name_lookup.get(tcid, "tool")
        safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name)[:48] or "tool"
        safe_tcid = re.sub(r"[^A-Za-z0-9_.-]", "_", tcid)[:48]
        artifact_filename = f"{safe_tcid}-{safe_tool}.json"

        if not artifact_dir_created:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact_dir_created = True

        artifact_path = artifact_dir / artifact_filename
        try:
            artifact_path.write_text(
                json.dumps(
                    {
                        "tool_call_id": tcid,
                        "tool_name": tool_name,
                        "session_id": session_id,
                        "result": text,
                        "size_chars": len(text),
                        "spilled_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("[T0] Failed to write artifact %s: %s", artifact_path, exc)
            output.append(msg)
            continue

        relative_ref = f"artifacts/{artifact_filename}"
        preview = text[:_ARTIFACT_PREVIEW_CHARS]
        new_content = f"{_ARTIFACT_REFERENCE_PREFIX}{relative_ref}] preview: {preview}"
        replaced = dict(msg)
        replaced["content"] = new_content
        replaced["_artifact_ref"] = relative_ref
        output.append(replaced)

    return output


def cleanup_old_logs(agent_id: uuid.UUID, retention_days: int = 30) -> int:
    """Remove T0 date directories older than retention_days. Returns count removed."""
    logs_dir = _agent_logs_dir(agent_id)
    if not logs_dir.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0

    for entry in logs_dir.iterdir():
        if not entry.is_dir():
            continue
        # Directory name should be YYYY-MM-DD
        if entry.name < cutoff_str:
            try:
                shutil.rmtree(entry)
                removed += 1
                logger.info("[T0] Cleaned up old log directory: %s", entry.name)
            except Exception as exc:
                logger.warning("[T0] Failed to remove %s: %s", entry.name, exc)

    if removed:
        logger.info("[T0] Cleaned %d old log directories for agent %s", removed, agent_id)
    return removed


def audit_t0_logs(agent_id: uuid.UUID, recent_days: int = 30) -> dict[str, Any]:
    """Inspect legacy log health for an agent.

    Counts behavior/* and system/* separately under the legacy logs layout while still
    counting any leftover legacy flat files (top-level under YYYY-MM-DD/) so
    audits before the migration runs are not under-reported.
    """
    logs_dir = _agent_logs_dir(agent_id)
    if not logs_dir.exists():
        return {
            "logs_dir_exists": False,
            "date_dirs": 0,
            "empty_date_dirs": 0,
            "total_files": 0,
            "recent_files": 0,
            "behavior_files": 0,
            "system_files": 0,
            "legacy_flat_files": 0,
            "healthy": False,
        }

    cutoff_label = (datetime.now(timezone.utc) - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    date_dirs = 0
    empty_date_dirs = 0
    total_files = 0
    recent_files = 0
    behavior_files = 0
    system_files = 0
    legacy_flat_files = 0

    for day_dir in logs_dir.iterdir():
        if not day_dir.is_dir():
            continue
        date_dirs += 1

        day_count = 0
        for child in day_dir.iterdir():
            if child.is_file() and child.suffix == ".md":
                # Pre-migration flat layout — counted but flagged.
                legacy_flat_files += 1
                day_count += 1
            elif child.is_dir() and child.name in ("behavior", "system"):
                bucket_files = [p for p in child.iterdir() if p.is_file() and p.suffix == ".md"]
                if child.name == "behavior":
                    behavior_files += len(bucket_files)
                else:
                    system_files += len(bucket_files)
                day_count += len(bucket_files)

        if day_count == 0:
            empty_date_dirs += 1
            continue
        total_files += day_count
        if day_dir.name >= cutoff_label:
            recent_files += day_count

    return {
        "logs_dir_exists": True,
        "date_dirs": date_dirs,
        "empty_date_dirs": empty_date_dirs,
        "total_files": total_files,
        "recent_files": recent_files,
        "behavior_files": behavior_files,
        "system_files": system_files,
        "legacy_flat_files": legacy_flat_files,
        "healthy": total_files > 0,
    }


def _extract_existing_session_ids(files: Iterable[Path]) -> set[str]:
    session_ids: set[str] = set()
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("session_id:"):
                    session_ids.add(line.split(":", 1)[1].strip())
                    break
        except Exception as exc:
            logger.debug("[T0] Failed to inspect %s: %s", path, exc)
    return session_ids


async def backfill_recent_chat_logs(
    agent_id: uuid.UUID,
    recent_days: int = 30,
    limit_sessions: int = 20,
    *,
    tenant_id: uuid.UUID | None = None,
    session_ids: Iterable[uuid.UUID] | None = None,
) -> dict[str, int]:
    """Backfill recent chat sessions into the append-only T0 session ledger.

    This function intentionally no longer writes ``logs/YYYY-MM-DD/**/chat-*.md``.
    The legacy per-file logger remains available for import/compatibility, but
    session T0 portable Memory evidence truth is
    ``memory/t0/sessions/<session>/segments/*/events.jsonl``; ``source.md`` is
    the deterministic readable projection.
    """
    data_root = Path(get_settings().AGENT_DATA_DIR)

    explicit_session_ids = [uuid.UUID(str(value)) for value in (session_ids or [])]
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    if explicit_session_ids:
        session_stmt = (
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.id.in_(explicit_session_ids),
            )
            .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
        )
    else:
        session_stmt = (
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.created_at >= cutoff,
            )
            .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
            .limit(limit_sessions)
        )

    try:
        async with tenant_scoped_session(tenant_id) as db:
            sessions = (await db.execute(session_stmt)).scalars().all()
            written = 0
            skipped_existing = 0
            skipped_empty = 0
            transcript_events_written = 0
            t0_events_written = 0

            for session in sessions:
                session_key = str(session.id)
                existing_t0 = bool(
                    replay_t0_session_events(agent_id=agent_id, session_id=session_key, data_root=data_root)
                )
                transcript_stmt = (
                    select(ChatTranscriptEvent.id)
                    .where(
                        ChatTranscriptEvent.agent_id == agent_id,
                        ChatTranscriptEvent.session_id == session.id,
                    )
                    .limit(1)
                )
                existing_transcript = bool((await db.execute(transcript_stmt)).scalars().all())
                if existing_t0 and existing_transcript:
                    skipped_existing += 1
                    continue

                message_stmt = (
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == session_key,
                        ChatMessage.role.in_(tuple(sorted(CHAT_MESSAGE_ROLES))),
                    )
                    .order_by(ChatMessage.created_at.asc())
                )
                messages = (await db.execute(message_stmt)).scalars().all()
                appendable_messages = [message for message in messages if (message.content or "").strip()]
                if not appendable_messages:
                    skipped_empty += 1
                    continue

                for offset, message in enumerate(appendable_messages):
                    role = str(message.role)
                    event_type = _backfill_event_type_for_role(role)
                    actor_type = _backfill_actor_type_for_role(role)
                    if not existing_t0:
                        append_t0_session_event(
                            agent_id=agent_id,
                            session_id=session_key,
                            event_type=event_type,
                            role=role,
                            content=message.content,
                            message_id=getattr(message, "id", None),
                            actor_id=getattr(session, "user_id", None) if role == "user" else agent_id,
                            source=str(session.source_channel or "backfill"),
                            data_root=data_root,
                            created_at=getattr(message, "created_at", None),
                            metadata={
                                "source": "backfill_recent_chat_logs",
                                "session_source": str(session.source_channel or ""),
                            },
                        )
                        t0_events_written += 1
                    if not existing_transcript:
                        _add_transcript_backfill_event(
                            db=db,
                            agent_id=agent_id,
                            tenant_id=tenant_id or getattr(session, "tenant_id", None),
                            session=session,
                            message=message,
                            event_type=event_type,
                            actor_type=actor_type,
                            offset=offset,
                        )
                        transcript_events_written += 1
                if not existing_t0:
                    seal_t0_session_segment(
                        agent_id=agent_id,
                        session_id=session_key,
                        reason="backfill_recent_chat_logs",
                        data_root=data_root,
                        created_at=getattr(session, "last_message_at", None),
                        metadata={"source": "backfill_recent_chat_logs"},
                    )
                written += 1
    except Exception as exc:
        logger.debug("[T0] Backfill skipped for %s: %s", agent_id, exc)
        return {
            "sessions_scanned": 0,
            "written": 0,
            "skipped_existing": 0,
            "skipped_empty": 0,
            "transcript_events_written": 0,
            "t0_events_written": 0,
        }

    return {
        "sessions_scanned": len(sessions),
        "written": written,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
        "transcript_events_written": transcript_events_written,
        "t0_events_written": t0_events_written,
    }


def _backfill_event_type_for_role(role: str) -> str:
    return {
        "user": "user_message",
        "assistant": "assistant_message",
        "system": "system_message",
        "tool_call": "tool_result",
    }.get(role, "message")


def _backfill_actor_type_for_role(role: str) -> str:
    return {
        "user": "user",
        "assistant": "assistant",
        "system": "system",
        "tool_call": "tool",
    }.get(role, "system")


def _backfill_sequence_for_message(message: Any, offset: int) -> int:
    created_at = _coerce_datetime(getattr(message, "created_at", None))
    if created_at is not None:
        return int(created_at.timestamp() * 1_000_000_000) + offset
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000) + offset


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _add_transcript_backfill_event(
    *,
    db: Any,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    session: Any,
    message: Any,
    event_type: str,
    actor_type: str,
    offset: int,
) -> None:
    event_id = uuid.uuid4()
    sequence = _backfill_sequence_for_message(message, offset)
    role = str(getattr(message, "role", "") or "")
    metadata = {
        "source": "backfill_recent_chat_logs",
        "session_source": str(getattr(session, "source_channel", "") or ""),
        "legacy_message_id": str(getattr(message, "id", "") or ""),
        "transcript_event_id": str(event_id),
        "transcript_sequence": sequence,
        "actor_type": actor_type,
        "event_type": event_type,
        "role": role,
        "t0_role": role,
        "visibility_scope": str(getattr(session, "visibility_scope", "") or "direct_user"),
        "listed_surface": str(getattr(session, "listed_surface", "") or "chat"),
    }
    transcript_event = ChatTranscriptEvent(
        id=event_id,
        sequence=sequence,
        tenant_id=_uuid_or_none(tenant_id),
        agent_id=agent_id,
        session_id=_uuid_or_none(getattr(session, "id", None)),
        run_id=_uuid_or_none(getattr(session, "runtime_task_id", None)),
        root_session_id=_uuid_or_none(getattr(session, "root_session_id", None)),
        parent_session_id=_uuid_or_none(getattr(session, "parent_session_id", None)),
        message_id=_uuid_or_none(getattr(message, "id", None)),
        actor_type=actor_type,
        event_type=event_type,
        visibility_scope=metadata["visibility_scope"],
        listed_surface=metadata["listed_surface"],
        content=str(getattr(message, "content", "") or ""),
        metadata_json=metadata,
        schema_version=1,
        item_type="user_message" if role == "user" else "agent_message" if role == "assistant" else "event",
        item_status="succeeded",
        projection_status="projected",
        projection_attempts=1,
        projected_at=_coerce_datetime(getattr(message, "created_at", None)) or datetime.now(timezone.utc),
    )
    created_at = _coerce_datetime(getattr(message, "created_at", None))
    if created_at is not None:
        transcript_event.created_at = created_at
    db.add(transcript_event)


def _chunked_session_ids(session_ids: list[uuid.UUID], batch_size: int) -> Iterable[list[uuid.UUID]]:
    effective_batch_size = max(1, batch_size)
    for index in range(0, len(session_ids), effective_batch_size):
        yield session_ids[index : index + effective_batch_size]


async def backfill_missing_chat_transcript_t0(
    *,
    recent_days: int = 3650,
    max_sessions: int = 10000,
    batch_size: int = 100,
) -> dict[str, int | str]:
    """Repair legacy chat sessions into both DB transcript events and canonical T0 ledger.

    Enumeration uses audited RLS BYPASS because it has to find cross-tenant
    legacy gaps at startup. Actual writes are delegated back into
    ``backfill_recent_chat_logs`` with the original tenant id so DB writes and
    workspace paths stay on the normal governed backfill path.
    """
    data_root = Path(get_settings().AGENT_DATA_DIR)
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    role_values = tuple(sorted(CHAT_MESSAGE_ROLES))
    message_exists = (
        select(ChatMessage.id)
        .where(
            ChatMessage.agent_id == ChatSession.agent_id,
            ChatMessage.conversation_id == cast(ChatSession.id, String),
            ChatMessage.role.in_(role_values),
        )
        .limit(1)
        .exists()
    )
    session_stmt = (
        select(ChatSession.id, ChatSession.agent_id, ChatSession.tenant_id)
        .where(
            ChatSession.created_at >= cutoff,
            ChatSession.tenant_id.is_not(None),
            message_exists,
        )
        .order_by(ChatSession.last_message_at.desc().nullslast(), ChatSession.created_at.desc())
        .limit(max(1, max_sessions))
    )

    grouped_targets: dict[tuple[uuid.UUID, uuid.UUID], list[uuid.UUID]] = {}
    skipped_existing = 0
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="startup legacy chat transcript/T0 backfill enumerate") as bypass_db:
            rows = (await bypass_db.execute(session_stmt)).all()
            for row in rows:
                session_id = uuid.UUID(str(row[0]))
                agent_id = uuid.UUID(str(row[1]))
                tenant_id = _uuid_or_none(row[2])
                if tenant_id is None:
                    continue
                existing_t0 = bool(
                    replay_t0_session_events(agent_id=agent_id, session_id=str(session_id), data_root=data_root)
                )
                transcript_count = int(
                    (
                        await bypass_db.execute(
                            select(func.count(ChatTranscriptEvent.id)).where(
                                ChatTranscriptEvent.agent_id == agent_id,
                                ChatTranscriptEvent.session_id == session_id,
                            )
                        )
                    ).scalar()
                    or 0
                )
                if existing_t0 and transcript_count > 0:
                    skipped_existing += 1
                    continue
                grouped_targets.setdefault((tenant_id, agent_id), []).append(session_id)

    target_session_count = sum(len(values) for values in grouped_targets.values())
    logger.info(
        "[T0] Startup chat transcript/T0 backfill enumeration complete: scanned=%d needing=%d groups=%d skipped_existing=%d",
        len(rows),
        target_session_count,
        len(grouped_targets),
        skipped_existing,
    )

    groups_processed = 0
    batches_processed = 0
    written = 0
    skipped_empty = 0
    transcript_events_written = 0
    t0_events_written = 0
    for (tenant_id, agent_id), session_ids in grouped_targets.items():
        groups_processed += 1
        group_batch_total = (len(session_ids) + max(1, batch_size) - 1) // max(1, batch_size)
        group_batch_index = 0
        for batch in _chunked_session_ids(session_ids, batch_size):
            group_batch_index += 1
            batches_processed += 1
            report = await backfill_recent_chat_logs(
                agent_id,
                recent_days=recent_days,
                limit_sessions=len(batch),
                tenant_id=tenant_id,
                session_ids=batch,
            )
            written += int(report.get("written", 0))
            skipped_empty += int(report.get("skipped_empty", 0))
            skipped_existing += int(report.get("skipped_existing", 0))
            transcript_events_written += int(report.get("transcript_events_written", 0))
            t0_events_written += int(report.get("t0_events_written", 0))
            logger.info(
                "[T0] Startup chat transcript/T0 backfill batch complete: group=%d/%d tenant=%s agent=%s batch=%d/%d sessions=%d written=%s transcript_events=%s t0_events=%s skipped_existing=%s skipped_empty=%s",
                groups_processed,
                len(grouped_targets),
                tenant_id,
                agent_id,
                group_batch_index,
                group_batch_total,
                len(batch),
                report.get("written", 0),
                report.get("transcript_events_written", 0),
                report.get("t0_events_written", 0),
                report.get("skipped_existing", 0),
                report.get("skipped_empty", 0),
            )

    return {
        "schema": "chat_transcript_t0_backfill.v1",
        "candidate_sessions_scanned": len(rows),
        "sessions_needing_backfill": target_session_count,
        "groups_processed": groups_processed,
        "batches_processed": batches_processed,
        "written": written,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
        "transcript_events_written": transcript_events_written,
        "t0_events_written": t0_events_written,
    }


async def run_startup_chat_transcript_t0_backfill() -> dict[str, int | str | bool]:
    """Run the bounded startup repair using configured production-safe limits."""
    settings = get_settings()
    if not settings.T0_STARTUP_BACKFILL_ENABLED:
        logger.info("[T0] Startup chat transcript/T0 backfill disabled")
        return {"schema": "chat_transcript_t0_backfill.v1", "enabled": False}

    report = await backfill_missing_chat_transcript_t0(
        recent_days=settings.T0_STARTUP_BACKFILL_RECENT_DAYS,
        max_sessions=settings.T0_STARTUP_BACKFILL_MAX_SESSIONS,
        batch_size=settings.T0_STARTUP_BACKFILL_BATCH_SIZE,
    )
    logger.info(
        "[T0] Startup chat transcript/T0 backfill complete: scanned=%s needing=%s written=%s transcript_events=%s t0_events=%s",
        report.get("candidate_sessions_scanned", 0),
        report.get("sessions_needing_backfill", 0),
        report.get("written", 0),
        report.get("transcript_events_written", 0),
        report.get("t0_events_written", 0),
    )
    return {"enabled": True, **report}


_LAYOUT_MARKER_FILENAME = ".t0_layout_v2"


def migrate_t0_layout(agent_id: uuid.UUID) -> dict[str, int]:
    """Move pre-PR-1 flat T0 files into behavior/ or system/ subdirectories.

    Layout before:  logs/YYYY-MM-DD/{type}-{HHmm}-{id}.md
    Layout after:   logs/YYYY-MM-DD/{behavior|system}/{type}-{HHmm}-{id}.md

    Idempotent via `.t0_layout_v2` marker at the agent root. Files with
    unrecognized name prefixes are left in place and counted under `skipped`.
    """
    agent_root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    marker = agent_root / _LAYOUT_MARKER_FILENAME
    if marker.exists():
        return {"moved": 0, "skipped": 0, "marker_existed": 1}

    logs_dir = _agent_logs_dir(agent_id)
    moved = 0
    skipped = 0

    if logs_dir.exists():
        for day_dir in logs_dir.iterdir():
            if not day_dir.is_dir():
                continue
            for entry in list(day_dir.iterdir()):
                if not entry.is_file() or entry.suffix != ".md":
                    continue
                prefix = entry.name.split("-", 1)[0]
                if prefix not in _BEHAVIOR_TYPES and prefix not in _SYSTEM_TYPES:
                    skipped += 1
                    continue
                target_dir = day_dir / _category_of(prefix)
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / entry.name
                if target_path.exists():
                    # Conflict: keep the in-place copy and remove the source so
                    # we don't churn it again on every restart.
                    entry.unlink()
                    skipped += 1
                    continue
                try:
                    shutil.move(str(entry), str(target_path))
                    moved += 1
                except OSError as exc:
                    logger.warning("[T0] Failed to move %s → %s: %s", entry, target_path, exc)
                    skipped += 1

    agent_root.mkdir(parents=True, exist_ok=True)
    try:
        with open(marker, "x", encoding="utf-8") as f:
            f.write(f"migrated_at: {datetime.now(timezone.utc).isoformat()}\nmoved: {moved}\nskipped: {skipped}\n")
    except FileExistsError:
        logger.debug("[T0] Layout marker already written for agent %s (concurrent migration)", agent_id)

    if moved or skipped:
        logger.info("[T0] Layout migration for %s: moved=%d skipped=%d", agent_id, moved, skipped)
    return {"moved": moved, "skipped": skipped, "marker_existed": 0}
