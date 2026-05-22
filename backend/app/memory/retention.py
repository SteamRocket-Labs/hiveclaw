"""Retention scoring for durable memories."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RetentionTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True, slots=True)
class RetentionInputs:
    salience: float
    created_at: datetime
    accessed_at: list[datetime] = field(default_factory=list)
    feedback_reinforce: float = 0.0
    lambda_decay: float = 0.01
    sigma_access: float = 0.3
    rho_feedback: float = 1.0


def compute_retention_score(inputs: RetentionInputs, *, now: datetime | None = None) -> float:
    current = _aware(now or datetime.now(UTC))
    created = _aware(inputs.created_at)
    age_days = max((current - created).total_seconds() / 86400.0, 0.0)
    salience = _clamp(inputs.salience)
    decayed = salience * math.exp(-inputs.lambda_decay * age_days)

    access_boost = 0.0
    for accessed in inputs.accessed_at:
        accessed_dt = _aware(accessed)
        days_since_access = max((current - accessed_dt).total_seconds() / 86400.0, 1.0)
        access_boost += 1.0 / days_since_access

    score = decayed + inputs.sigma_access * access_boost + inputs.rho_feedback * inputs.feedback_reinforce
    return round(_clamp(score), 4)


def retention_tier(score: float) -> RetentionTier:
    if score >= 0.8:
        return RetentionTier.HOT
    if score >= 0.3:
        return RetentionTier.WARM
    return RetentionTier.COLD


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))

