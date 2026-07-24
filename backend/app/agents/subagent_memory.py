"""Subagent memory (cut ⑥): tenant-scoped 记忆.md + governed write (How-not-What).

The "mutilated self-evolution" layer. A named subagent accumulates *implicit
how-to craft* — source/judgment calibration, pitfalls, pointer maps — NOT domain
knowledge (that belongs to the digital employee's soul/T3). One layer only:
task → distill How → write back to 记忆.md. No T3 curation / soul / dream.

Memory Control Plane invariant (铁律): runtime durable writes go through
``prepare_memory_write_with_llm`` (LLM-primary threat classification,
privacy/sensitivity classification, exact-authority PL4 rejection, lifecycle
metadata). Secret-shaped prose is never a PL4 fact by itself. Runtime ingress
and model-output boundaries remove authority-bound credential bytes before this
lane; the sync ``prepare_memory_write`` path is retained for offline
tests/migrations. A rejected write ABORTS — it never falls back to raw content,
and the store never hand-assembles Markdown around the gate.

Storage is tenant-scoped (a separate ``base_dir`` per tenant). The offline daemon
that scans the T0 session ledger and LLM-distills How is wired on top of ``distill_and_record``
via an injected distiller; periodic scheduling + T0 scanning is operational
follow-up (kept out of this cut so the governed-write invariant lands first).
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.agents.subagent_definition import _tenant_subagent_root, validate_subagent_name
from app.memory.write_gate import MemoryWriteDecision, prepare_memory_write, prepare_memory_write_with_llm
from app.services.llm_client import chat_complete, get_max_tokens

logger = logging.getLogger(__name__)

# How-craft categories a subagent may record. NOT "what" (domain facts → the
# digital employee's knowledge); only "how" (craft). This is the guard that keeps
# a subagent from slowly growing into a second analyst.
SUBAGENT_HOW_CATEGORIES = (
    "source_calibration",
    "judgment_calibration",
    "pitfall",
    "pointer_map",
)

# A distiller turns a run log into [(how_category, how_text), ...]. Injected by
# the spawn path / daemon (LLM or pattern, sync or async) so this module stays
# testable and IO-free by default.
HowDistiller = Callable[[str], "list[tuple[str, str]] | Awaitable[list[tuple[str, str]]]"]


@dataclass(slots=True)
class HowWriteResult:
    written: bool
    rejected: bool
    held: bool = False
    reason: str = ""
    content: str = ""


class SubagentMemoryStore:
    """Tenant-scoped store for a subagent's 记忆.md (governed append + load)."""

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)

    def _path(self, spec_name: str) -> Path:
        safe_name = validate_subagent_name(spec_name)
        base = self.base_dir.resolve()
        path = (base / f"{safe_name}.memory.md").resolve()
        if not path.is_relative_to(base):
            raise ValueError("invalid subagent name: path escapes tenant memory store")
        return path

    def record_how(self, spec_name: str, how_text: str, *, category: str) -> HowWriteResult:
        """Append one implicit-How entry through the sync fallback gate.

        * Category must be a How-craft category (never a domain-'what' category).
        * The content is classified/masked by ``prepare_memory_write``; a rejected
          decision ABORTS the write (no raw fallback).
        """

        if category not in SUBAGENT_HOW_CATEGORIES:
            return HowWriteResult(
                written=False,
                rejected=True,
                reason=f"category {category!r} is not a How-craft category {SUBAGENT_HOW_CATEGORIES}",
            )

        decision = prepare_memory_write(how_text, category=category)
        return self._record_decision(spec_name, category=category, decision=decision)

    async def record_how_with_llm(
        self,
        spec_name: str,
        how_text: str,
        *,
        category: str,
        tenant_id: object | None = None,
        agent_id: object | None = None,
    ) -> HowWriteResult:
        """Append one implicit-How entry through the LLM-primary write gate."""

        if category not in SUBAGENT_HOW_CATEGORIES:
            return HowWriteResult(
                written=False,
                rejected=True,
                reason=f"category {category!r} is not a How-craft category {SUBAGENT_HOW_CATEGORIES}",
            )

        decision = await prepare_memory_write_with_llm(
            how_text,
            category=category,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        return self._record_decision(spec_name, category=category, decision=decision)

    def _record_decision(self, spec_name: str, *, category: str, decision: MemoryWriteDecision) -> HowWriteResult:
        if decision.rejected:
            logger.warning("[SubagentMemory] write rejected by gate: name=%s reason=%s", spec_name, decision.reason)
            return HowWriteResult(written=False, rejected=True, reason=decision.reason)
        if decision.held:
            logger.info(
                "[SubagentMemory] write held for semantic review: name=%s reason=%s", spec_name, decision.reason
            )
            return HowWriteResult(written=False, rejected=False, held=True, reason=decision.reason)

        self.base_dir.mkdir(parents=True, exist_ok=True)
        entry = self._format_entry(decision.content, category=category, metadata=decision.metadata)
        with self._path(spec_name).open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return HowWriteResult(written=True, rejected=False, held=False, content=decision.content)

    def load(self, spec_name: str, *, active_only: bool = False) -> str:
        """Return the subagent's memory text (empty string if none yet).

        ``active_only=True`` filters entries already absorbed into the
        definition body (evolution loop P0) — the spawn-time prompt injection
        uses this so promoted craft is never injected twice.
        """

        path = self._path(spec_name)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        if not active_only:
            return text
        return "\n".join(line for line in text.splitlines() if "[absorbed=" not in line)

    def mark_absorbed(self, spec_name: str, entry_ids: list[str], *, proposal_id: str) -> int:
        """Tag entries as absorbed into the definition (reversible: tag, never delete).

        Idempotent — an already-absorbed entry is never re-tagged. Returns the
        number of entries newly tagged. Mirrors dream's archive semantics.
        """

        path = self._path(spec_name)
        if not path.exists() or not entry_ids:
            return 0
        wanted = {str(entry_id) for entry_id in entry_ids if str(entry_id).strip()}
        marked = 0
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "[absorbed=" in line:
                continue
            if any(f"[id={entry_id}]" in line for entry_id in wanted):
                lines[i] = f"{line} [absorbed={proposal_id}]"
                marked += 1
        if marked:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return marked

    def count_active_entries(self, spec_name: str) -> int:
        """Number of not-yet-absorbed entries (the evolution-loop trigger metric)."""

        return sum(1 for line in self.load(spec_name, active_only=True).splitlines() if line.startswith("- ["))

    @staticmethod
    def _format_entry(content: str, *, category: str, metadata: dict) -> str:
        entry_id = metadata.get("entry_id", "")
        sensitivity = metadata.get("sensitivity", "")
        return f"\n- [{category}][s={sensitivity}][id={entry_id}] {content}\n"


async def distill_and_record(
    store: SubagentMemoryStore,
    spec_name: str,
    run_log: str,
    *,
    distiller: HowDistiller,
    tenant_id: object | None = None,
    agent_id: object | None = None,
) -> list[HowWriteResult]:
    """One-layer evolution: distill How from a run log, record each via the gate.

    ``distiller`` is injected (the spawn path supplies the LLM distiller; tests
    and daemons may supply sync pattern extractors). This is the single
    ``task → distill → governed write-back`` layer — no T3/soul/dream.
    """

    raw = distiller(run_log)
    lessons: list[tuple[str, str]] = await raw if inspect.isawaitable(raw) else raw
    return [
        await store.record_how_with_llm(
            spec_name,
            how_text,
            category=category,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        for category, how_text in lessons
    ]


# ── LLM How distiller (production spawn wiring) ────────────────────────────────

# Vendor-neutral by product law (L3 model equality) — pinned by test.
DISTILL_HOW_SYSTEM_PROMPT = """\
You distill reusable CRAFT from a subagent's run log — how it should work next time, never what it learned about the world.

Hard rules:
- HOW, not WHAT: domain facts, findings, and conclusions belong to the caller's report, NOT here. Record only durable improvements to the subagent's own working method.
- Most runs teach nothing durable. An empty list is the normal result — record a lesson ONLY when the run revealed something that would change how the subagent works next time.
- Never record secrets, credentials, or personal data.
- Each lesson is one self-contained sentence, at most 200 characters, written in the same language as the run log.

Categories (choose exactly one per lesson):
- "source_calibration" — which sources/tools proved reliable or unreliable for which purpose
- "judgment_calibration" — where the subagent's confidence was miscalibrated and how to adjust
- "pitfall" — a concrete trap in this kind of task and how to avoid it
- "pointer_map" — where relevant things live (paths, structures, entry points) that took effort to find

Return ONLY a JSON array, no other text: [{"category": "...", "lesson": "..."}, ...] — or [] when nothing durable was learned.
"""


async def llm_distill_how(
    run_log: str,
    *,
    model_config: dict,
    agent_id: object | None = None,
    tenant_id: object | None = None,
) -> list[tuple[str, str]]:
    """LLM-distill How-craft lessons from a run log.

    Fail-soft by design: distillation is a bonus path layered on a completed
    run — an unusable model response logs a warning and yields no lessons, it
    never breaks the spawn result. The governed write gate still judges every
    lesson downstream (record_how).
    """

    try:
        response = await chat_complete(
            provider=model_config["provider"],
            api_key=model_config["api_key"],
            model=model_config["model"],
            base_url=model_config.get("base_url"),
            messages=[
                {"role": "system", "content": DISTILL_HOW_SYSTEM_PROMPT},
                {"role": "user", "content": run_log},
            ],
            temperature=0.2,
            max_tokens=get_max_tokens(
                str(model_config.get("provider") or ""),
                str(model_config.get("model") or ""),
                model_config.get("max_output_tokens"),
            ),
            timeout=45.0,
            usage_source="subagent_memory",
            usage_agent_id=agent_id,
            usage_tenant_id=tenant_id,
        )
        content = str(response.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            raise ValueError("distiller response must be a JSON array")
        lessons: list[tuple[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            lesson = str(item.get("lesson") or "").strip()
            if category and lesson:
                lessons.append((category, lesson))
        return lessons
    except Exception as exc:
        logger.warning("[SubagentMemory] LLM How distillation skipped (model=%s): %s", model_config.get("model"), exc)
        return []


def make_llm_how_distiller(
    model_config: dict,
    *,
    agent_id: object | None = None,
    tenant_id: object | None = None,
) -> Callable[[str], Awaitable[list[tuple[str, str]]]]:
    """Bind a model config into an async HowDistiller for the spawn context."""

    async def _distill(run_log: str) -> list[tuple[str, str]]:
        return await llm_distill_how(run_log, model_config=model_config, agent_id=agent_id, tenant_id=tenant_id)

    return _distill


def memory_store_for_tenant(tenant_id: object, *, agent_data_dir: Path | str | None = None) -> SubagentMemoryStore:
    """Construct the tenant-scoped persistent subagent memory store."""

    return SubagentMemoryStore(_tenant_subagent_root(tenant_id, kind="memory", agent_data_dir=agent_data_dir))


def memory_store_for_agent(agent_id: object, *, agent_data_dir: Path | str | None = None) -> SubagentMemoryStore:
    """Construct the agent-scoped memory store (§12.5: memory follows the
    definition's scope).

    Agent-private definitions accumulate craft at
    ``<workspace>/subagents/.memory/<name>.memory.md`` — the dot-dir keeps memory
    files out of the definition glob, and two agents with same-named private
    definitions never share memory. Tenant isolation is inherited (the agent
    workspace already lives inside the tenant boundary); the governed write
    gate applies identically.
    """

    from app.agents.subagent_definition import agent_subagent_root

    return SubagentMemoryStore(agent_subagent_root(agent_id, agent_data_dir=agent_data_dir) / ".memory")
