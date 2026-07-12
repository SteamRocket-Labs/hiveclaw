#!/usr/bin/env python3
"""Apply Hive's audited Python 3.12 compatibility patch to lark-oapi.

The official 1.7.1 wheel bundles an old protobuf runtime and initializes an
event loop at import time.  Both paths emit deprecation warnings on Python
3.12.  The SDK doesn't expose a supported hook for either behavior, so Hive
patches the exact pinned release at install time.  Every replacement is
idempotent and fails closed when the upstream source no longer matches.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
import csv
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import tempfile


SUPPORTED_VERSION = "1.7.1"


@dataclass(frozen=True)
class SourcePatch:
    relative_path: str
    original: str
    replacement: str


PATCHES = (
    SourcePatch(
        "ws/pb/google/protobuf/internal/well_known_types.py",
        "_EPOCH_DATETIME_NAIVE = datetime.datetime.utcfromtimestamp(0)",
        "_EPOCH_DATETIME_NAIVE = datetime.datetime.fromtimestamp(0, datetime.UTC).replace(tzinfo=None)",
    ),
    SourcePatch(
        "ws/client.py",
        "from typing import Callable, Dict, Mapping, Optional",
        "from typing import Any, Callable, Dict, Mapping, Optional",
    ),
    SourcePatch(
        "ws/client.py",
        """try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
""",
        """loop = None


def _get_loop():
    global loop
    if loop is not None and not loop.is_closed():
        return loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop
""",
    ),
    SourcePatch(
        "ws/client.py",
        "self._conn: Optional[websockets.WebSocketClientProtocol] = None",
        "self._conn: Optional[Any] = None",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.run_until_complete(self._connect())",
        "_get_loop().run_until_complete(self._connect())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.run_until_complete(self._disconnect())",
        "_get_loop().run_until_complete(self._disconnect())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.run_until_complete(self._reconnect())",
        "_get_loop().run_until_complete(self._reconnect())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.create_task(self._ping_loop())",
        "_get_loop().create_task(self._ping_loop())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.run_until_complete(_select())",
        "_get_loop().run_until_complete(_select())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.create_task(self._receive_message_loop())",
        "_get_loop().create_task(self._receive_message_loop())",
    ),
    SourcePatch(
        "ws/client.py",
        "loop.create_task(self._handle_message(msg))",
        "_get_loop().create_task(self._handle_message(msg))",
    ),
    SourcePatch(
        "core/cache/expiring_cache.py",
        "loop = asyncio.get_event_loop()",
        "loop = asyncio.get_running_loop()",
    ),
)


def apply_patches(package_root: Path) -> int:
    """Patch *package_root* and return the number of changed source files."""
    pending: dict[Path, str] = {}
    for patch in PATCHES:
        path = package_root / patch.relative_path
        source = pending.get(path)
        if source is None:
            source = path.read_text(encoding="utf-8")
        if patch.replacement in source:
            pending[path] = source
            continue
        if patch.original not in source:
            raise RuntimeError(f"lark-oapi compatibility anchor missing in {path}: {patch.original[:80]!r}")
        pending[path] = source.replace(patch.original, patch.replacement, 1)

    changed = 0
    for path, source in pending.items():
        current = path.read_text(encoding="utf-8")
        if current != source:
            path.write_text(source, encoding="utf-8")
            changed += 1
    return changed


def refresh_distribution_record(installed) -> None:
    """Keep wheel RECORD hashes truthful after the audited source patch."""
    record_entry = next(
        (item for item in installed.files or () if str(item).endswith(".dist-info/RECORD")),
        None,
    )
    if record_entry is None:
        raise RuntimeError("lark-oapi installation has no dist-info/RECORD")

    target_paths = {f"lark_oapi/{patch.relative_path}" for patch in PATCHES}
    record_path = Path(installed.locate_file(record_entry))
    with record_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    seen: set[str] = set()
    for row in rows:
        if not row or row[0] not in target_paths:
            continue
        source = Path(installed.locate_file(row[0])).read_bytes()
        digest = urlsafe_b64encode(hashlib.sha256(source).digest()).decode("ascii").rstrip("=")
        row[1:] = [f"sha256={digest}", str(len(source))]
        seen.add(row[0])
    if seen != target_paths:
        missing = ", ".join(sorted(target_paths - seen))
        raise RuntimeError(f"lark-oapi RECORD is missing patched paths: {missing}")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=record_path.parent,
        prefix=f".{record_path.name}.",
        delete=False,
    ) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerows(rows)
        temporary_path = Path(handle.name)
    temporary_path.replace(record_path)


def main() -> None:
    try:
        installed = distribution("lark-oapi")
    except PackageNotFoundError as exc:
        raise SystemExit("lark-oapi must be installed before applying the compatibility patch") from exc
    if installed.version != SUPPORTED_VERSION:
        raise SystemExit(f"unsupported lark-oapi version {installed.version}; expected exactly {SUPPORTED_VERSION}")

    package_root = Path(installed.locate_file("lark_oapi"))
    changed = apply_patches(package_root)
    refresh_distribution_record(installed)
    print(f"lark-oapi {SUPPORTED_VERSION} compatibility verified ({changed} files changed)")


if __name__ == "__main__":
    main()
