"""§ Memory section — 4-layer pyramid, usage guidance, current state."""

_MEMORY_SECTION_TEMPLATE = """\
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layers
- **T0** (logs/): raw session records, 30-day retention
- **T1** (focus.md): current task list, volatile
- **T2** (learnings/): recent observations, curated by heartbeat every ~45 min
- **T3** (memory/*.md + soul.md): long-term knowledge, refined by dream every ~4 hours

Your conversations automatically produce T0 logs and T2 extractions. \
The heartbeat curates T2 → T3. The dream refines T3 and promotes patterns to soul.md.

### Using Memory Tools
- `save_memory(category, content)` — Directly write to T3 (use sparingly, heartbeat handles most curation)
- `search_memory(query)` — Search T3 via FTS5 for relevant knowledge

### What's Worth Remembering
- User corrections and preferences (highest value)
- Project decisions and constraints
- Strategies that worked or failed
- NOT: code patterns, file paths, debugging steps (these are in the workspace)
- NOT: ephemeral task details (those belong in focus.md)

### Current Memory State
{memory_snapshot}\
"""


def build_memory_section(memory_snapshot: str = "") -> str:
    snapshot = memory_snapshot.strip() if memory_snapshot else "(no memory loaded)"
    return _MEMORY_SECTION_TEMPLATE.format(memory_snapshot=snapshot)
