"""Decision trace and feedback-link store."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DECISION_REF_RE = re.compile(r"\bdecision/(?P<id>[A-Za-z0-9_.:-]+)\b")
_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    id: str
    action: str
    chosen: str
    reasoning: str
    alternatives_considered: list[str]
    situational_factors: list[str]
    charter_zone: str
    preflight: dict[str, str]
    sensitivity: str
    tenant_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    tool_name: str | None = None
    checkpoint_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FeedbackSignal:
    id: str
    refs: str
    reaction: str
    polarity: str
    source: str
    rationale_from_owner: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _stable_uuid(value: str, *, namespace: str) -> uuid.UUID:
    parsed = _uuid_or_none(value)
    if parsed is not None:
        return parsed
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{value}")


def _decision_payload(decision: DecisionTrace) -> dict[str, Any]:
    return {
        "id": decision.id,
        "action": decision.action,
        "chosen": decision.chosen,
        "reasoning": decision.reasoning,
        "alternatives_considered": decision.alternatives_considered,
        "situational_factors": decision.situational_factors,
        "charter_zone": decision.charter_zone,
        "preflight": decision.preflight,
        "sensitivity": decision.sensitivity,
        "tenant_id": decision.tenant_id,
        "agent_id": decision.agent_id,
        "user_id": decision.user_id,
        "session_id": decision.session_id,
        "message_id": decision.message_id,
        "tool_name": decision.tool_name,
        "checkpoint_id": decision.checkpoint_id,
        "created_at": decision.created_at.isoformat(),
    }


def _decision_from_record(row: Any) -> DecisionTrace:
    return DecisionTrace(
        id=str(row.decision_id),
        action=str(row.action),
        chosen=str(row.chosen),
        reasoning=str(row.reasoning),
        alternatives_considered=[str(item) for item in (row.alternatives_json or [])],
        situational_factors=[str(item) for item in (row.situational_factors_json or [])],
        charter_zone=str(row.charter_zone),
        preflight={str(k): str(v) for k, v in (row.preflight_json or {}).items()},
        sensitivity=str(row.sensitivity),
        tenant_id=_optional_str(row.tenant_id),
        agent_id=_optional_str(row.agent_id),
        user_id=_optional_str(row.user_id),
        session_id=_optional_str(row.session_id),
        message_id=_optional_str(row.message_id),
        tool_name=_optional_str(row.tool_name),
        checkpoint_id=_optional_str(row.checkpoint_id),
        created_at=row.created_at or datetime.now(UTC),
    )


def _feedback_from_record(row: Any) -> FeedbackSignal:
    return FeedbackSignal(
        id=str(row.id),
        refs=str(row.refs),
        reaction=str(row.reaction),
        polarity=str(row.polarity),
        source=str(row.source),
        rationale_from_owner=str(row.rationale_from_owner or ""),
        created_at=row.created_at or datetime.now(UTC),
    )


def _feedback_payload(feedback: FeedbackSignal) -> dict[str, Any]:
    return {
        "id": feedback.id,
        "refs": feedback.refs,
        "reaction": feedback.reaction,
        "polarity": feedback.polarity,
        "source": feedback.source,
        "rationale_from_owner": feedback.rationale_from_owner,
        "created_at": feedback.created_at.isoformat(),
    }


class SqlDecisionTraceStore:
    """Async SQL-backed decision trace store; its caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._decisions: dict[str, DecisionTrace] = {}

    async def record_decision(
        self,
        *,
        action: str,
        chosen: str,
        reasoning: str,
        alternatives_considered: list[str],
        situational_factors: list[str],
        charter_zone: str,
        preflight: dict[str, str],
        sensitivity: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        tool_name: str | None = None,
        checkpoint_id: str | None = None,
    ) -> DecisionTrace:
        from app.models.decision_trace import DecisionTraceRecord

        decision = DecisionTrace(
            id=str(uuid.uuid4()),
            action=action,
            chosen=chosen,
            reasoning=reasoning,
            alternatives_considered=alternatives_considered,
            situational_factors=situational_factors,
            charter_zone=charter_zone,
            preflight=preflight,
            sensitivity=sensitivity,
            tenant_id=_optional_str(tenant_id),
            agent_id=_optional_str(agent_id),
            user_id=_optional_str(user_id),
            session_id=_optional_str(session_id),
            message_id=_optional_str(message_id),
            tool_name=_optional_str(tool_name),
            checkpoint_id=_optional_str(checkpoint_id),
        )
        row = DecisionTraceRecord(
            id=uuid.uuid4(),
            decision_id=decision.id,
            tenant_id=_uuid_or_none(tenant_id),
            agent_id=_uuid_or_none(agent_id),
            user_id=_uuid_or_none(user_id),
            session_id=decision.session_id,
            message_id=decision.message_id,
            tool_name=decision.tool_name,
            checkpoint_id=decision.checkpoint_id,
            action=decision.action,
            chosen=decision.chosen,
            reasoning=decision.reasoning,
            alternatives_json=list(decision.alternatives_considered),
            situational_factors_json=list(decision.situational_factors),
            charter_zone=decision.charter_zone,
            preflight_json=dict(decision.preflight),
            sensitivity=decision.sensitivity,
            payload_json=_decision_payload(decision),
            created_at=decision.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        self._decisions[decision.id] = decision
        return decision

    async def record_feedback(
        self,
        *,
        decision_id: str,
        reaction: str,
        polarity: str,
        source: str,
        rationale_from_owner: str = "",
    ) -> FeedbackSignal:
        from app.models.decision_trace import DecisionTraceFeedbackRecord, DecisionTraceRecord

        normalized_id = decision_id_from_ref(decision_id)
        decision = self._decisions.get(normalized_id)
        tenant_id: uuid.UUID | None = _uuid_or_none(decision.tenant_id) if decision else None
        if decision is None:
            result = await self._session.execute(
                select(DecisionTraceRecord).where(DecisionTraceRecord.decision_id == normalized_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(normalized_id)
            decision = _decision_from_record(row)
            tenant_id = row.tenant_id
            self._decisions[normalized_id] = decision
        feedback = FeedbackSignal(
            id=str(uuid.uuid4()),
            refs=normalize_decision_ref(normalized_id),
            reaction=reaction,
            polarity=polarity,
            source=source,
            rationale_from_owner=rationale_from_owner,
        )
        row = DecisionTraceFeedbackRecord(
            id=uuid.UUID(feedback.id),
            decision_id=normalized_id,
            tenant_id=tenant_id,
            refs=feedback.refs,
            reaction=feedback.reaction,
            polarity=feedback.polarity,
            source=feedback.source,
            rationale_from_owner=feedback.rationale_from_owner,
            payload_json=_feedback_payload(feedback),
            created_at=feedback.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return feedback

    async def import_decision(self, decision: DecisionTrace) -> DecisionTrace:
        """Import a legacy JSONL decision while preserving its public decision id."""
        from app.models.decision_trace import DecisionTraceRecord

        normalized_id = decision_id_from_ref(decision.id)
        result = await self._session.execute(
            select(DecisionTraceRecord).where(DecisionTraceRecord.decision_id == normalized_id)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            imported = _decision_from_record(existing)
            self._decisions[imported.id] = imported
            return imported

        row = DecisionTraceRecord(
            id=uuid.uuid4(),
            decision_id=normalized_id,
            tenant_id=_uuid_or_none(decision.tenant_id),
            agent_id=_uuid_or_none(decision.agent_id),
            user_id=_uuid_or_none(decision.user_id),
            session_id=decision.session_id,
            message_id=decision.message_id,
            tool_name=decision.tool_name,
            checkpoint_id=decision.checkpoint_id,
            action=decision.action,
            chosen=decision.chosen,
            reasoning=decision.reasoning,
            alternatives_json=list(decision.alternatives_considered),
            situational_factors_json=list(decision.situational_factors),
            charter_zone=decision.charter_zone,
            preflight_json=dict(decision.preflight),
            sensitivity=decision.sensitivity,
            payload_json=_decision_payload(decision),
            created_at=decision.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        self._decisions[decision.id] = decision
        return decision

    async def import_feedback(self, feedback: FeedbackSignal) -> FeedbackSignal:
        """Import legacy JSONL feedback while keeping its id and decision ref stable."""
        from app.models.decision_trace import DecisionTraceFeedbackRecord, DecisionTraceRecord

        feedback_id = _stable_uuid(feedback.id, namespace="decision_trace_feedback")
        result = await self._session.execute(
            select(DecisionTraceFeedbackRecord).where(DecisionTraceFeedbackRecord.id == feedback_id)
        )
        existing_feedback = result.scalar_one_or_none()
        if existing_feedback is not None:
            return _feedback_from_record(existing_feedback)

        normalized_decision_id = decision_id_from_ref(feedback.refs)
        decision_result = await self._session.execute(
            select(DecisionTraceRecord).where(DecisionTraceRecord.decision_id == normalized_decision_id)
        )
        decision_row = decision_result.scalar_one_or_none()
        if decision_row is None:
            raise KeyError(normalized_decision_id)

        row = DecisionTraceFeedbackRecord(
            id=feedback_id,
            decision_id=normalized_decision_id,
            tenant_id=decision_row.tenant_id,
            refs=normalize_decision_ref(normalized_decision_id),
            reaction=feedback.reaction,
            polarity=feedback.polarity,
            source=feedback.source,
            rationale_from_owner=feedback.rationale_from_owner,
            payload_json=_feedback_payload(feedback),
            created_at=feedback.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return feedback

    async def get_decision(self, decision_id: str) -> DecisionTrace:
        from app.models.decision_trace import DecisionTraceRecord

        normalized_id = decision_id_from_ref(decision_id)
        result = await self._session.execute(
            select(DecisionTraceRecord).where(DecisionTraceRecord.decision_id == normalized_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(normalized_id)
        return _decision_from_record(row)

    async def feedback_for_decision(self, decision_id: str) -> list[FeedbackSignal]:
        from app.models.decision_trace import DecisionTraceFeedbackRecord

        normalized_id = decision_id_from_ref(decision_id)
        result = await self._session.execute(
            select(DecisionTraceFeedbackRecord).where(DecisionTraceFeedbackRecord.decision_id == normalized_id)
        )
        return [_feedback_from_record(row) for row in result.scalars().all()]


async def list_session_decision_traces(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the product-facing decision ledger for one authorized session."""

    from app.models.decision_trace import DecisionTraceFeedbackRecord, DecisionTraceRecord

    bounded_limit = max(1, min(int(limit), 100))
    result = await db.execute(
        select(DecisionTraceRecord)
        .where(
            DecisionTraceRecord.tenant_id == tenant_id,
            DecisionTraceRecord.agent_id == agent_id,
            DecisionTraceRecord.session_id == str(session_id),
        )
        .order_by(DecisionTraceRecord.created_at.desc(), DecisionTraceRecord.id.desc())
        .limit(bounded_limit)
    )
    rows = list(result.scalars().all())
    decision_ids = [str(row.decision_id) for row in rows]
    feedback_counts: dict[str, int] = {}
    if decision_ids:
        count_result = await db.execute(
            select(
                DecisionTraceFeedbackRecord.decision_id,
                func.count(DecisionTraceFeedbackRecord.id),
            )
            .where(
                DecisionTraceFeedbackRecord.tenant_id == tenant_id,
                DecisionTraceFeedbackRecord.decision_id.in_(decision_ids),
            )
            .group_by(DecisionTraceFeedbackRecord.decision_id)
        )
        feedback_counts = {str(decision_id): int(count) for decision_id, count in count_result.all()}

    return [
        {
            "id": str(row.decision_id),
            "action": str(row.action),
            "tool_name": _optional_str(row.tool_name),
            "outcome": str(row.chosen),
            "reason_codes": [str(item) for item in (row.situational_factors_json or [])],
            "created_at": (row.created_at or datetime.now(UTC)).isoformat(),
            "feedback_count": feedback_counts.get(str(row.decision_id), 0),
        }
        for row in rows
    ]


class TenantScopedSqlDecisionTraceStore:
    """Production store that opens a tenant-scoped session per append."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    async def _tenant_for_decision(self, decision_id: str) -> uuid.UUID | None:
        from app.database import async_session, enter_rls_bypass
        from app.models.decision_trace import DecisionTraceRecord

        factory = self._session_factory or async_session
        normalized_id = decision_id_from_ref(decision_id)
        async with factory() as session:
            async with enter_rls_bypass(session, reason=f"decision feedback tenant resolution for {normalized_id}"):
                return (
                    await session.execute(
                        select(DecisionTraceRecord.tenant_id).where(DecisionTraceRecord.decision_id == normalized_id)
                    )
                ).scalar_one_or_none()

    async def record_decision(self, **kwargs: Any) -> DecisionTrace:
        from app.database import tenant_scoped_session

        tenant_id = kwargs.get("tenant_id")
        async with tenant_scoped_session(tenant_id, session_factory=self._session_factory) as session:
            return await SqlDecisionTraceStore(session).record_decision(**kwargs)

    async def record_feedback(self, **kwargs: Any) -> FeedbackSignal:
        from app.database import tenant_scoped_session

        tenant_id = await self._tenant_for_decision(str(kwargs.get("decision_id") or ""))
        async with tenant_scoped_session(tenant_id, session_factory=self._session_factory) as session:
            return await SqlDecisionTraceStore(session).record_feedback(**kwargs)


def normalize_decision_ref(value: str) -> str:
    decision_id = decision_id_from_ref(value)
    if not decision_id:
        raise ValueError("decision id is required")
    return f"decision/{decision_id}"


def decision_id_from_ref(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("decision/"):
        raw = raw.removeprefix("decision/").strip()
    if _DECISION_ID_RE.fullmatch(raw) is None:
        raise ValueError("invalid decision id")
    return raw


def extract_decision_id_from_text(text: str) -> str | None:
    match = _DECISION_REF_RE.search(str(text or ""))
    if not match:
        return None
    return decision_id_from_ref(match.group("id"))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
