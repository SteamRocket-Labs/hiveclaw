"""Deprecated scene/wiki curation runtime.

The accepted T3 path has been narrowed to four MD files plus the
T3 Consolidator -> Memory Gate -> Platform Gate lane. The older scene/wiki
curators remain importable for compatibility tests and future migration work,
but the live heartbeat tick no longer builds a second memory system from T3.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable

LlmFn = Callable[[str, str], Awaitable[str]]

logger = logging.getLogger(__name__)


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
    """Compatibility no-op; the canonical live path is T3 consolidation."""
    del agent_id, tenant_id, data_root
    return {"status": "disabled", "reason": "t3_consolidation_lane_is_canonical", "new_entries": 0}
