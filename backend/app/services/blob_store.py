"""Narrow immutable blob provider boundary with verified streaming writes."""

from __future__ import annotations

import asyncio
import hashlib
import stat
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol



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


class FilesystemBlobStore:
    """Immutable local provider using pinned descriptors and no-overwrite publication."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().absolute()

    def _relative(self, location: BlobLocation) -> str:
        bucket = str(location.bucket or "").strip()
        object_key = str(location.object_key or "").strip().replace("\\", "/")
        key = Path(object_key)
        if (location.provider != "filesystem" or not bucket or bucket in {".", ".."}
            or "/" in bucket or "\\" in bucket or not object_key or key.is_absolute()
            or any(part in {"", ".", ".."} for part in key.parts)):
            raise ValueError("unsafe blob location")
        return (Path(bucket) / key).as_posix()

    @contextmanager
    def _directory(self, relative: str, *, create=False):
        from app.services.confined_storage import confined_storage_parent

        try:
            with confined_storage_parent(self.root, relative, create=create) as location:
                yield location
        except OSError as exc:
            raise BlobStoreError("Blob storage is unavailable or aliased") from exc

    @contextmanager
    def _open(self, location):
        with self._directory(self._relative(location)) as (directory, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise BlobStoreError("Blob file has changed or shared ownership")
                yield handle

    def _stat(self, location: BlobLocation) -> BlobStat:
        try:
            with self._open(location) as handle:
                size = os.fstat(handle.fileno()).st_size
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                return BlobStat(location, available=True, size_bytes=size, sha256=digest)
        except BlobStoreError as exc:
            cause = exc
            while cause is not None:
                if isinstance(cause, FileNotFoundError):
                    return BlobStat(location, available=False)
                cause = cause.__cause__
            raise

    def _put(self, request: BlobPutRequest) -> BlobReceipt:
        relative = self._relative(request.location)
        if (request.expected_size < 0 or len(request.expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in request.expected_sha256)):
            raise BlobIntegrityError("Invalid expected blob hash/size")
        existing = self._stat(request.location)
        if existing.available:
            if (existing.size_bytes, existing.sha256) != (request.expected_size, request.expected_sha256):
                raise BlobIntegrityError("Immutable blob collision")
            return BlobReceipt(request.location, request.expected_sha256, request.expected_size, idempotent_replay=True)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with self._directory(relative, create=True) as (directory, name):
                temporary = "." + uuid.uuid4().hex + ".upload"
                try:
                    output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600, dir_fd=directory)
                    with os.fdopen(output_fd, "wb") as output:
                        source_fd = os.open(request.source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                        with os.fdopen(source_fd, "rb") as source:
                            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                                raise BlobIntegrityError("Blob source is not a regular file")
                            digest, size = hashlib.sha256(), 0
                            while chunk := source.read(1024 * 1024):
                                digest.update(chunk)
                                size += len(chunk)
                                output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != request.expected_size or digest.hexdigest() != request.expected_sha256:
                        raise BlobIntegrityError("Blob source verification failed")
                    try:
                        # Linking an exclusively created private staging inode is
                        # atomic and cannot replace an existing immutable key.
                        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        try:
                            winner = self._stat(request.location)
                        except BlobStoreError as exc:
                            raise BlobIntegrityError("Concurrent immutable blob collision") from exc
                        if (winner.size_bytes, winner.sha256) != (size, request.expected_sha256):
                            raise BlobIntegrityError("Concurrent immutable blob collision")
                        return BlobReceipt(request.location, request.expected_sha256, size, idempotent_replay=True)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                        os.fsync(directory)
                    except FileNotFoundError:
                        pass
            return BlobReceipt(request.location, request.expected_sha256, size)
        except OSError as exc:
            raise BlobStoreError("Blob storage is unavailable or changed") from exc

    async def put_verified(self, request: BlobPutRequest) -> BlobReceipt:
        return await asyncio.to_thread(self._put, request)

    def put_verified_sync(self, request: BlobPutRequest) -> BlobReceipt:
        """Use the same verified publication for synchronous snapshot producers."""
        return self._put(request)

    async def stat(self, location: BlobLocation) -> BlobStat:
        return await asyncio.to_thread(self._stat, location)

    async def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]:
        try:
            with self._open(location) as handle:
                while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                    yield chunk
        except OSError as exc:
            raise BlobStoreError("Blob storage is unavailable or changed") from exc

    async def delete(self, location: BlobLocation, *, gc_receipt_id: str) -> None:
        if not str(gc_receipt_id or "").strip():
            raise BlobStoreError("Blob delete requires a GC receipt id")
        if not (await self.stat(location)).available:
            return
        try:
            with self._directory(self._relative(location)) as (directory, name):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise BlobStoreError("Blob file has changed or shared ownership")
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
        except OSError as exc:
            raise BlobStoreError("Blob storage changed during removal") from exc
