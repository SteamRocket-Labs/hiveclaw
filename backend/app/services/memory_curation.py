"""Memory curation runtime — wires scene/wiki curators into the heartbeat tick.

Spec §12 P5: the curators are pure modules with injected LLMs; this module is
their live entry point. Each heartbeat tick it:

1. Reads the curation cursor (memory/.curation_cursor.json) and collects new
   knowledge/strategy T3 entries since the last run.
2. Below the batch threshold → skip (cheap no-op).
3. Builds the tenant's summary-model LLM caller and runs scene consolidation
   over the new atoms; a proposed patch is applied through the governed
   write (`apply_scene_patch`).
4. Picks the dominant concept among the new entries and runs wiki
   consolidation for it; proposed patches apply via `apply_wiki_patch`.
5. Advances the cursor. Held candidates stay visible in
   memory/distillation_audit.jsonl — a hold is a decision, not a retry loop.

All failures are contained: curation must never break the heartbeat tick.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.memory.scene_curator import LlmFn, apply_scene_patch, curate_scene
from app.memory.wiki_curator import apply_wiki_patch, curate_wiki_page

logger = logging.getLogger(__name__)

_CURSOR_FILENAME = ".curation_cursor.json"
_MIN_NEW_ENTRIES = 3
_MAX_BATCH = 8
_CURATED_FILES = {"knowledge.md", "strategies.md"}


def _cursor_path(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / _CURSOR_FILENAME


def _read_cursor(data_root: Path, agent_id: uuid.UUID) -> set[str]:
    path = _cursor_path(data_root, agent_id)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(v) for v in payload.get("processed_entry_ids") or [] if v}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[MemoryCuration] cursor read failed for %s: %s", agent_id, exc)
        return set()


def _write_cursor(data_root: Path, agent_id: uuid.UUID, processed: set[str]) -> None:
    path = _cursor_path(data_root, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_entry_ids": sorted(processed)[-2000:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _build_llm_caller(tenant_id: uuid.UUID | None) -> LlmFn | None:
    """Wrap the tenant summary model as the curators' injected LLM."""
    if tenant_id is None:
        return None
    try:
        from app.services.memory_service import _get_summary_model_config

        model_config = await _get_summary_model_config(tenant_id)
    except Exception as exc:  # noqa: BLE001 — model resolution failure degrades to hold
        logger.info("[MemoryCuration] summary model unavailable: %s", exc)
        return None
    if not model_config:
        return None

    async def caller(system: str, user: str) -> str:
        from app.memory.metrics import record_autonomous_llm_call
        from app.services.llm_client import LLMMessage, create_llm_client

        client = create_llm_client(**model_config)
        try:
            response = await client.stream(
                messages=[LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
                max_tokens=4000,
                temperature=0.2,
            )
            record_autonomous_llm_call(source="memory_curation", outcome="success")
            return response.content or ""
        except Exception:
            record_autonomous_llm_call(source="memory_curation", outcome="failure")
            raise
        finally:
            await client.close()

    return caller


async def run_scene_wiki_curation_tick(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    *,
    data_root: Path | None = None,
) -> dict:
    """One curation pass. Returns a summary dict; never raises."""
    try:
        if data_root is None:
            from app.config import get_settings

            data_root = Path(get_settings().AGENT_DATA_DIR)

        from app.memory.md_store import build_t3_entry_manifest

        cursor = _read_cursor(data_root, agent_id)
        new_entries = [
            entry
            for entry in build_t3_entry_manifest(data_root, agent_id)
            if entry.filename in _CURATED_FILES and entry.entry_id not in cursor
        ]
        if len(new_entries) < _MIN_NEW_ENTRIES:
            return {"status": "skipped", "new_entries": len(new_entries)}
        batch = new_entries[:_MAX_BATCH]

        llm = await _build_llm_caller(tenant_id)
        atoms = [{"content": entry.content, "source_ref": f"t3:{entry.source}#{entry.entry_id}"} for entry in batch]

        scene_candidate = await curate_scene(data_root, agent_id, atoms=atoms, llm=llm)
        scene_result = {"status": scene_candidate.status, "reason": scene_candidate.reason}
        if scene_candidate.status == "proposed":
            scene_result["apply"] = apply_scene_patch(data_root, agent_id, scene_candidate)

        wiki_result: dict = {"status": "skipped"}
        concepts = Counter(
            (entry.metadata.get("concept") or "").strip().lower() for entry in batch if entry.metadata.get("concept")
        )
        if concepts:
            concept, _count = concepts.most_common(1)[0]
            evidence = [
                {"content": entry.content, "source_ref": f"t3:{entry.source}#{entry.entry_id}"}
                for entry in batch
                if (entry.metadata.get("concept") or "").strip().lower() == concept
            ]
            wiki_candidate = await curate_wiki_page(
                data_root, agent_id, concept=concept, evidence_entries=evidence, llm=llm
            )
            wiki_result = {"status": wiki_candidate.status, "reason": wiki_candidate.reason, "concept": concept}
            if wiki_candidate.status == "proposed":
                wiki_result["apply"] = apply_wiki_patch(data_root, agent_id, wiki_candidate)

        cursor.update(entry.entry_id for entry in batch)
        _write_cursor(data_root, agent_id, cursor)

        return {
            "status": "ran",
            "batch": len(batch),
            "scene": scene_result,
            "wiki": wiki_result,
        }
    except Exception as exc:  # noqa: BLE001 — curation must never break the heartbeat tick
        logger.warning("[MemoryCuration] curation tick failed for %s: %s", agent_id, exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
