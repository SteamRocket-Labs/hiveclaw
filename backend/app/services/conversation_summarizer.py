"""Conversation summarization — compress old messages to save tokens."""

import logging
import re

from app.services.llm_error_policy import is_llm_error_message

logger = logging.getLogger(__name__)

# Default fallback when ProviderSpec is unavailable
CHARS_PER_TOKEN = 3.5


def _get_chars_per_token(provider: str) -> float:
    """Read chars_per_token from ProviderSpec (PROVIDER_REGISTRY).

    Falls back to CHARS_PER_TOKEN constant for unknown providers.
    No hardcoded provider names — all values live in the registry.
    """
    if not provider:
        return CHARS_PER_TOKEN
    try:
        from app.services.llm_client import get_provider_spec

        spec = get_provider_spec(provider)
        if spec is not None:
            return spec.chars_per_token
    except Exception:
        pass
    return CHARS_PER_TOKEN


def estimate_tokens(messages: list[dict], *, provider: str = "") -> int:
    """Estimate total tokens across all messages.

    Uses ProviderSpec.chars_per_token for better accuracy per provider.
    """
    cpt = _get_chars_per_token(provider)
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Vision format: array of parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    # Image tokens vary by detail level: ~85 low, ~765 high.
                    detail = "auto"
                    img_data = part.get("image_url", {})
                    if isinstance(img_data, dict):
                        detail = img_data.get("detail", "auto")
                    tokens_for_image = 85 if detail == "low" else 765 if detail == "high" else 300
                    total_chars += int(tokens_for_image * cpt)
        # Tool calls: estimate actual JSON arg size instead of flat 200
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                total_chars += len(fn.get("name", "")) + len(fn.get("arguments", "")) + 50
    return int(total_chars / cpt)


def _extract_tool_summary(messages: list[dict]) -> str:
    """Extract a compact summary of tool calls from messages."""
    tool_entries: list[str] = []
    for msg in messages:
        # Assistant messages with tool_calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                tool_entries.append(f"called {name}")
        # Tool result messages
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                preview = content[:200].replace("\n", " ")
                tool_entries.append(f"  → {preview}")
    if not tool_entries:
        return ""
    # Keep last 15 tool interactions to stay compact
    return "\n".join(tool_entries[-15:])


def _extract_artifacts(messages: list[dict]) -> list[str]:
    patterns = (
        r"(\/[A-Za-z0-9_\-./]+)",
        r"(https?:\/\/[^\s)]+)",
        r"\b([A-Za-z0-9_-]{6,})\b",
    )
    artifacts: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        for pattern in patterns:
            for match in re.findall(pattern, content):
                if not isinstance(match, str):
                    continue
                normalized = match.strip()
                if len(normalized) < 6 or normalized in seen:
                    continue
                if normalized.startswith("http") or normalized.startswith("/") or "_" in normalized:
                    artifacts.append(normalized)
                    seen.add(normalized)
    return artifacts[:8]


def _extract_preferences(messages: list[dict]) -> list[str]:
    preferences: list[str] = []
    seen: set[str] = set()
    hints = ("prefer", "记住", "更喜欢", "不要", "务必", "请用", "以后", "always", "never")
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        lowered = content.lower()
        if any(hint in lowered or hint in content for hint in hints):
            pref = content.strip()[:200]
            if pref not in seen:
                preferences.append(pref)
                seen.add(pref)
    return preferences[:5]


def _extract_pending(messages: list[dict]) -> list[str]:
    pending: list[str] = []
    seen: set[str] = set()
    hints = ("next", "pending", "todo", "need to", "下一步", "还需要", "待", "继续")
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        lowered = content.lower()
        if any(hint in lowered or hint in content for hint in hints):
            item = content.strip()[:200]
            if item not in seen:
                pending.append(item)
                seen.add(item)
        if len(pending) >= 4:
            break
    pending.reverse()
    return pending


def _extract_decisions(messages: list[dict]) -> list[str]:
    """Extract key reasoning and decisions from assistant messages."""
    decisions: list[str] = []
    seen: set[str] = set()
    hints = (
        "决定",
        "方案",
        "因为",
        "所以",
        "选择",
        "采用",
        "原因是",
        "I decided",
        "because",
        "approach",
        "solution",
        "chose",
        "the reason",
        "结论",
        "总结",
        "建议",
        "recommend",
    )
    for msg in messages:
        if msg.get("role") != "assistant" or "tool_calls" in msg:
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        lowered = content.lower()
        if any(hint in lowered or hint in content for hint in hints):
            # Extract the sentence containing the decision hint
            for line in content.split("\n"):
                line = line.strip()
                if len(line) > 20 and any(h in line.lower() or h in line for h in hints):
                    snippet = line[:250]
                    key = snippet[:60].lower()
                    if key not in seen:
                        decisions.append(snippet)
                        seen.add(key)
                    break
        if len(decisions) >= 5:
            break
    return decisions


