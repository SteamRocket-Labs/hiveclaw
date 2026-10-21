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
from typing import Literal, Protocol



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
    provider_token: str | None = None


@dataclass(frozen=True, slots=True)
class BlobDeleteReceipt:
    outcome: Literal["absent", "changed", "unknown", "denied", "unavailable"]
    effect_attempted: bool = False


class BlobStore(Protocol):
    async def put_verified(self, request: BlobPutRequest) -> BlobReceipt: ...

    async def stat(self, location: BlobLocation, *, expected_size: int | None = None) -> BlobStat: ...

    def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]: ...

    async def stat_for_delete(self, location: BlobLocation, *, expected_size: int) -> BlobStat: ...

    async def delete(self, location: BlobLocation, *, gc_receipt_id: str,
        expected_sha256: str, expected_size: int) -> BlobDeleteReceipt: ...


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

    def _stat(self, location: BlobLocation, *, expected_size=None) -> BlobStat:
        try:
            with self._open(location) as handle:
                size = os.fstat(handle.fileno()).st_size
                if expected_size is not None and size > expected_size:
                    raise BlobIntegrityError("Blob exceeds its authoritative size")
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

    async def stat(self, location: BlobLocation, *, expected_size=None) -> BlobStat:
        return await asyncio.to_thread(self._stat, location, expected_size=expected_size)

    async def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]:
        try:
            with self._open(location) as handle:
                while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                    yield chunk
        except OSError as exc:
            raise BlobStoreError("Blob storage is unavailable or changed") from exc

    def _stat_for_delete(self, location, *, expected_size):
        # A missing mount or parent is unavailable, not evidence that an exact
        # object disappeared. Retained parent directories anchor later readback.
        with self._directory(self._relative(location)):
            return self._stat(location, expected_size=expected_size)

    async def stat_for_delete(self, location, *, expected_size):
        return await asyncio.to_thread(self._stat_for_delete, location, expected_size=expected_size)

    def _delete(self, location, *, gc_receipt_id, expected_sha256, expected_size):
        if not str(gc_receipt_id or "").strip():
            raise BlobStoreError("Blob delete requires a GC receipt id")
        attempted = False
        try:
            with self._directory(self._relative(location)) as (directory, name):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
                except FileNotFoundError:
                    return BlobDeleteReceipt("absent")
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != expected_size
                        or hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256):
                        return BlobDeleteReceipt("changed")
                    def signature(info):
                        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                            info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    # Reopen the server-selected namespace before unlinking;
                    # immutable private-directory writers cannot replace keys.
                    with self._directory(self._relative(location)) as (check, _):
                        if (os.fstat(check).st_dev, os.fstat(check).st_ino) != (os.fstat(directory).st_dev, os.fstat(directory).st_ino):
                            return BlobDeleteReceipt("changed")
                    if signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != signature(before):
                        return BlobDeleteReceipt("changed")
                    attempted = True
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    try:
                        os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        return BlobDeleteReceipt("unknown", True)
            return BlobDeleteReceipt("absent", True)
        except (OSError, BlobStoreError):
            return BlobDeleteReceipt("unknown" if attempted else "unavailable", attempted)

    async def delete(self, location, *, gc_receipt_id, expected_sha256, expected_size):
        return await asyncio.to_thread(self._delete, location, gc_receipt_id=gc_receipt_id,
            expected_sha256=expected_sha256, expected_size=expected_size)
