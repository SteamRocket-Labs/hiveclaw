"""Source-read reuse must expire with its disclosure and retain exact readers."""

import asyncio
from contextlib import nullcontext
import uuid
import weakref

import pytest


class _Reader:
    __slots__ = ("id", "__weakref__")

    def __init__(self):
        self.id = uuid.uuid4()


@pytest.fixture
def source_read():
    from app.services import content_input_receipts as receipts

    return (
        receipts,
        uuid.uuid4(),
        {
            "source_kind": "workspace_artifact",
            "source_id": str(uuid.uuid4()),
            "source_version": "a" * 64,
        },
    )


@pytest.mark.parametrize("raise_on_exit", [False, True])
async def test_inherited_read_revalidates_after_disclosure_exit(source_read, monkeypatch, raise_on_exit):
    receipts, tenant, source = source_read
    db, reader = _Reader(), _Reader()
    release = asyncio.Event()
    allowed, calls = True, 0

    async def authorize(*args, **kwargs):
        nonlocal calls
        calls += 1
        if not allowed:
            raise receipts.ContentInputReceiptError("content_source_access_denied", "Source access revoked.", 403)

    async def delayed_read():
        await release.wait()
        await receipts._validate_input_dependencies(db, tenant, [source], user=reader)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", authorize)
    with pytest.raises(RuntimeError, match="disclosure interrupted") if raise_on_exit else nullcontext():
        with receipts.group_source_read():
            await receipts._validate_input_dependencies(db, tenant, [source], user=reader)
            child = asyncio.create_task(delayed_read())
            if raise_on_exit:
                raise RuntimeError("disclosure interrupted")
    allowed = False
    release.set()
    with pytest.raises(receipts.ContentInputReceiptError) as inherited:
        await child
    assert inherited.value.code == "content_source_access_denied"
    assert inherited.value.status_code == 403
    with pytest.raises(receipts.ContentInputReceiptError) as fresh:
        await receipts._validate_input_dependencies(db, tenant, [source], user=reader)
    assert fresh.value.code == inherited.value.code
    assert fresh.value.status_code == inherited.value.status_code
    assert calls == 3


@pytest.mark.parametrize("reader_kind", ["user", "runtime_request"])
async def test_source_read_keeps_reader_identities_alive_only_until_exit(source_read, monkeypatch, reader_kind):
    receipts, tenant, source = source_read

    async def authorize(*args, **kwargs):
        pass

    async def validate_transient_reader():
        db, reader = _Reader(), _Reader()
        await receipts._validate_input_dependencies(db, tenant, [source], **{reader_kind: reader})
        return weakref.ref(db), weakref.ref(reader)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", authorize)
    with receipts.group_source_read():
        references = await validate_transient_reader()
        # Integer identity keys cannot outlive the objects whose identities they reuse.
        assert all(reference() is not None for reference in references)
    assert all(reference() is None for reference in references)


async def test_active_children_reuse_exact_checks_and_nested_disclosures_are_independent(source_read, monkeypatch):
    receipts, tenant, source = source_read
    db, reader = _Reader(), _Reader()
    calls = 0

    async def authorize(*args, **kwargs):
        nonlocal calls
        calls += 1

    async def read():
        await receipts._validate_input_dependencies(db, tenant, [source], user=reader)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", authorize)
    with receipts.group_source_read():
        await read()
        await asyncio.create_task(read())
        assert calls == 1
        with receipts.group_source_read():
            await read()
            await read()
            assert calls == 2
        await read()
        assert calls == 2
    await read()
    assert calls == 3


async def test_disclosure_expiring_during_validation_cannot_cache_the_late_success(source_read, monkeypatch):
    receipts, tenant, source = source_read
    db, reader = _Reader(), _Reader()
    started, release = asyncio.Event(), asyncio.Event()
    allowed, calls = True, 0

    async def authorize(*args, **kwargs):
        nonlocal calls
        calls += 1
        if not allowed:
            raise receipts.ContentInputReceiptError("content_source_access_denied", "Source access revoked.", 403)
        started.set()
        await release.wait()

    async def read_twice():
        await receipts._validate_input_dependencies(db, tenant, [source], user=reader)
        with pytest.raises(receipts.ContentInputReceiptError) as second:
            await receipts._validate_input_dependencies(db, tenant, [source], user=reader)
        assert second.value.code == "content_source_access_denied"
        assert second.value.status_code == 403

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", authorize)
    with receipts.group_source_read():
        child = asyncio.create_task(read_twice())
        await started.wait()
    allowed = False
    release.set()
    await child
    assert calls == 2
