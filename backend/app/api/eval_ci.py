"""CI-only eval endpoints for the isolated Railway eval environment."""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from app.evals.hive_live_runner import DETERMINISTIC_BEHAVIOR_SCENARIOS
from app.services.eval_ci_service import run_production_behavior_eval_for_ci

router = APIRouter(prefix="/eval-ci", tags=["eval-ci"])


class BehaviorEvalRequest(BaseModel):
    scenarios: list[str] | None = None


def _configured_token() -> str:
    return os.environ.get("HIVE_EVAL_CI_TOKEN", "").strip()


def _require_eval_ci_token(authorization: str | None) -> None:
    expected = _configured_token()
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="eval ci endpoint disabled")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid eval ci token")


def _validate_scenarios(scenarios: list[str] | None) -> tuple[str, ...] | None:
    if not scenarios:
        return None
    allowed = set(DETERMINISTIC_BEHAVIOR_SCENARIOS)
    unknown = sorted(set(scenarios) - allowed)
    if unknown:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown eval scenarios: {unknown}")
    return tuple(scenarios)


@router.post("/behavior")
async def run_behavior_eval(
    payload: BehaviorEvalRequest | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_eval_ci_token(authorization)
    scenarios = _validate_scenarios(payload.scenarios if payload else None)
    try:
        return await run_production_behavior_eval_for_ci(scenarios=scenarios)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
