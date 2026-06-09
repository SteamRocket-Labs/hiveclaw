"""Focus task-id slug normalizer.

The focus.md "Objective Projection" was retired — a schedule is just a
trigger, not an objective. The only survivor is :func:`normalize_focus_task_id`,
a pure string-slug helper that trigger code still uses to canonicalize the
optional ``AgentTrigger.focus_ref`` field.
"""

from __future__ import annotations

import re


def normalize_focus_task_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    return cleaned or "task"
