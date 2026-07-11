"""File-backed self-evolution ledger.

Every automatic prompt/skill/policy change should leave candidate, eval, and
promotion decision records before it becomes durable behavior.
Each candidate carries the thin `hive_evolution_manifest.v1` contract.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.evolution_manifest import EVOLUTION_MANIFEST_SCHEMA, build_evolution_manifest


def _ledger_path(workspace: Path) -> Path:
    path = workspace / "evolution" / "evolution_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(
    workspace: Path,
    payload: dict[str, Any],
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    if transaction is None:
        with AgentAssetTransaction(workspace, operation="evolution_ledger_append") as own_transaction:
            result = _append(workspace, payload, transaction=own_transaction)
            own_transaction.commit()
            return result
    payload = dict(payload)
    payload.setdefault("created_at", _now_iso())
    transaction.append_text(
        "evolution/evolution_ledger.jsonl",
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )
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
    candidate_id: str | None = None,
    manifest: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    candidate_id = candidate_id or _candidate_id(target_type, target_id, diff, source_attempt_ids)
    refs = [str(item).strip() for item in source_attempt_ids if str(item).strip()]
    candidate_manifest = manifest or build_evolution_manifest(
        change_type=target_type,
        target_type=target_type,
        target_id=target_id,
        source_refs=[f"runtime_task:{ref}" for ref in refs] or [f"candidate:{candidate_id}:source-missing"],
        trace_refs=[f"trace:{ref}" for ref in refs] or [f"candidate:{candidate_id}:trace-missing"],
        eval_refs=[],
        rollback_strategy=(
            f"restore {baseline_version}" if baseline_version else "restore previous baseline before applying candidate"
        ),
        metadata={"candidate_id": candidate_id, "manifest_schema": EVOLUTION_MANIFEST_SCHEMA},
    )
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
            "manifest": candidate_manifest,
            "metadata": metadata or {},
        },
        transaction=transaction,
    )


def record_memory_promotion_candidate(
    workspace: Path,
    *,
    target_type: str,
    target_id: str,
    proposed_diff: str,
    source_refs: list[str],
    evidence: str,
    novelty: float | int | None = None,
    reusability: float | int | None = None,
    volatility: str = "stable",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refs = [str(ref).strip() for ref in source_refs if str(ref).strip()]
    if not refs:
        raise ValueError("memory promotion candidate requires at least one source_ref")
    candidate_id = _candidate_id(target_type, target_id, proposed_diff, refs)
    candidate_manifest = build_evolution_manifest(
        change_type="memory",
        target_type=target_type,
        target_id=target_id,
        source_refs=refs,
        trace_refs=refs,
        eval_refs=[],
        rollback_strategy=f"restore previous {target_type} content before applying candidate",
        risk_level="high" if target_type == "memory:soul" else "medium",
        metadata={"candidate_id": candidate_id, "evidence": (evidence or "inferred").strip().lower()},
    )
    return _append(
        workspace,
        {
            "schema": "memory_promotion_candidate.v1",
            "event": "memory_promotion_candidate",
            "candidate_id": candidate_id,
            "target_type": target_type,
            "target_id": target_id,
            "source_refs": refs,
            "evidence": (evidence or "inferred").strip().lower(),
            "novelty": round(float(novelty), 2) if novelty is not None else None,
            "reusability": round(float(reusability), 2) if reusability is not None else None,
            "volatility": (volatility or "stable").strip().lower(),
            "diff_hash": hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest(),
            "diff_preview": proposed_diff[:1000],
            "manifest": candidate_manifest,
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


def decide_memory_promotion(candidate: dict[str, Any]) -> dict[str, str]:
    evidence = str(candidate.get("evidence") or "inferred").lower()
    volatility = str(candidate.get("volatility") or "stable").lower()
    target_type = str(candidate.get("target_type") or "")
    source_refs = candidate.get("source_refs") or []
    if evidence == "inferred":
        return {"decision": "hold", "reason": "inferred evidence cannot be promoted directly"}
    if volatility in {"ephemeral", "session"}:
        return {"decision": "hold", "reason": f"{volatility} memory is not durable enough to promote"}
    if target_type == "memory:soul" and evidence not in {"user_stated", "tool_verified", "system_observed"}:
        return {"decision": "hold", "reason": "soul promotion requires verified evidence"}
    if target_type == "memory:soul" and not source_refs:
        return {"decision": "hold", "reason": "soul promotion requires source_refs"}
    if target_type == "memory:soul" and evidence == "system_observed" and len(source_refs) < 2:
        return {"decision": "hold", "reason": "system_observed soul promotion requires multiple source_refs"}
    return {"decision": "promote", "reason": "memory promotion candidate passed evidence and volatility gates"}


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


def record_memory_promotion_decision(
    workspace: Path,
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    rollback_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if decision == "promote" and not rollback_ref:
        raise ValueError("promoted memory decisions require rollback_ref")
    return _append(
        workspace,
        {
            "schema": "memory_promotion_decision.v1",
            "event": "memory_promotion_decision",
            "candidate_id": candidate_id,
            "decision": decision,
            "reason": reason,
            "rollback_ref": rollback_ref,
            "metadata": metadata or {},
        },
    )


def record_rollback_event(
    workspace: Path,
    *,
    candidate_id: str,
    restored_ref: str,
    reason: str,
    operator: str = "system",
    metadata: dict[str, Any] | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    return _append(
        workspace,
        {
            "schema": "evolution_rollback_event.v1",
            "event": "rollback",
            "candidate_id": candidate_id,
            "restored_ref": restored_ref,
            "reason": reason,
            "operator": operator,
            "metadata": metadata or {},
        },
        transaction=transaction,
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
