"""File-backed self-evolution ledger.

Every automatic prompt/skill/policy change should leave candidate, eval, and
promotion decision records before it becomes durable behavior.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ledger_path(workspace: Path) -> Path:
    path = workspace / "evolution" / "evolution_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("created_at", _now_iso())
    with _ledger_path(workspace).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def _candidate_id(target_type: str, target_id: str, diff: str, source_attempt_ids: list[str]) -> str:
    raw = json.dumps(
        {
            "target_type": target_type,
            "target_id": target_id,
            "diff": diff,
            "source_attempt_ids": source_attempt_ids,
            "nonce": uuid.uuid4().hex,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_evolution_candidate(
    workspace: Path,
    *,
    target_type: str,
    target_id: str,
    diff: str,
    source_attempt_ids: list[str],
    baseline_version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_id = _candidate_id(target_type, target_id, diff, source_attempt_ids)
    return _append(
        workspace,
        {
            "schema": "evolution_candidate.v1",
            "event": "candidate",
            "candidate_id": candidate_id,
            "target_type": target_type,
            "target_id": target_id,
            "baseline_version": baseline_version,
            "source_attempt_ids": source_attempt_ids,
            "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "diff_preview": diff[:1000],
            "metadata": metadata or {},
        },
    )


def record_eval_run(
    workspace: Path,
    *,
    candidate_id: str,
    dataset: str,
    reward: float,
    baseline_reward: float,
    passed: bool,
    traces: list[str],
    critical_regressions: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _append(
        workspace,
        {
            "schema": "evolution_eval_run.v1",
            "event": "eval_run",
            "candidate_id": candidate_id,
            "dataset": dataset,
            "reward": reward,
            "baseline_reward": baseline_reward,
            "passed": passed,
            "critical_regressions": critical_regressions,
            "traces": traces,
            "metadata": metadata or {},
        },
    )


def decide_promotion(eval_run: dict[str, Any], *, min_reward_delta: float = 0.0) -> dict[str, str]:
    if int(eval_run.get("critical_regressions") or 0) > 0:
        return {"decision": "hold", "reason": "blocked by critical regression"}
    if not bool(eval_run.get("passed")):
        return {"decision": "hold", "reason": "eval did not pass"}
    reward = float(eval_run.get("reward") or 0.0)
    baseline_reward = float(eval_run.get("baseline_reward") or 0.0)
    if reward - baseline_reward < min_reward_delta:
        return {"decision": "hold", "reason": "reward delta below promotion threshold"}
    return {"decision": "promote", "reason": "reward beat baseline with no critical regressions"}


def record_promotion_decision(
    workspace: Path,
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    rollback_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _append(
        workspace,
        {
            "schema": "evolution_promotion_decision.v1",
            "event": "promotion_decision",
            "candidate_id": candidate_id,
            "decision": decision,
            "reason": reason,
            "rollback_ref": rollback_ref,
            "metadata": metadata or {},
        },
    )


def load_evolution_ledger(workspace: Path) -> list[dict[str, Any]]:
    path = _ledger_path(workspace)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries
