"""§ Plan Mode Guidance — when to REQUEST planning first.

A (user correction): the agent's judgment may REQUEST entering Plan Mode via the
``request_plan_mode`` tool, but it NEVER enters Plan Mode itself — entry is always
the user's explicit approval. ``request_plan_mode`` surfaces an approval card; the
user is the gate. This section moves the "should we plan?" judgment out of a
pre-LLM regex and into the model's own reasoning.

Gated to live interactive chat surfaces (a user must be present to approve the
request); unattended runs (trigger/heartbeat/delegation) get no guidance.
The eligible-surface set is owned by ``runtime/session.py`` — consumed here, not
redefined.
"""

from __future__ import annotations

_PLAN_MODE_GUIDANCE_SECTION = """\
## When to Request Planning First

For some requests, drafting and confirming a plan before acting saves rework and
prevents surprises. When that is the case, call ``request_plan_mode(reason)`` to
ask the user to enter Plan Mode — you never enter it yourself. Entry is always the
user's explicit approval: if they approve, you draft a confirmable plan; if they
decline, proceed normally.

Request planning first when the task is:
- Multi-step or spans several files/systems, where order and scope matter.
- Irreversible or externally visible (sends, deletes, schedules, payments, posts).
- Ambiguous in scope or success criteria — confirming the shape first avoids wasted work.
- High-cost (long-running research, large generation) where a wrong direction is expensive.
- A standing/recurring automation (a trigger) whose cadence, recipients,
  and stop conditions the user should approve up front.

Do NOT request planning for:
- Simple, single-step, or read-only requests — just do them.
- Work whose scope is already clear and cheap to redo.
- Work the user has already approved or explicitly asked you to start, unless it is irreversible, externally visible, high-cost, ambiguous, or multi-system.

How to request: call ``request_plan_mode`` with a concrete ``reason`` — name why
planning helps for THIS task and what the plan would cover — then END your turn and
wait for the user's decision. One request per task; once they decline or proceed,
don't re-ask."""


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