def _extract_summary(messages: list[dict]) -> str:
    """Extract a state-first snapshot without an LLM."""
    user_asks: list[str] = []
    assistant_answers: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            user_asks.append(content[:200])
        elif role == "assistant" and "tool_calls" not in msg and not is_llm_error_message(content):
            assistant_answers.append(content[:300])

    if not user_asks and not assistant_answers:
        return "\n".join(
            [
                "**Task Ledger:** (unknown)",
                "**Pending Ledger:** (none captured)",
                "**Primary Request and Intent:** (unknown)",
                "**Current Work:** (none captured)",
            ]
        )

    task = user_asks[-1] if user_asks else "(unknown)"
    decision_candidates = user_asks[-4:-1]
    if assistant_answers:
        decision_candidates.append(assistant_answers[-1][:120])
    decision_text = "; ".join(item[:120] for item in decision_candidates if item) or "(none captured)"
    tool_summary = _extract_tool_summary(messages) or "(no tool activity captured)"
    artifacts = _extract_artifacts(messages)
    artifact_text = "\n".join(f"- {item}" for item in artifacts) if artifacts else "- (none captured)"
    preferences = _extract_preferences(messages)
    preference_text = "\n".join(f"- {item}" for item in preferences) if preferences else "- (none captured)"
    pending = _extract_pending(messages)
    pending_text = "\n".join(f"- {item}" for item in pending) if pending else "- (none captured)"

    # Extract agent reasoning and decisions
    decisions = _extract_decisions(messages)
    reasoning_text = "\n".join(f"- {d}" for d in decisions) if decisions else "- (none captured)"

    # Problem Solving: extract attempted approaches from assistant messages
    _problem_hints = ("tried", "attempted", "failed", "succeeded", "worked", "didn't work", "error", "fix")
    problem_items: list[str] = []
    for ans in assistant_answers[-10:]:
        for hint in _problem_hints:
            if hint in ans.lower():
                problem_items.append(ans[:200])
                break
    problem_text = "\n".join(f"- {p}" for p in problem_items) if problem_items else "- (none captured)"

    # Current Work: last assistant message
    current_work = assistant_answers[-1][:300] if assistant_answers else "(none captured)"

    return "\n".join(
        [
            f"**Task Ledger:** {task}",
            f"**Decision Ledger:** {decision_text}",
            f"**Artifact Ledger:**\n{artifact_text}",
            f"**Tool Ledger:**\n{tool_summary}",
            f"**Preference Ledger:**\n{preference_text}",
            f"**Pending Ledger:**\n{pending_text}",
            f"**Primary Request and Intent:** {task}",
            f"**Key Technical Decisions:** {decision_text}",
            f"**Files and Code Sections:**\n{artifact_text}",
            f"**Problem Solving:**\n{problem_text}",
            f"**Errors and Fixes:**\n{reasoning_text}",
            "**All User Messages:** " + "; ".join(u[:100] for u in user_asks[-10:])
            if user_asks
            else "**All User Messages:** (none)",
            f"**User Preferences:**\n{preference_text}",
            f"**Tool Outcomes:**\n{tool_summary}",
            f"**Pending Tasks:**\n{pending_text}",
            f"**Current Work:** {current_work}",
        ]
    )


def _extract_summary_from_response(content: str) -> str | None:
    """Extract <summary> content, stripping <analysis> scratchpad.

    The LLM is instructed to use <analysis> as a reasoning scratchpad and
    <summary> for the final output.  Only the <summary> block is persisted
    into context — the analysis is discarded to save tokens.
    """
    if not content:
        return None
    summary_match = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
    if summary_match:
        return summary_match.group(1).strip()
    # Fallback: strip <analysis> block if model didn't wrap in <summary>
    stripped = re.sub(r"<analysis>.*?</analysis>", "", content, flags=re.DOTALL).strip()
    return stripped if stripped else content.strip()


