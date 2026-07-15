"""Inventory, backfill, quarantine, restore, and sweep Agent storage safely.

Examples:
    python -m app.scripts.storage_lifecycle inventory --json
    python -m app.scripts.storage_lifecycle backfill --dry-run --json
    python -m app.scripts.storage_lifecycle backfill --apply --manifest <path> \
        --confirm --manifest-sha256 <sha256>
    python -m app.scripts.storage_lifecycle gc --dry-run --json
    python -m app.scripts.storage_lifecycle gc --apply --manifest <path> \
        --confirm --manifest-sha256 <sha256>
    python -m app.scripts.storage_lifecycle restore --apply --run-id <run_id> --confirm
    python -m app.scripts.storage_lifecycle sweep --dry-run --json
    python -m app.scripts.storage_lifecycle sweep --apply --manifest <path> \
        --confirm --manifest-sha256 <sha256>
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.models.agent import Agent
from app.services import agent_asset_transaction as asset_tx
from app.services.storage_lifecycle import (
    apply_transaction_backfill,
    apply_transaction_quarantine,
    apply_transaction_sweep,
    build_transaction_backfill_manifest,
    build_transaction_gc_manifest,
    build_transaction_sweep_manifest,
    inventory_storage,
    restore_transaction_quarantine,
)


async def _authoritative_agent_tenants() -> dict[str, str]:
    async with async_session() as db:
        async with enter_rls_bypass(
            db,
            reason="storage lifecycle fleet Agent tenant authority inventory",
        ) as bypass_db:
            rows = (await bypass_db.execute(select(Agent.id, Agent.tenant_id).order_by(Agent.id))).all()
    return {str(agent_id): str(tenant_id) for agent_id, tenant_id in rows}


def _load_manifest(path: str | None) -> dict[str, Any]:
    if not path:
        raise SystemExit("--manifest is required with --apply")
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"manifest must be a JSON object: {manifest_path}")
    return payload


def _require_apply_confirmation(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise SystemExit("HIGH-RISK OPERATION: --apply requires --confirm")
    if not str(args.manifest_sha256 or "").strip():
        raise SystemExit("HIGH-RISK OPERATION: --apply requires --manifest-sha256")


def _persist_artifact(data_root: Path, payload: dict[str, Any], *, prefix: str) -> Path:
    run_id = str(payload.get("run_id") or payload.get("generated_at") or "inventory").replace(":", "-")
    path = data_root / ".storage_lifecycle" / "manifests" / f"{prefix}-{run_id}.json"
    asset_tx._atomic_write_json(path, payload)
    return path


def _render(payload: dict[str, Any], *, full_json: bool) -> str:
    if full_json:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    summary = {
        key: payload.get(key)
        for key in (
            "mode",
            "run_id",
            "manifest_sha256",
            "candidate_count",
            "candidate_bytes",
            "processed_count",
            "processed_bytes",
            "receipt_path",
            "artifact_path",
        )
        if key in payload
    }
    if "transactions" in payload:
        transactions = payload["transactions"]
        summary["transactions"] = {
            key: transactions.get(key) for key in ("count", "logical_bytes", "payload_bytes", "by_operation")
        }
    if "holds" in payload:
        summary["hold_count"] = len(payload.get("holds") or [])
    if "skipped" in payload:
        summary["skipped_count"] = len(payload.get("skipped") or [])
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    data_root = Path(args.data_root or get_settings().AGENT_DATA_DIR).expanduser().resolve()
    if args.command == "inventory":
        tenants = await _authoritative_agent_tenants()
        payload = inventory_storage(data_root, agent_tenants=tenants)
        artifact = _persist_artifact(data_root, payload, prefix="inventory")
        payload["artifact_path"] = str(artifact)
        return payload

    if args.command == "backfill":
        if args.apply:
            _require_apply_confirmation(args)
            manifest = _load_manifest(args.manifest)
            return apply_transaction_backfill(
                data_root,
                manifest,
                expected_manifest_sha256=args.manifest_sha256,
            )
        tenants = await _authoritative_agent_tenants()
        payload = build_transaction_backfill_manifest(data_root, agent_tenants=tenants)
        artifact = _persist_artifact(data_root, payload, prefix="backfill")
        payload["artifact_path"] = str(artifact)
        return payload

    if args.command == "gc":
        if args.apply:
            _require_apply_confirmation(args)
            manifest = _load_manifest(args.manifest)
            return apply_transaction_quarantine(
                data_root,
                manifest,
                expected_manifest_sha256=args.manifest_sha256,
                grace=timedelta(hours=max(0.0, args.grace_hours)),
            )
        payload = build_transaction_gc_manifest(data_root)
        artifact = _persist_artifact(data_root, payload, prefix="gc")
        payload["artifact_path"] = str(artifact)
        return payload

    if args.command == "restore":
        if not args.apply or not args.confirm:
            raise SystemExit("restore requires --apply --confirm")
        return restore_transaction_quarantine(data_root, run_id=args.run_id)

    if args.command == "sweep":
        if args.apply:
            _require_apply_confirmation(args)
            manifest = _load_manifest(args.manifest)
            return apply_transaction_sweep(
                data_root,
                manifest,
                expected_manifest_sha256=args.manifest_sha256,
            )
        payload = build_transaction_sweep_manifest(data_root)
        artifact = _persist_artifact(data_root, payload, prefix="sweep")
        payload["artifact_path"] = str(artifact)
        return payload

    raise SystemExit(f"unsupported command: {args.command}")


def _add_dry_run_apply_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Generate a zero-mutation manifest (default).")
    mode.add_argument("--apply", action="store_true", help="Apply an already reviewed immutable manifest.")
    parser.add_argument("--manifest", help="Reviewed manifest JSON path; required with --apply.")
    parser.add_argument("--manifest-sha256", help="Exact reviewed manifest hash; required with --apply.")
    parser.add_argument("--confirm", action="store_true", help="Explicit operator confirmation; required with --apply.")


def _add_trailing_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print the complete output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=None, help="Defaults to AGENT_DATA_DIR.")
    parser.add_argument("--json", action="store_true", help="Print the complete manifest/receipt.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Read-only inventory plus an immutable audit artifact.")
    _add_trailing_json_argument(inventory)
    backfill = subparsers.add_parser("backfill", help="Classify/finalize legacy transaction payloads.")
    _add_dry_run_apply_arguments(backfill)
    _add_trailing_json_argument(backfill)
    gc = subparsers.add_parser("gc", help="Mark or quarantine finalized payloads; never directly deletes.")
    _add_dry_run_apply_arguments(gc)
    gc.add_argument("--grace-hours", type=float, default=24.0, help="Quarantine grace before sweep eligibility.")
    _add_trailing_json_argument(gc)
    restore = subparsers.add_parser("restore", help="Restore one reversible quarantine run.")
    restore.add_argument("--run-id", required=True)
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--confirm", action="store_true")
    _add_trailing_json_argument(restore)
    sweep = subparsers.add_parser("sweep", help="Physically delete quarantined payload after grace.")
    _add_dry_run_apply_arguments(sweep)
    _add_trailing_json_argument(sweep)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = asyncio.run(_run(args))
    print(_render(payload, full_json=args.json))


if __name__ == "__main__":
    main()
