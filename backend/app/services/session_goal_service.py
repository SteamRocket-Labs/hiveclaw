"""Session Goal persistence for agent tools (A9/A1/A5).

Goal Mode is a session-local, Codex-inspired control loop. ``goal_start`` /
``update_goal`` / ``get_goal`` are agent tools that must durably persist through
the same governed session-goal storage the user-facing ``/goal`` command uses, so
a goal declared or completed by the model is tied to user/session permissions and
survives restarts. These helpers open their own tenant-scoped session (resolved
from the agent) rather than depending on an ambient request transaction.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.runtime.decision_ledger import build_agent_cycle_decision_entry
from app.services.tenant_resolver import resolve_tenant_for_agent

logger = logging.getLogger(__name__)

# Goal statuses that end the autonomous loop and release the pinned
# attention-set seeds (M7).
_ATTENTION_CLEARING_STATUSES = {"complete", "blocked", "cancelled"}


def _resolve_attention_refs(agent_id: uuid.UUID, declared: list[str], *, data_root: Path) -> list[str]:
    """Map declared topics onto knowledge page ids where a page exists.

    Graph-resident refs get PPR diffusion; unresolved topics stay literal
    (self-boost only). The declaration itself comes from the model.
    """
    from app.memory.relation_graph import KNOWLEDGE_PAGE_DIRS, slugify_title

    refs: list[str] = []
    for topic in declared:
        text = str(topic or "").strip()
        if not text:
            continue
        slug = slugify_title(text)
        resolved = text
        for page_dir in KNOWLEDGE_PAGE_DIRS:
            if (data_root / str(agent_id) / "memory" / page_dir / f"{slug}.md").exists():
                resolved = f"{page_dir}/{slug}"
                break
        refs.append(resolved)
    return refs


def _sync_goal_attention_seeds(
    agent_id: uuid.UUID,
    session_uuid: uuid.UUID,
    *,
    attention_set: list[str] | None = None,
    clear: bool = False,
) -> None:
    """Pin/clear the goal's attention-set seeds in the session working set (M7).

    Fail-open: attention seeding is a recall enhancement, never a reason for a
    goal write to fail.
    """
    if not attention_set and not clear:
        return
    try:
        from app.memory.session_working_set import (
            clear_pinned_items,
            load_working_set,
            pin_attention_set,
            save_working_set,
        )

        data_root = Path(get_settings().AGENT_DATA_DIR)
        state = load_working_set(data_root, agent_id, str(session_uuid))
        if clear:
            state = clear_pinned_items(state)
        if attention_set:
            state = pin_attention_set(
                state, _resolve_attention_refs(agent_id, attention_set, data_root=data_root)
            )
        save_working_set(data_root, agent_id, str(session_uuid), state)
    except (OSError, ValueError) as exc:
        logger.warning("[session_goal] attention seed sync failed for %s: %s", session_uuid, exc)

# Statuses the model may set through the update_goal tool. Hive-native terminal
# states (usage_limited/budget_limited/cancelled) are set by the runtime loop or
# the user command surface, not by the goal-owning agent mid-turn.
_TOOL_UPDATABLE_STATUSES = ("active", "paused", "blocked", "complete")

_LEDGER_LIMIT = 100


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _remaining_tokens(goal: AgentSessionGoal) -> int | None:
    if goal.token_budget is None:
        return None
    return max(0, int(goal.token_budget) - max(0, int(goal.tokens_used or 0)))


def _goal_state(goal: AgentSessionGoal) -> dict[str, Any]:
    return {
        "goal_id": str(goal.id),
        "objective": goal.objective,
        "status": goal.status,
        "token_budget": goal.token_budget,
        "tokens_used": int(goal.tokens_used or 0),
        "remaining_tokens": _remaining_tokens(goal),
        "max_continuation_turns": goal.max_continuation_turns,
        "continuation_count": int(goal.continuation_count or 0),
        "blocked_count": int(goal.blocked_count or 0),
        "completion_summary": goal.completion_summary,
    }


def _append_decision_entry(metadata: dict[str, Any], entry: dict[str, Any]) -> None:
    ledger = [dict(item) for item in metadata.get("goal_decision_ledger", []) if isinstance(item, dict)]
    ledger.append(dict(entry))
    if len(ledger) > _LEDGER_LIMIT:
        del ledger[: len(ledger) - _LEDGER_LIMIT]
    metadata["goal_decision_ledger"] = ledger
    metadata["last_goal_decision_entry"] = dict(entry)


async def _load_chat_session(db: Any, *, agent_id: uuid.UUID, session_uuid: uuid.UUID) -> ChatSession | None:
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_uuid, ChatSession.agent_id == agent_id).limit(1)
    )
    return result.scalar_one_or_none()


async def _load_active_goal(db: Any, *, agent_id: uuid.UUID, session_uuid: uuid.UUID) -> AgentSessionGoal | None:
    result = await db.execute(
        select(AgentSessionGoal)
        .where(
            AgentSessionGoal.agent_id == agent_id,
            AgentSessionGoal.chat_session_id == session_uuid,
            AgentSessionGoal.status == "active",
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _load_goal_for_update(db: Any, *, agent_id: uuid.UUID, session_uuid: uuid.UUID) -> AgentSessionGoal | None:
    goal = await _load_active_goal(db, agent_id=agent_id, session_uuid=session_uuid)
    if goal is not None:
        return goal
    result = await db.execute(
        select(AgentSessionGoal)
        .where(AgentSessionGoal.agent_id == agent_id, AgentSessionGoal.chat_session_id == session_uuid)
        .order_by(AgentSessionGoal.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def persist_session_goal_from_tool(
    *,
    agent_id: uuid.UUID,
    session_id: Any,
    user_id: uuid.UUID | None,
    objective: str,
    token_budget: int | None = None,
    max_continuation_turns: int | None = None,
    attention_set: list[str] | None = None,
) -> dict[str, Any]:
    """A9: durably create a session goal from the ``goal_start`` agent tool.

    Any pre-existing active goal for this session is superseded (retired) so the
    partial unique index on active goals stays satisfied, mirroring Codex
    single-active-goal-per-thread ``set`` semantics.
    """
    objective = str(objective or "").strip()
    if not objective:
        return {"ok": False, "error": "objective is required."}
    session_uuid = _as_uuid(session_id)
    if session_uuid is None:
        return {"ok": False, "error": "a valid session is required to start a goal."}

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        session = await _load_chat_session(db, agent_id=agent_id, session_uuid=session_uuid)
        if session is None:
            return {"ok": False, "error": "chat session not found for this agent."}

        superseded_goal_id: str | None = None
        existing = await _load_active_goal(db, agent_id=agent_id, session_uuid=session_uuid)
        if existing is not None:
            existing.status = "cancelled"
            existing_metadata = dict(existing.metadata_json or {})
            existing_metadata["superseded"] = True
            existing_metadata["superseded_reason"] = "replaced_by_new_goal_start"
            existing.metadata_json = existing_metadata
            superseded_goal_id = str(existing.id)
            # Release the partial unique index before inserting the new active goal.
            await db.flush()

        goal = AgentSessionGoal(
            tenant_id=tenant_id,
            agent_id=agent_id,
            chat_session_id=session_uuid,
            created_by_user_id=user_id,
            objective=objective,
            token_budget=token_budget,
            max_continuation_turns=max_continuation_turns,
            metadata_json={
                "source": "goal_start_tool",
                "session_id": str(session_uuid),
                **({"supersedes": superseded_goal_id} if superseded_goal_id else {}),
            },
        )
        db.add(goal)
        await db.flush()
        payload = {"ok": True, "superseded_goal_id": superseded_goal_id, **_goal_state(goal)}
        await db.commit()
    # M7: the model's own attention-set declaration becomes pinned W_t seeds.
    _sync_goal_attention_seeds(agent_id, session_uuid, attention_set=list(attention_set or []) or None)
    return payload


async def update_session_goal_from_tool(
    *,
    agent_id: uuid.UUID,
    session_id: Any,
    status: str | None = None,
    objective: str | None = None,
    summary: str | None = None,
    attention_set: list[str] | None = None,
) -> dict[str, Any]:
    """A1: model self-reported completion / blocked / re-scope write-back."""
    session_uuid = _as_uuid(session_id)
    if session_uuid is None:
        return {"ok": False, "error": "a valid session is required to update a goal."}

    status_value = str(status or "").strip().lower() or None
    objective_value = str(objective or "").strip() or None
    summary_value = str(summary or "").strip() or None
    if status_value is None and objective_value is None and summary_value is None:
        return {"ok": False, "error": "provide at least one of status, objective, or summary."}
    if status_value is not None and status_value not in _TOOL_UPDATABLE_STATUSES:
        return {
            "ok": False,
            "error": f"unsupported status {status_value!r}; use one of {', '.join(_TOOL_UPDATABLE_STATUSES)}.",
        }

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        goal = await _load_goal_for_update(db, agent_id=agent_id, session_uuid=session_uuid)
        if goal is None:
            return {"ok": False, "error": "no session goal found for this session."}

        metadata = dict(goal.metadata_json or {})
        previous_status = goal.status
        changes: list[str] = []

        if objective_value is not None and objective_value != goal.objective:
            goal.objective = objective_value
            # A7: re-orient the next continuation turn to the new objective.
            metadata["objective_updated_pending"] = True
            changes.append("objective")
        if summary_value is not None:
            goal.completion_summary = summary_value
            changes.append("summary")
        if status_value is not None:
            goal.status = status_value
            changes.append("status")
            if status_value == "complete":
                goal.completed_at = datetime.now(timezone.utc)

        metadata["last_update_source"] = "update_goal_tool"
        _append_decision_entry(
            metadata,
            build_agent_cycle_decision_entry(
                subsystem="goal",
                trigger="update_goal_tool",
                judge="session_goal.update_session_goal_from_tool",
                decision="update",
                outcome=goal.status,
                next_action="stop_goal" if goal.status in {"complete", "blocked"} else "continue_goal",
                model_interaction="agent_reported_goal_state",
                user_visible=True,
                details={
                    "changes": changes,
                    "status_transition": {"from": previous_status, "to": goal.status},
                    "summary": summary_value,
                },
            ),
        )
        goal.metadata_json = metadata
        await db.flush()
        payload = {"ok": True, "changes": changes, **_goal_state(goal)}
        await db.commit()
    # M7: terminal states release the pinned seeds; a fresh declaration (or a
    # re-scoped objective with one) supersedes the previous attention set.
    _sync_goal_attention_seeds(
        agent_id,
        session_uuid,
        attention_set=list(attention_set or []) or None,
        clear=status_value in _ATTENTION_CLEARING_STATUSES,
    )
    return payload


async def get_session_goal_from_tool(*, agent_id: uuid.UUID, session_id: Any) -> dict[str, Any]:
    """A5: read the active session goal state and remaining budget."""
    session_uuid = _as_uuid(session_id)
    if session_uuid is None:
        return {"ok": False, "error": "a valid session is required to read a goal."}

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        goal = await _load_goal_for_update(db, agent_id=agent_id, session_uuid=session_uuid)
        if goal is None:
            return {"ok": False, "error": "no session goal found for this session."}
        return {"ok": True, **_goal_state(goal)}