# Summarization system prompt — uses <analysis>/<summary> scratchpad pattern.
# The <analysis> block is stripped by _extract_summary_from_response() before
# the summary reaches context, letting the model reason without wasting tokens.
_SUMMARIZE_SYSTEM_PROMPT = """\
<role>
You are compressing a long conversation into a structured summary so the
next turn can resume safely when prior messages fall out of context. You are
NOT generating long-term memory — memory extraction runs as a separate
pipeline. Your job is session-state preservation, not knowledge distillation.
</role>

<tool_contract>
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- Do NOT use read_file, write_file, web_search, execute_code, or ANY other tool.
- You already have every piece of context you need in the conversation below.
- Tool calls will be REJECTED and will WASTE your only turn.
- Your entire response must be plain text: one <analysis> block followed by
  one <summary> block. Nothing before, between, or after.
</tool_contract>

<scratchpad_pattern>
This prompt uses an analysis/summary scratchpad:
- The <analysis> block is your reasoning surface. It is STRIPPED before the
  summary reaches the next turn's context, so spend tokens freely here.
- The <summary> block is what the next turn actually sees. Every character
  here costs context budget forever — be dense and concrete.

Why this split matters: concise summaries without a scratchpad tend to drop
specific file paths, user corrections, and in-flight work. The scratchpad
lets you surface everything first, then compress deliberately.
</scratchpad_pattern>

<autonomy_run_state>
When the conversation includes autonomous work, preserve the run state without
turning it into durable memory:
- Objective Ledger is the source of truth for goals.
- Trigger is wake policy, not the goal itself.
- focus.md is a readable projection of current objectives.
- Preserve objective ids, objective keys, status, success criteria, blocker
  reasons, and objective_session_key values when they are needed to resume.
- Preserve Runtime Task / Attempt ids, trigger/heartbeat run status, output
  artifacts, and artifact paths when they are needed to resume or audit.
- Preserve attempt evidence exactly enough for objective evaluation.
- Do not rewrite autonomous run state as long-term memory, soul.md identity,
  user preference, or general policy. Memory extraction and dream promotion
  decide durable lessons separately.
</autonomy_run_state>

<analysis_instructions>
Wrap detailed analysis in <analysis> tags. Cover all of:
1. **Chronological walkthrough** — each user request, your approach, outcome.
2. **Technical surface** — every file path, code snippet, function signature,
   architecture decision, API shape.
3. **User corrections and feedback** — these are the highest-value signals;
   do not let any slip through.
4. **Errors and resolutions** — the error, root cause, what fixed it.
5. **Problem-solving trajectory** — what was tried, what worked, what failed.
   Failed approaches are as important as successful ones — prevents retrying.
6. **Double-check** for technical accuracy and completeness before writing the
   summary — every required field must be addressed thoroughly.
</analysis_instructions>

<summary_format>
After </analysis>, produce the summary in <summary> tags. Use EXACTLY these
11 fields in this exact order. Do not skip any field — use "(none)" if empty.

**Primary Request and Intent:** [core goal + current status — be specific]
**Key Technical Concepts & Decisions:** [technologies, frameworks, and patterns in play; architecture choices, constraints, tradeoffs decided]
**Files and Code Sections:** [file_path:line_number + full code snippets where applicable for critical changes, plus why each file matters]
**Problem Solving:** [approaches tried, what worked, what didn't — prevent re-trying failed approaches]
**Errors and Fixes:** [errors encountered + root causes + resolutions]
**All User Messages:** [list ALL user messages that are not tool results — critical for tracking changing intent]
**User Preferences:** [corrections, stated preferences, feedback — highest priority to preserve]
**Tool Outcomes:** [key tool calls and their results — focus on outcomes, not individual calls]
**Pending Tasks:** [incomplete items + where work left off — include direct quotes from recent messages]
**Current Work:** [what was actively being done when compression triggered — include file names and code snippets where applicable]
**Next Step:** [the next step DIRECTLY in line with the user's most recent explicit request and the task in progress when compression triggered. Include a verbatim quote from the most recent conversation showing exactly where work left off, so there is no drift in task interpretation. If the last task concluded, use "(none)" — do NOT start tangential requests or already-completed old requests without confirming with the user first]
</summary_format>

<good_summary_example>
For a conversation where a user had asked to fix an auth bug, corrected the
approach once, and left mid-way through adding a regression test:

<summary>
**Primary Request and Intent:** Fix token-expiry race in auth middleware. User
explicitly scoped to middleware.py only — do not touch refresh.py even though
a related bug exists there. Status: fix landed, regression test in progress.
**Key Technical Concepts & Decisions:** FastAPI middleware chain, JWT refresh
flow. Reorder refresh check before response-header write (not after). User
rejected the alternative "wrap handler in try/except" as too broad.
**Files and Code Sections:** backend/app/auth/middleware.py:138-148 (fix applied
— refresh check now precedes header write, closing the race window);
backend/tests/auth/test_middleware.py::test_expired_token_refreshes (new, in
progress — fixture mock_clock not yet wired up).
**Problem Solving:** First attempt moved refresh to a decorator — user rejected
("too magic"). Second attempt (inline reorder) accepted.
**Errors and Fixes:** pytest AttributeError on mock_clock — unresolved, blocker
for test completion.
**All User Messages:** 1) "Fix the token-expiry race." 2) "No, don't touch
refresh.py, scope to middleware only." 3) "Decorator is too magic — inline it."
4) "Add a regression test before we call this done."
**User Preferences:** Prefers inline code over decorators for auth paths.
Strictly scopes fixes — does not want related-but-separate bugs touched.
**Tool Outcomes:** Edit middleware.py:138-148 applied; pytest last run failed
at fixture setup (mock_clock missing).
**Pending Tasks:** Wire up mock_clock fixture, then re-run pytest.
**Current Work:** Writing test_expired_token_refreshes; stuck at mock_clock.
**Next Step:** Wire up the mock_clock fixture so the regression test runs.
User's last message: "Add a regression test before we call this done." —
work stopped at the failing fixture setup.
</summary>
</good_summary_example>

<bad_summary_examples>
DO NOT produce summaries like these:

❌ **Over-compressed, loses file paths**
```
**Primary Request and Intent:** User wanted a bug fixed.
**Files and Code Sections:** Changed the auth module.
**Pending Tasks:** Finish the test.
```
(No file paths, no line numbers — next turn cannot pick up the work.)

❌ **Drops user corrections**
```
**Primary Request and Intent:** Fix token-expiry bug.
**User Preferences:** (none)
```
(User explicitly rejected the decorator approach — that's the highest-value
signal. Never drop corrections.)

❌ **Narrates prose instead of structured fields**
```
<summary>
The user asked for a fix and I worked on it for a while. We tried a few
approaches and eventually settled on one. The test is still being written.
</summary>
```
(Missing all 11 required fields. Parent parses the structure — prose breaks it.)

❌ **Rewrites session state as policy**
```
**User Preferences:** Users universally prefer inline code over decorators.
```
(One user, one session — not universal policy. Memory extraction handles
long-term policy; you handle session state only.)

❌ **Forgets "Current Work" so next turn has no anchor**
```
**Current Work:** (none)
```
(Unless the session genuinely ended, always name the specific in-flight
action so the next turn can resume without re-asking the user.)

❌ **Invents a next step the user never asked for**
```
**Next Step:** Refactor refresh.py to clean up the related bug found earlier.
```
(The user explicitly scoped to middleware.py only. A next step must trace to
the user's most recent explicit request — tangential or already-completed
work needs fresh confirmation, never a self-assigned restart.)
</bad_summary_examples>

<hard_rules>
1. Output ONLY: `<analysis>...</analysis><summary>...</summary>`. No prose
   outside. No other tags. No tool calls.
2. All 11 summary fields must appear in the specified order. Empty fields
   use "(none)" — never omit.
3. Preserve file paths, line numbers, and code snippets verbatim. These are
   more valuable than descriptive prose.
4. Preserve user corrections word-for-word where practical. They are the
   highest-value signal.
5. Respond in the same language as the conversation. Do not translate.
</hard_rules>\
"""


