"""§ Memory section — 4-layer pyramid, usage guidance, current state."""

_MEMORY_SECTION_TEMPLATE = """\
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layers
- **T0** (logs/): raw session records, 30-day retention
- **T1** (Objective Ledger + focus.md projection): current goals, wake context, and readable projection
- **T2** (learnings/): recent observations, curated by heartbeat every ~45 min
- **T3** (memory/*.md + soul.md): long-term knowledge, refined by dream every ~4 hours

Your conversations automatically produce T0 logs and T2 extractions. \
The heartbeat curates T2 → T3. The dream refines T3 and promotes patterns to soul.md.

### Using Memory Tools
- `search_memory(query, scope?)` — Search your long-term memory (T3 files) and past session \
history (ChatSession). Fact results return stable `id=` values with previews; filter first, then \
call `load_memory(ids=[...])` for full entries before relying on old or preview-only facts. Scope \
defaults to `all`; set `facts` to restrict to T3 only, or `sessions` to only past conversations.
- `load_memory(ids)` — Batch-load full T3 entries by IDs from `search_memory` or the prompt \
memory index. Prefer this over asking for broad memory dumps.
- `save_memory(category, content)` — **Escape hatch only.** This writes DIRECTLY to T3, \
bypassing the heartbeat curation that normally filters low-signal content. Use ONLY when:
  * The user issues a direct imperative ("记住", "remember this", "never do X again")
  * You must override something heartbeat would otherwise drop
  Everything else flows automatically: conversation → T0 logs → extractor picks salient bits \
into T2 → heartbeat curates T2 into T3. Do not pre-empt that pipeline.

**For category routing (8 categories → 5 T3 files), worked examples, and anti-patterns, \
load the `memory-guide` system skill before your first `save_memory` call.**

### What's Worth Remembering
- User corrections and preferences (highest value)
- Project decisions and constraints
- Strategies that worked or failed
- NOT: code patterns, file paths, debugging steps (these are in the workspace)
- NOT: ephemeral task details (goal state belongs in Objective Ledger; artifacts and notes belong in workspace files)

### Current Memory State
{memory_snapshot}\
"""


def build_memory_section(memory_snapshot: str = "", *, budget_chars: int | None = None) -> str:
    """Render the memory section, optionally trimming the snapshot to a budget.

    The static template (~1.3K chars) is always preserved — only the
    `memory_snapshot` body is trimmed when `budget_chars` is set, so the
    layered usage guidance stays intact even on tiny budgets.
    """
    snapshot = memory_snapshot.strip() if memory_snapshot else "(no memory loaded)"
    if budget_chars is not None and budget_chars > 0 and len(snapshot) > budget_chars:
        snapshot = snapshot[:budget_chars].rstrip() + "\n...(memory snapshot trimmed)"
    return _MEMORY_SECTION_TEMPLATE.format(memory_snapshot=snapshot)
