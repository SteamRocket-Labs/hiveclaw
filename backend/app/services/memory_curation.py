"""Deprecated scene/wiki curation runtime.

The accepted T3 path has been narrowed to four MD files plus the
T3 Consolidator -> Memory Gate -> Platform Gate lane. The older scene/wiki
curators remain importable for compatibility tests and future migration work,
but the live heartbeat tick no longer builds a second memory system from T3.
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
_CURATED_FILES: set[str] = set()
_RETRYABLE_HOLD_MARKERS = (
    "no llm",
    "llm output invalid",
    "missing frontmatter",
    "missing required sections",
)


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


def _is_retryable_hold(status: str, reason: str) -> bool:
    if status != "held":
        return False
    normalized = (reason or "").lower()
    return any(marker in normalized for marker in _RETRYABLE_HOLD_MARKERS)


async def _build_llm_caller(tenant_id: uuid.UUID | None, agent_id: uuid.UUID | None = None) -> LlmFn | None:
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
        from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context

        client = create_llm_client_from_config(
            with_llm_usage_context(
                model_config,
                source="memory_curation",
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
        )
        try:
            response = await client.stream(
                messages=[LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
                max_tokens=8192,  # CC-standard auxiliary-call floor
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
        return {"status": "disabled", "reason": "t3_consolidation_lane_is_canonical", "new_entries": 0}

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

        llm = await _build_llm_caller(tenant_id, agent_id=agent_id)
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

        retryable_hold = _is_retryable_hold(scene_result["status"], scene_result.get("reason", "")) or (
            _is_retryable_hold(wiki_result.get("status", ""), wiki_result.get("reason", ""))
        )
        if not retryable_hold:
            cursor.update(entry.entry_id for entry in batch)
            _write_cursor(data_root, agent_id, cursor)

        return {
            "status": "ran",
            "batch": len(batch),
            "scene": scene_result,
            "wiki": wiki_result,
            "retryable_hold": retryable_hold,
        }
    except Exception as exc:  # noqa: BLE001 — curation must never break the heartbeat tick
        logger.warning("[MemoryCuration] curation tick failed for %s: %s", agent_id, exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