# ── Summary input construction (docs/compaction-cc-alignment.md §3 P0) ──
# CC baseline: the FULL history goes into the summary request; mechanical
# truncation appears only as an over-window fallback (truncateHeadForPTLRetry
# philosophy). Per-message caps below are defensive limits against single
# anomalous entries (e.g. an un-spilled giant tool result), not routine pruning.
_SUMMARY_INPUT_USER_ASSISTANT_CAP = 8000  # chars per user/assistant message (was 800)
_SUMMARY_INPUT_TOOL_RESULT_CAP = 12000  # chars per tool result (was 1500)
_SUMMARY_INPUT_TOOL_ARGS_CAP = 2000  # chars per tool-call args preview (was 300)
_SUMMARY_INPUT_WINDOW_RATIO = 0.7  # input budget as fraction of the summary model window
_SUMMARY_MAX_OUTPUT_TOKENS = 8000  # was 2500; CC uses 20K — 8K is the safe cross-provider value


def _serialize_message_for_summary(msg: dict) -> list[str]:
    """Serialize one message into summary-input lines with defensive caps."""
    lines: list[str] = []
    role = msg.get("role", "")
    content = msg.get("content", "")

    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            args_preview = fn.get("arguments", "")[:_SUMMARY_INPUT_TOOL_ARGS_CAP]
            lines.append(f"assistant: [called {name}({args_preview})]")
        return lines

    if role == "tool":
        if isinstance(content, str) and content.strip():
            lines.append(f"tool_result: {content[:_SUMMARY_INPUT_TOOL_RESULT_CAP]}")
        return lines

    if not isinstance(content, str) or not content.strip():
        return lines
    if role == "user":
        lines.append(f"user: {content[:_SUMMARY_INPUT_USER_ASSISTANT_CAP]}")
    elif role == "assistant" and not is_llm_error_message(content):
        lines.append(f"assistant: {content[:_SUMMARY_INPUT_USER_ASSISTANT_CAP]}")
    return lines


