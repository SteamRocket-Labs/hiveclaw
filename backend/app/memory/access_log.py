"""Access-count writeback for retriever hits (Phase 3 residual / Phase 13).

Every prompt-included memory entry should accumulate evidence of its own
relevance: `access_count` and `last_accessed` are two of the inputs the
retention formula (§6.3) uses. Up to Phase 12 those fields were only
incremented in-memory after retrieval; this module persists the bump
back to the entry's markdown line so the retention picture survives
restarts and so dream/auto-tune can read it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ACCESS_COUNT_RE = re.compile(r"\[access_count=(?P<count>\d+)\]")
_LAST_ACCESSED_RE = re.compile(r"\[last_accessed=[^\]]+\]")


def bump_access(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    file_relpath: str,
    entry_id: str,
    now: datetime | None = None,
) -> bool:
    """Increment `access_count` and update `last_accessed` for a single entry.

    Returns True when the entry was found and rewritten; False on missing
    file, missing entry_id, or malformed line (no `access_count=` token).
    """

    path = Path(data_root) / str(agent_id) / file_relpath
    if not path.exists():
        return False

    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp = when.isoformat()

    text = path.read_text(encoding="utf-8")
    needle = f"[entry_id={entry_id}]"
    lines = text.splitlines(keepends=True)
    updated = False
    for index, line in enumerate(lines):
        if needle not in line:
            continue
        count_match = _ACCESS_COUNT_RE.search(line)
        if count_match is None:
            return False
        new_count = int(count_match.group("count")) + 1
        rewritten = _ACCESS_COUNT_RE.sub(f"[access_count={new_count}]", line, count=1)
        if _LAST_ACCESSED_RE.search(rewritten):
            rewritten = _LAST_ACCESSED_RE.sub(f"[last_accessed={timestamp}]", rewritten, count=1)
        else:
            rewritten = rewritten.rstrip("\n") + f" [last_accessed={timestamp}]\n"
        lines[index] = rewritten
        updated = True
        break

    if not updated:
        return False

    path.write_text("".join(lines), encoding="utf-8")
    return True
