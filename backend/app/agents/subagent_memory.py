"""Subagent memory (cut ⑥): tenant-scoped 记忆.md + governed write (How-not-What).

The "mutilated self-evolution" layer. A named subagent accumulates *implicit
how-to craft* — source/judgment calibration, pitfalls, pointer maps — NOT domain
knowledge (that belongs to the digital employee's soul/T3). One layer only:
task → distill How → write back to 记忆.md. No T3 curation / soul / dream.

Memory Control Plane invariant (铁律): every durable write goes through
``prepare_memory_write`` (privacy/sensitivity classification, PL4 rejection,
lifecycle metadata). A rejected write ABORTS — it never falls back to raw
content, and the store never hand-assembles Markdown around the gate.

Storage is tenant-scoped (a separate ``base_dir`` per tenant). The offline daemon
that scans T0 logs and LLM-distills How is wired on top of ``distill_and_record``
via an injected distiller; periodic scheduling + T0 scanning is operational
follow-up (kept out of this cut so the governed-write invariant lands first).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.agents.subagent_definition import _tenant_subagent_root, validate_subagent_name
from app.memory.write_gate import prepare_memory_write

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

# A distiller turns a run log into [(how_category, how_text), ...]. Injected by the
# daemon (LLM or pattern) so this module stays testable and IO-free.
HowDistiller = Callable[[str], list[tuple[str, str]]]


@dataclass(slots=True)
class HowWriteResult:
    written: bool
    rejected: bool
    reason: str = ""
    content: str = ""


class SubagentMemoryStore:
    """Tenant-scoped store for a subagent's 记忆.md (governed append + load)."""

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)

    def _path(self, spec_name: str) -> Path:
        safe_name = validate_subagent_name(spec_name)
        base = self.base_dir.resolve()
        path = (base / f"{safe_name}.记忆.md").resolve()
        if not path.is_relative_to(base):
            raise ValueError("invalid subagent name: path escapes tenant memory store")
        return path

    def record_how(self, spec_name: str, how_text: str, *, category: str) -> HowWriteResult:
        """Append one implicit-How entry — MUST pass the governed write gate.

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
        if decision.rejected:
            logger.warning("[SubagentMemory] write rejected by gate: name=%s reason=%s", spec_name, decision.reason)
            return HowWriteResult(written=False, rejected=True, reason=decision.reason)

        self.base_dir.mkdir(parents=True, exist_ok=True)
        entry = self._format_entry(decision.content, category=category, metadata=decision.metadata)
        with self._path(spec_name).open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return HowWriteResult(written=True, rejected=False, content=decision.content)

    def load(self, spec_name: str) -> str:
        """Return the subagent's 记忆.md text (empty string if none yet)."""

        path = self._path(spec_name)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _format_entry(content: str, *, category: str, metadata: dict) -> str:
        entry_id = metadata.get("entry_id", "")
        sensitivity = metadata.get("sensitivity", "")
        return f"\n- [{category}][s={sensitivity}][id={entry_id}] {content}\n"


def distill_and_record(
    store: SubagentMemoryStore,
    spec_name: str,
    run_log: str,
    *,
    distiller: HowDistiller,
) -> list[HowWriteResult]:
    """One-layer evolution: distill How from a run log, record each via the gate.

    ``distiller`` is injected (the daemon supplies the LLM/pattern extractor). This
    is the single ``task → distill → governed write-back`` layer — no T3/soul/dream.
    Periodic T0 scanning + scheduling is operational wiring on top of this.
    """

    return [store.record_how(spec_name, how_text, category=category) for category, how_text in distiller(run_log)]


def memory_store_for_tenant(tenant_id: object, *, agent_data_dir: Path | str | None = None) -> SubagentMemoryStore:
    """Construct the tenant-scoped persistent subagent memory store."""

    return SubagentMemoryStore(_tenant_subagent_root(tenant_id, kind="memory", agent_data_dir=agent_data_dir))


def memory_store_for_agent(agent_id: object, *, agent_data_dir: Path | str | None = None) -> SubagentMemoryStore:
    """Construct the agent-scoped memory store (§12.5: memory follows the
    definition's scope).

    Agent-private definitions accumulate craft at
    ``<workspace>/subagents/.memory/<name>.记忆.md`` — the dot-dir keeps memory
    files out of the definition glob, and two agents with same-named private
    definitions never share memory. Tenant isolation is inherited (the agent
    workspace already lives inside the tenant boundary); the governed write
    gate applies identically.
    """

    from app.agents.subagent_definition import agent_subagent_root

    return SubagentMemoryStore(agent_subagent_root(agent_id, agent_data_dir=agent_data_dir) / ".memory")
