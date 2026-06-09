"""§ Plan Mode Guidance — when to SUGGEST planning first.

A (user correction): the agent's judgment may SUGGEST entering Plan Mode, but it
NEVER enters Plan Mode itself — entry is always the user's explicit choice. This
section moves the "should we plan?" judgment out of a pre-LLM regex and into the
model's own reasoning, surfaced as a suggestion in its reply.

Gated to live interactive chat surfaces (a user must be present to act on the
suggestion); unattended runs (trigger/heartbeat/delegation) get no guidance.
The eligible-surface set is owned by ``runtime/session.py`` — consumed here, not
redefined.
"""

from __future__ import annotations

_PLAN_MODE_GUIDANCE_SECTION = """\
## When to Suggest Planning First

For some requests, drafting and confirming a plan before acting saves rework and
prevents surprises. You may SUGGEST that the user enter Plan Mode — you never enter
it yourself. Entry is always the user's explicit choice; if they decline, proceed
normally.

Suggest planning first when the task is:
- Multi-step or spans several files/systems, where order and scope matter.
- Irreversible or externally visible (sends, deletes, schedules, payments, posts).
- Ambiguous in scope or success criteria — confirming the shape first avoids wasted work.
- High-cost (long-running research, large generation) where a wrong direction is expensive.
- A standing/recurring automation (a trigger) whose cadence, recipients,
  and stop conditions the user should approve up front.

Do NOT suggest planning for:
- Simple, single-step, or read-only requests — just do them.
- Work whose scope is already clear and cheap to redo.
- Anything the user has already approved or explicitly asked you to start.

How to suggest: end your reply with a short, concrete recommendation — name why
planning helps for THIS task and what the plan would cover — then let the user
decide. One suggestion per request; once they decline or proceed, don't re-ask."""


def build_plan_mode_guidance_section() -> str:
    return _PLAN_MODE_GUIDANCE_SECTION


def should_show_plan_mode_guidance(source: str = "", channel: str = "") -> bool:
    """True only on live interactive chat surfaces, where a user can act on the
    suggestion. Consumes the eligible-surface set owned by ``runtime/session.py``."""
    from app.runtime.session import _INTERACTIVE_PLAN_CHAT_SURFACES

    normalized_source = str(source or "").lower()
    normalized_channel = str(channel or "").lower()
    return (
        normalized_source in _INTERACTIVE_PLAN_CHAT_SURFACES
        or normalized_channel in _INTERACTIVE_PLAN_CHAT_SURFACES
    )
