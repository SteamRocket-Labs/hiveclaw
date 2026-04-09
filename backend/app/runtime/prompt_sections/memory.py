"""§ Memory section — 4-layer pyramid, usage guidance, current state."""

_MEMORY_SECTION_TEMPLATE = """\
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layer Structure
| Layer | Files | Purpose | Lifecycle |
|-------|-------|---------|-----------|
| T0 Raw Logs | logs/YYYY-MM-DD/*.md | Complete session records | 30 days |
| T1 Working | focus.md | Current task list | Volatile |
| T2 Episodic | learnings/*.md | Recent observations | Curated by heartbeat |
| T3 Semantic | memory/*.md + soul.md | Long-term knowledge | Refined by dream |

### How Memory Flows
1. Your conversations automatically produce T0 logs and T2 extractions
2. The heartbeat curates T2 → T3 every ~45 minutes (quality filtering)
3. The dream refines T3 and promotes patterns to soul.md every ~4 hours

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
