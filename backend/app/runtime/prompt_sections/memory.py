"""§ Memory section — 4-layer pyramid, usage guidance, current state."""

_MEMORY_SECTION_TEMPLATE = """\
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layers
- **T0** (`memory/t0/sessions/<session_id>/segments/<segment_id>/source.md`): append-only raw session ledger, 30-day retention
- **T2** (`memory/sessions/<session_id>/segments/<segment_id>/`): reviewed Segment Packages
  (`summary.md`, `labels.md`, `review.md`, `manifest.json`) built from sealed T0 session segments
- **T3** (memory/*.md + soul.md): long-term knowledge, refined by dream about once a day

Your conversations and runtime events automatically produce T0 ledger events. \
When a T0 segment is sealed, the T0→T2 pipeline builds a reviewed Segment Package. \
The heartbeat curates reviewed T2 packages → T3. The dream refines T3 and promotes patterns to soul.md.

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
  Everything else flows automatically: conversation/runtime event → T0 ledger → sealed T0 session segment \
→ reviewed T2 Segment Package → heartbeat curates T2 into T3. Do not pre-empt that pipeline.
- `update_memory(memory_id, content, category?)` — Use when a loaded T3 fact is wrong or stale \
and the user gives an explicit correction. The replacement is write-gated, and the old entry is \
archived with a supersession edge.
- `retire_memory(memory_id, reason)` — Use when a loaded T3 fact should leave active recall \
without replacement. This archives evidence; it does not delete history.

**For category routing (8 categories → 5 T3 files), worked examples, and anti-patterns, \
load the `memory-guide` system skill before your first memory write/update/retire call.**

### What's Worth Remembering
- User corrections and preferences (highest value)
- Project decisions and constraints
- Strategies that worked or failed
- NOT: code patterns, file paths, debugging steps (these are in the workspace)
- NOT: ephemeral task details (in-flight work belongs in your work ledger; artifacts and notes belong in workspace files)

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
        snapshot = snapshot[:budget_chars].rstrip() + (
            "\n...(memory context trimmed to fit budget — use search_memory to retrieve more)"
        )
    return _MEMORY_SECTION_TEMPLATE.format(memory_snapshot=snapshot)
