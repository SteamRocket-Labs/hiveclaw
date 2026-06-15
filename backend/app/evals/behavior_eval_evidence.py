"""Behavior eval evidence helpers for CI fail-closed reporting."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BEHAVIOR_EVAL_EVIDENCE_SCHEMA = "behavior_eval_evidence.v1"
_PASSING_STATUSES = {"passed", "ready"}
_NONPASSING_STATUSES = {"blocked", "failed", "skipped"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def build_behavior_eval_evidence(
    *,
    status: str,
    reason: str,
    missing: list[str] | tuple[str, ...] | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _clean(status).lower()
    if normalized not in _PASSING_STATUSES | _NONPASSING_STATUSES:
        raise ValueError(f"unsupported behavior eval evidence status: {status!r}")
    return {
        "schema": BEHAVIOR_EVAL_EVIDENCE_SCHEMA,
        "status": normalized,
        "passed": normalized in _PASSING_STATUSES,
        "reason": _clean(reason),
        "missing": [item for item in (missing or []) if _clean(item)],
        "report": report or {},
        "created_at": _now_iso(),
    }


def build_missing_secret_evidence(*, api_url: str | None, token: str | None) -> dict[str, Any]:
    missing: list[str] = []
    if not _clean(api_url):
        missing.append("HIVE_EVAL_API_URL")
    if not _clean(token):
        missing.append("HIVE_EVAL_CI_TOKEN")
    if missing:
        return build_behavior_eval_evidence(
            status="blocked",
            reason="live behavior eval secrets are required; the gate is fail-closed, not skipped",
            missing=missing,
        )
    return build_behavior_eval_evidence(
        status="ready",
        reason="live behavior eval secrets are configured",
    )


def write_behavior_eval_evidence(evidence: dict[str, Any], output: str | Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _exit_code_for(evidence: dict[str, Any]) -> int:
    status = _clean(evidence.get("status")).lower()
    if status in {"passed", "ready"}:
        return 0
    if status == "blocked":
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write behavior eval evidence for CI gates.")
    parser.add_argument("--api-url", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.status:
        evidence = build_behavior_eval_evidence(status=args.status, reason=args.reason)
    else:
        evidence = build_missing_secret_evidence(api_url=args.api_url, token=args.token)
    write_behavior_eval_evidence(evidence, args.output)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return _exit_code_for(evidence)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
