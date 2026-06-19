"""Read-only audit for self-evolution memory hygiene."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.memory.md_store import build_t3_entry_manifest
from app.memory.t2_store import load_t2_entries
from app.services.evolution_ledger import load_evolution_ledger


def _has_source_ref_text(line: str) -> bool:
    lowered = line.lower()
    return "[refs=" in lowered or "source_ref" in lowered or "trace_ref" in lowered


def _soul_learned_lines(soul_text: str) -> list[str]:
    lines = []
    in_learned = False
    for raw in soul_text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_learned = line.lower() == "## learned behaviors"
            continue
        if in_learned and line.startswith("- "):
            lines.append(line)
    return lines


def run_self_evolution_audit(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    write_report: bool = False,
) -> dict[str, Any]:
    workspace = Path(data_root) / str(agent_id)
    t2_entries, _mtimes = load_t2_entries(Path(data_root), agent_id)
    t2_without_evidence = [
        entry for entry in t2_entries
        if not entry.get("evidence") or not entry.get("source_refs")
    ]

    t3_without_refs = 0
    for entry in build_t3_entry_manifest(Path(data_root), agent_id):
        refs = entry.metadata.get("source_refs") or entry.metadata.get("evidence_refs") or ""
        if not refs and not _has_source_ref_text(entry.content):
            t3_without_refs += 1

    ledger_entries = load_evolution_ledger(workspace)
    promoted_candidate_ids = {
        str(entry.get("candidate_id"))
        for entry in ledger_entries
        if entry.get("event") == "memory_promotion_decision" and entry.get("decision") == "promote"
    }
    soul_lines = []
    soul_path = workspace / "soul.md"
    if soul_path.exists():
        soul_lines = _soul_learned_lines(soul_path.read_text(encoding="utf-8", errors="replace"))
    soul_without_records = len(soul_lines) if not promoted_candidate_ids else 0

    dream_without_trace = sum(
        1
        for entry in ledger_entries
        if entry.get("event") == "memory_promotion_candidate"
        and entry.get("target_type") == "memory:soul"
        and not entry.get("source_refs")
    )

    report: dict[str, Any] = {
        "agent_id": str(agent_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "t2_entries": len(t2_entries),
        "t2_entries_without_evidence": len(t2_without_evidence),
        "t3_entries_without_source_ref": t3_without_refs,
        "soul_lines_without_promotion_record": soul_without_records,
        "dream_promotions_without_trace_ref": dream_without_trace,
        "retriever_index_shadow_miss_rate": None,
    }

    if write_report:
        report_dir = Path(data_root) / "tmp" / "reports" / "self-evolution-audit"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{agent_id}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
    return report
