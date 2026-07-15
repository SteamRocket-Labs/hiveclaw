"""Fleet repair for unsigned RecoveryManifest singleton artifacts.

The old files cannot prove tenant, principal, root run, policy, or transcript
authority. Repair therefore preserves their exact bytes in platform quarantine;
it never promotes their semantic content into a live session.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


_LEGACY_RELATIVE_PATHS = (
    Path("runtime_artifacts") / "recovery_manifest.json",
    Path("workspace") / "recovery_manifest.json",
)
_QUARANTINE_RELATIVE_DIR = Path("runtime_artifacts") / "recovery_manifests" / "quarantine"


@dataclass(frozen=True, slots=True)
class LegacyRecoveryRepairReport:
    mode: str
    scanned: int
    would_quarantine: int
    quarantined: int
    by_reason: dict[str, int]
    schema: str = "hive.recovery_manifest_legacy_repair.v1"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "scanned": self.scanned,
            "would_quarantine": self.would_quarantine,
            "quarantined": self.quarantined,
            "by_reason": dict(sorted(self.by_reason.items())),
        }


def _legacy_reason(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "corrupt_json"
    if not isinstance(payload, dict):
        return "legacy_authority_unverifiable"
    if isinstance(payload.get("body"), dict) and isinstance(payload.get("integrity"), dict):
        return "signed_legacy_requires_session_revalidation"
    return "legacy_authority_unverifiable"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quarantine_exact_bytes(*, path: Path, agent_root: Path, reason: str, raw: bytes) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    destination_dir = agent_root / _QUARANTINE_RELATIVE_DIR
    try:
        destination_dir.resolve(strict=False).relative_to(agent_root.resolve())
    except ValueError as exc:
        raise OSError("recovery quarantine destination escapes the agent root") from exc
    if destination_dir.is_symlink():
        raise OSError("recovery quarantine destination is a symlink")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{reason}-{digest}.json"
    if destination.exists():
        if destination.read_bytes() != raw:
            raise OSError("recovery quarantine digest collision")
        path.unlink(missing_ok=True)
    else:
        os.replace(path, destination)
        os.chmod(destination, 0o600)
    _fsync_directory(destination_dir)
    _fsync_directory(path.parent)


def _agent_roots(data_root: Path):
    if not data_root.exists():
        return
    for candidate in sorted(data_root.iterdir()):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        yield candidate


def quarantine_legacy_recovery_manifest(
    path: str | Path,
    *,
    agent_root: str | Path,
    apply: bool,
) -> str | None:
    """Classify and optionally quarantine one exact legacy path."""

    source = Path(path)
    root = Path(agent_root)
    if root.is_symlink() or source.is_symlink() or not source.is_file():
        return None
    try:
        source.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    raw = source.read_bytes()
    reason = _legacy_reason(raw)
    if apply:
        _quarantine_exact_bytes(
            path=source,
            agent_root=root,
            reason=reason,
            raw=raw,
        )
    return reason


def repair_legacy_recovery_manifests(
    data_root: str | Path,
    *,
    apply: bool = False,
) -> LegacyRecoveryRepairReport:
    """Scan or quarantine legacy singleton files without consuming their state."""

    root = Path(data_root)
    by_reason: Counter[str] = Counter()
    scanned = 0
    quarantined = 0
    for agent_root in _agent_roots(root):
        for relative_path in _LEGACY_RELATIVE_PATHS:
            path = agent_root / relative_path
            reason = quarantine_legacy_recovery_manifest(
                path,
                agent_root=agent_root,
                apply=apply,
            )
            if reason is None:
                continue
            scanned += 1
            by_reason[reason] += 1
            if apply:
                quarantined += 1
    return LegacyRecoveryRepairReport(
        mode="apply" if apply else "dry_run",
        scanned=scanned,
        would_quarantine=scanned,
        quarantined=quarantined,
        by_reason=dict(by_reason),
    )


__all__ = [
    "LegacyRecoveryRepairReport",
    "quarantine_legacy_recovery_manifest",
    "repair_legacy_recovery_manifests",
]
