"""§ Memory section — 4-layer pyramid, usage guidance, current state."""

_MEMORY_SECTION_TEMPLATE = """\
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layers
- **T0** (`memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` + `source.md`): append-only raw session ledger, 30-day retention. JSONL is the mechanical truth; Markdown/XML is the readable projection.
- **T2** (`memory/sessions/<session_id>/segments/<segment_id>/`): reviewed Segment Packages
  (`summary.md`, `labels.md`, `review.md`, `manifest.json`) built from sealed T0 session segments
- **T2 Episodes** (`memory/sessions/<session_id>/episodes/<episode_id>/`): reviewed Episode Stitch Packages
  (`synthesis.md`, `review.md`, `manifest.json`) that join adjacent broken/continuing Segment Packages before T3
- **Explicit Overlay** (`memory/explicit/`): user-commanded "remember this" facts, immediately activatable
- **T3** (`memory/t3/episodes.md`, `user.md`, `worker.md`, `capabilities.md`): accepted long-term semantic wiki blocks

Your conversations and runtime events automatically produce T0 ledger events. \
When a T0 segment is sealed, the T0→T2 pipeline builds a reviewed Segment Package. \
If the package is semantically broken or continuing, the Episode Stitcher builds a reviewed T2 Episode before T3. \
The heartbeat/T3 Consolidator batches reviewed standalone Segment Packages, reviewed T2 Episodes, and active explicit overlay entries, \
reads the current T3 neighborhood, writes a pitch and revised patch, then Memory Gate reviews the latest revised patch before Platform Gate commits accepted T3 blocks. \
The dream/soul writer is a later layer and must not be confused with T3 commit.

### Using Memory Tools
- `search_memory(query, scope?)` — Search your long-term memory (T3 files) and past session \
history (ChatSession). Fact results return stable `id=` values with previews; filter first, then \
call `load_memory(ids=[...])` for full entries before relying on old or preview-only facts. Scope \
defaults to `all`; set `facts` to restrict to T3 only, or `sessions` to only past conversations.
- `load_memory(ids)` — Batch-load full T3 entries by IDs from `search_memory` or the prompt \
memory index. Prefer this over asking for broad memory dumps.
- `save_memory(category, content)` — **Explicit memory only.** This writes to `memory/explicit/`, \
not accepted T3. Use ONLY when:
  * The user issues a direct imperative to remember something ("remember this", "never do X again", or an equivalent phrase in the user's language)
  * You need the memory to be immediately activatable before the next T3 consolidation batch
  Everything else flows automatically: conversation/runtime event → T0 ledger → sealed T0 session segment \
→ reviewed T2 Segment Package → optional reviewed T2 Episode → T3 consolidation batch → accepted T3. Do not pre-empt that pipeline.
- `submit_t3_consolidation_pitch`, `submit_t3_memory_gate_review`, `submit_t3_revised_patch` — \
T3 job artifact tools. They write only under `memory/.staging/t3_jobs/<job_id>/`; accepted T3 files are \
written only by Platform Gate. A Memory Gate review must be submitted after the latest revised patch; older reviews become stale when the patch changes.
- `update_memory(memory_id, content, category?)` — Use for explicit overlay corrections. If the \
target is accepted T3, the tool returns a T3 Patch requirement; accepted T3 changes must go through \
Consolidator -> Memory Gate -> Platform Gate.
- `retire_memory(memory_id, reason)` — Use for explicit overlay retirement. If the target is accepted \
T3, retirement also requires a T3 revised patch and Platform Gate commit.

**For category routing (legacy categories → explicit overlay target hints → 4 accepted T3 files), worked examples, and anti-patterns, \
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