def _resolve_summary_input_budget_chars(provider: str, max_input_tokens: int | None) -> int:
    """Input char budget = summary-model window × ratio, in provider chars-per-token."""
    window_tokens = max_input_tokens
    if not window_tokens or window_tokens <= 0:
        try:
            from app.services.llm_client import get_provider_spec

            spec = get_provider_spec(provider)
            window_tokens = spec.max_input_tokens if spec else None
        except Exception as exc:
            logger.debug("[Summarizer] Provider spec lookup failed for %s: %s", provider, exc)
            window_tokens = None
    if not window_tokens or window_tokens <= 0:
        window_tokens = 128000
    return int(window_tokens * _SUMMARY_INPUT_WINDOW_RATIO * _get_chars_per_token(provider))


def _build_summary_input(
    messages: list[dict],
    *,
    provider: str,
    max_input_tokens: int | None = None,
) -> tuple[str, int]:
    """Serialize the FULL message history for the summary LLM.

    Returns (text, dropped_message_count). Mechanical head-drop happens ONLY
    when the serialized input exceeds the summary model's window budget —
    oldest messages go first, mirroring CC's truncateHeadForPTLRetry.
    """
    per_message: list[str] = []
    for msg in messages:
        block = "\n".join(_serialize_message_for_summary(msg))
        if block:
            per_message.append(block)

    if not per_message:
        return "", 0

    budget_chars = _resolve_summary_input_budget_chars(provider, max_input_tokens)

    # Accumulate from the newest backwards; everything older than the budget is dropped.
    kept_reversed: list[str] = []
    used = 0
    for block in reversed(per_message):
        cost = len(block) + 1  # +1 for the joining newline
        if used + cost > budget_chars and kept_reversed:
            break
        kept_reversed.append(block)
        used += cost

    dropped = len(per_message) - len(kept_reversed)
    if dropped:
        logger.warning(
            "[Summarizer] Summary input over window budget — dropped %d oldest of %d messages",
            dropped,
            len(per_message),
            extra={"metric": "summary_input_head_drop", "dropped": dropped, "total": len(per_message)},
        )
    return "\n".join(reversed(kept_reversed)), dropped


def _resolve_summary_max_tokens(provider: str, model: str) -> int:
    """Clamp the summary output budget to the provider/model output cap."""
    try:
        from app.services.llm_client import get_provider_spec

        spec = get_provider_spec(provider)
        if spec is not None:
            provider_cap = spec.model_max_tokens.get(model, spec.default_max_tokens)
            if provider_cap and provider_cap > 0:
                return min(_SUMMARY_MAX_OUTPUT_TOKENS, provider_cap)
    except Exception as exc:
        logger.debug("[Summarizer] Output cap lookup failed for %s/%s: %s", provider, model, exc)
    return _SUMMARY_MAX_OUTPUT_TOKENS


async def _llm_summarize(messages: list[dict], model_config: dict) -> str | None:
    """Use LLM to create a detailed summary of old messages.

    Uses <analysis>/<summary> scratchpad pattern: LLM reasons in <analysis>
    tags (stripped before persistence), outputs clean summary in <summary> tags.
    """
    from app.services.llm_client import LLMMessage, create_llm_client_from_config

    provider = model_config.get("provider", "")
    model_name = model_config.get("model", "")
    text, _ = _build_summary_input(
        messages,
        provider=provider,
        max_input_tokens=model_config.get("max_input_tokens"),
    )
    if not text:
        return None

    client = create_llm_client_from_config(model_config)
    try:
        response = await client.stream(
            messages=[
                LLMMessage(role="system", content=_SUMMARIZE_SYSTEM_PROMPT),
                LLMMessage(role="user", content=text),
            ],
            max_tokens=_resolve_summary_max_tokens(provider, model_name),
            temperature=0.3,
        )
        return _extract_summary_from_response(response.content)
    finally:
        await client.close()
