from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_filesystem_blob_store_put_stat_read_delete_is_verified(tmp_path: Path) -> None:
    from app.services.blob_store import BlobLocation, BlobPutRequest, FilesystemBlobStore

    content = b"canonical evidence\n" * 1000
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    store = FilesystemBlobStore(tmp_path / "objects")
    location = BlobLocation(provider="filesystem", bucket="durable", object_key=f"tenant-a/{digest}")

    receipt = await store.put_verified(
        BlobPutRequest(
            tenant_id="tenant-a",
            kind="t0_source",
            retention_class="canonical_archive",
            location=location,
            source_path=source,
            expected_sha256=digest,
            expected_size=len(content),
        )
    )
    replay = await store.put_verified(
        BlobPutRequest(
            tenant_id="tenant-a",
            kind="t0_source",
            retention_class="canonical_archive",
            location=location,
            source_path=source,
            expected_sha256=digest,
            expected_size=len(content),
        )
    )
    stat = await store.stat(location)
    restored = b"".join([chunk async for chunk in store.open_stream(location)])

    assert receipt.sha256 == digest
    assert replay.idempotent_replay is True
    assert stat.available is True and stat.size_bytes == len(content)
    assert restored == content

    await store.delete(location, gc_receipt_id="gc-receipt-1")
    assert (await store.stat(location)).available is False


@pytest.mark.asyncio
async def test_blob_store_rejects_hash_mismatch_and_path_escape(tmp_path: Path) -> None:
    from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobPutRequest, FilesystemBlobStore

    source = tmp_path / "source.bin"
    source.write_bytes(b"evidence")
    store = FilesystemBlobStore(tmp_path / "objects")

    with pytest.raises(BlobIntegrityError):
        await store.put_verified(
            BlobPutRequest(
                tenant_id="tenant-a",
                kind="t0_source",
                retention_class="canonical_archive",
                location=BlobLocation(provider="filesystem", bucket="durable", object_key="tenant-a/object"),
                source_path=source,
                expected_sha256="0" * 64,
                expected_size=source.stat().st_size,
            )
        )

    with pytest.raises(ValueError, match="unsafe blob"):
        await store.stat(BlobLocation(provider="filesystem", bucket="durable", object_key="../escape"))
