from __future__ import annotations

import hashlib
from pathlib import Path
import asyncio

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


@pytest.mark.asyncio
async def test_blob_publication_never_overwrites_a_concurrent_immutable_key(tmp_path):
    from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobPutRequest, FilesystemBlobStore

    store = FilesystemBlobStore(tmp_path / "objects")
    location = BlobLocation(provider="filesystem", bucket="durable", object_key="tenant/immutable")
    requests = []
    for name, body in (("a", b"A" * 1024), ("b", b"B" * 1024)):
        source = tmp_path / name
        source.write_bytes(body)
        requests.append(BlobPutRequest(tenant_id="tenant", kind="attachment", retention_class="original",
            location=location, source_path=source, expected_sha256=hashlib.sha256(body).hexdigest(), expected_size=len(body)))
    results = await asyncio.gather(*(store.put_verified(request) for request in requests), return_exceptions=True)
    successful = [row for row in results if not isinstance(row, BaseException)]
    assert len(successful) == 1 and sum(isinstance(row, BlobIntegrityError) for row in results) == 1
    actual = b"".join([chunk async for chunk in store.open_stream(location)])
    assert hashlib.sha256(actual).hexdigest() == successful[0].sha256


@pytest.mark.asyncio
async def test_blob_provider_refuses_an_in_root_parent_alias(tmp_path):
    from app.services.blob_store import BlobStoreError, BlobLocation, BlobPutRequest, FilesystemBlobStore

    root = tmp_path / "objects"
    retained = root / "durable/retained-owner"
    retained.mkdir(parents=True)
    (root / "durable/tenant").symlink_to(retained, target_is_directory=True)
    source = tmp_path / "source"
    source.write_bytes(b"NEW_BYTES")
    request = BlobPutRequest(tenant_id="tenant", kind="attachment", retention_class="original",
        location=BlobLocation(provider="filesystem", bucket="durable", object_key="tenant/foreign"),
        source_path=source, expected_sha256=hashlib.sha256(b"NEW_BYTES").hexdigest(), expected_size=9)
    with pytest.raises(BlobStoreError):
        await FilesystemBlobStore(root).put_verified(request)
    assert not (retained / "foreign").exists()
