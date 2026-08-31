#!/usr/bin/env python3
"""Reject unapproved legacy-brand bytes from a release snapshot."""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import tarfile
from typing import Mapping


ROOT = Path(__file__).resolve().parents[2]
LEGACY_BRAND = ("clawi" + "th").encode("ascii")
LEGACY_UPPER = b"CLAW" + b"ITH"
LEGACY_INDEX = LEGACY_UPPER + b"_PIP_INDEX_URL"
LEGACY_HOST = LEGACY_UPPER + b"_PIP_TRUSTED_HOST"
PERSONAL_RELEASE_MARKERS = tuple(
    marker.encode("utf-8")
    for marker in (
        "rocky" + "243",
        "@hiveclaw" + "243",
        "翁" + "吉义",
        "王" + "天怡",
        "慕" + "涵",
        "吴" + "桐",
        "Tong" + " Wu",
        "苏" + "晴",
        "Qing" + " Su",
    )
)

# Exact lines, not occurrence counts: an approved compatibility identifier
# cannot be moved into user-visible copy while preserving the same count.
# Release compatibility approvals are intentionally narrow:
# - old package-mirror environment names remain input-only fallbacks while Hive names win;
# - the historical KDF salt remains byte-stable so existing ciphertext stays decryptable; and
# - the browser theme key remains only for a one-time move to the current storage key.
APPROVED_COMPATIBILITY_LINES: dict[str, tuple[bytes, ...]] = {
    "Dockerfile": (
        b"ARG " + LEGACY_INDEX,
        b"ARG " + LEGACY_HOST,
        b'RUN EFFECTIVE_PIP_INDEX_URL="${HIVE_PIP_INDEX_URL:-$' + LEGACY_INDEX + b'}"; \\',
        b'    EFFECTIVE_PIP_TRUSTED_HOST="${HIVE_PIP_TRUSTED_HOST:-$' + LEGACY_HOST + b'}"; \\',
    ),
    "backend/Dockerfile": (
        b"ARG " + LEGACY_INDEX,
        b"ARG " + LEGACY_HOST,
        b'RUN EFFECTIVE_PIP_INDEX_URL="${HIVE_PIP_INDEX_URL:-$' + LEGACY_INDEX + b'}"; \\',
        b'    EFFECTIVE_PIP_TRUSTED_HOST="${HIVE_PIP_TRUSTED_HOST:-$' + LEGACY_HOST + b'}"; \\',
    ),
    "backend/app/services/secrets_provider.py": (b'_KDF_SALT = b"' + LEGACY_BRAND + b'-secrets-v1"',),
    "backend/tests/services/test_secrets_provider.py": (b'    historical_salt = b"' + LEGACY_BRAND + b'-secrets-v1"',),
    "docker-compose.yml": (
        b"        " + LEGACY_INDEX + b": ${" + LEGACY_INDEX + b":-}",
        b"        " + LEGACY_HOST + b": ${" + LEGACY_HOST + b":-}",
    ),
    "frontend/src/utils/theme.legacy.test.ts": (b"const LEGACY_KEY = '" + LEGACY_BRAND + b"-accent-color';",),
    "frontend/src/utils/theme.ts": (b"const LEGACY_KEY = '" + LEGACY_BRAND + b"-accent-color';",),
    "setup.sh": (
        b'EFFECTIVE_PIP_INDEX_URL="${HIVE_PIP_INDEX_URL:-${' + LEGACY_INDEX + b':-}}"',
        b'EFFECTIVE_PIP_TRUSTED_HOST="${HIVE_PIP_TRUSTED_HOST:-${' + LEGACY_HOST + b':-}}"',
    ),
}


def working_tree_snapshot(root: Path = ROOT) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    snapshot: dict[str, bytes] = {}
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = os.fsdecode(raw_path)
        candidate = root / relative
        if not os.path.lexists(candidate):
            continue
        if candidate.is_symlink():
            payload = os.fsencode(os.readlink(candidate))
        elif candidate.is_file():
            payload = candidate.read_bytes()
        else:
            payload = b""
        snapshot[PurePosixPath(relative).as_posix()] = payload
    return snapshot


def git_archive_snapshot(ref: str, root: Path = ROOT) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=root,
        check=True,
        capture_output=True,
    )
    snapshot: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name).as_posix()
            if member.isfile():
                extracted = archive.extractfile(member)
                snapshot[name] = extracted.read() if extracted is not None else b""
            elif member.issym() or member.islnk():
                snapshot[name] = os.fsencode(member.linkname)
            elif member.isdir():
                snapshot[name] = b""
    return snapshot


def release_errors(snapshot: Mapping[str, bytes]) -> list[str]:
    errors: list[str] = []
    needle = LEGACY_BRAND.lower()
    observed: dict[str, tuple[bytes, ...]] = {}
    for path, payload in snapshot.items():
        if needle.decode("ascii") in path.casefold():
            errors.append(f"legacy brand appears in release path: {path}")
        matching_lines = tuple(line for line in payload.splitlines() if needle in line.lower())
        if matching_lines:
            observed[path] = matching_lines
    if observed != APPROVED_COMPATIBILITY_LINES:
        observed_paths = set(observed)
        approved_paths = set(APPROVED_COMPATIBILITY_LINES)
        errors.append(
            "legacy compatibility lines differ from the approved release set: "
            f"unexpected_paths={sorted(observed_paths - approved_paths)!r}, "
            f"missing_paths={sorted(approved_paths - observed_paths)!r}, "
            f"changed_paths={sorted(path for path in observed_paths & approved_paths if observed[path] != APPROVED_COMPATIBILITY_LINES[path])!r}"
        )

    required_current_inputs = {
        "Dockerfile": (b"ARG HIVE_PIP_INDEX_URL", b"ARG HIVE_PIP_TRUSTED_HOST"),
        "backend/Dockerfile": (b"ARG HIVE_PIP_INDEX_URL", b"ARG HIVE_PIP_TRUSTED_HOST"),
        "docker-compose.yml": (
            b"        HIVE_PIP_INDEX_URL: ${HIVE_PIP_INDEX_URL:-}",
            b"        HIVE_PIP_TRUSTED_HOST: ${HIVE_PIP_TRUSTED_HOST:-}",
        ),
    }
    for path, required in required_current_inputs.items():
        payload = snapshot.get(path, b"")
        for expected in required:
            if expected not in payload:
                errors.append(f"{path} is missing current Hive package-mirror input {expected!r}")

    license_text = snapshot.get("LICENSE", b"")
    if b"Copyright 2025 DataElem Inc." not in license_text:
        errors.append("upstream license attribution is missing")
    return errors


def personal_identifier_errors(snapshot: Mapping[str, bytes]) -> list[str]:
    errors: list[str] = []
    for path, payload in snapshot.items():
        encoded_path = path.casefold().encode("utf-8")
        folded_payload = payload.lower()
        for marker in PERSONAL_RELEASE_MARKERS:
            if marker.lower() in encoded_path or marker.lower() in folded_payload:
                errors.append(f"personal identifier appears in release archive: {path}")
                break
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("working-tree", help="scan tracked and untracked release-candidate files")
    archive = subparsers.add_parser("git-archive", help="scan the exact archive bytes for a committed Git ref")
    archive.add_argument("ref", nargs="?", default="HEAD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = working_tree_snapshot() if args.command == "working-tree" else git_archive_snapshot(args.ref)
    errors = release_errors(snapshot)
    if args.command == "git-archive":
        errors.extend(personal_identifier_errors(snapshot))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"release hygiene gate passed ({args.command}, {len(snapshot)} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
