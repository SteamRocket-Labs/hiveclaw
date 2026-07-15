"""Narrow immutable blob provider boundary with verified streaming writes."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aiofiles


class BlobStoreError(RuntimeError):
    """Base provider boundary failure."""


class BlobIntegrityError(BlobStoreError):
    """Provider content did not match the authoritative hash/size contract."""


@dataclass(frozen=True, slots=True)
class BlobLocation:
    provider: str
    bucket: str
    object_key: str


@dataclass(frozen=True, slots=True)
class BlobPutRequest:
    tenant_id: str
    kind: str
    retention_class: str
    location: BlobLocation
    source_path: Path
    expected_sha256: str
    expected_size: int
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class BlobReceipt:
    location: BlobLocation
    sha256: str
    size_bytes: int
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class BlobStat:
    location: BlobLocation
    available: bool
    size_bytes: int | None = None
    sha256: str | None = None


class BlobStore(Protocol):
    async def put_verified(self, request: BlobPutRequest) -> BlobReceipt: ...

    async def stat(self, location: BlobLocation) -> BlobStat: ...

    def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]: ...

    async def delete(self, location: BlobLocation, *, gc_receipt_id: str) -> None: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FilesystemBlobStore:
    """Local/test adapter that preserves the same immutable provider contract."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, location: BlobLocation) -> Path:
        bucket = str(location.bucket or "").strip()
        object_key = str(location.object_key or "").strip().replace("\\", "/")
        key = Path(object_key)
        if (
            not bucket
            or bucket in {".", ".."}
            or "/" in bucket
            or "\\" in bucket
            or not object_key
            or key.is_absolute()
            or any(part in {"", ".", ".."} for part in key.parts)
        ):
            raise ValueError("unsafe blob location")
        target = (self.root / bucket / key).resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("unsafe blob location") from exc
        return target

    async def put_verified(self, request: BlobPutRequest) -> BlobReceipt:
        source = Path(request.source_path).resolve()
        if not source.is_file():
            raise BlobStoreError(f"blob source is not a file: {source}")
        if request.expected_size < 0 or len(request.expected_sha256) != 64:
            raise BlobIntegrityError("invalid expected blob hash/size")
        target = self._path(request.location)
        if target.exists():
            size = target.stat().st_size
            digest = _sha256_file(target)
            if size != request.expected_size or digest != request.expected_sha256:
                raise BlobIntegrityError(f"immutable blob collision: {request.location.object_key}")
            return BlobReceipt(request.location, digest, size, idempotent_replay=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.upload")
        digest = hashlib.sha256()
        size = 0
        try:
            async with aiofiles.open(source, "rb") as input_handle, aiofiles.open(temp, "wb") as output_handle:
                while chunk := await input_handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    await output_handle.write(chunk)
                await output_handle.flush()
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            actual_digest = digest.hexdigest()
            if size != request.expected_size or actual_digest != request.expected_sha256:
                raise BlobIntegrityError(
                    f"blob source verification failed: expected={request.expected_size}/{request.expected_sha256} "
                    f"actual={size}/{actual_digest}"
                )
            os.replace(temp, target)
            _fsync_directory(target.parent)
            return BlobReceipt(request.location, actual_digest, size)
        finally:
            temp.unlink(missing_ok=True)

    async def stat(self, location: BlobLocation) -> BlobStat:
        target = self._path(location)
        if not target.is_file():
            return BlobStat(location, available=False)
        return BlobStat(location, available=True, size_bytes=target.stat().st_size, sha256=_sha256_file(target))

    async def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]:
        target = self._path(location)
        if not target.is_file():
            raise BlobStoreError(f"blob unavailable: {location.object_key}")
        async with aiofiles.open(target, "rb") as handle:
            while chunk := await handle.read(1024 * 1024):
                yield chunk

    async def delete(self, location: BlobLocation, *, gc_receipt_id: str) -> None:
        if not str(gc_receipt_id or "").strip():
            raise BlobStoreError("blob delete requires a GC receipt id")
        target = self._path(location)
        target.unlink(missing_ok=True)
        if target.parent.exists():
            _fsync_directory(target.parent)
