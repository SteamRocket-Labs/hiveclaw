"""A remaining source-authority wait fails visibly instead of reaching the gateway timeout."""

import asyncio
import time

import pytest

from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("endpoint", ["download", "content"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_artifact_authority_wait_returns_retryable_503(
    delivered, owner_sessionmaker, monkeypatch, endpoint, legacy  # noqa: F811
):
    from app.core import resource_authority
    from app.models.chat_artifact import ChatArtifact
    from app.services import chat_artifact_delivery

    d = delivered
    if legacy:
        async with owner_sessionmaker() as db:
            artifact = await db.get(ChatArtifact, d.first.id)
            artifact.snapshot_json = {key: value for key, value in artifact.snapshot_json.items()
                if key != "snapshot_storage_path"}
            await db.commit()
    original = resource_authority.authorize_resource_action
    completed, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_after_authorization(*args, **kwargs):
        await original(*args, **kwargs)
        completed.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(resource_authority, "authorize_resource_action", wait_after_authorization)
    monkeypatch.setattr(chat_artifact_delivery, "ARTIFACT_READ_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    response = await asyncio.wait_for(d.client.get(d.url + "/" + endpoint), timeout=1)
    assert response.status_code == 503, response.text
    assert completed.is_set() and cancelled.is_set()
    assert response.json()["detail"] == {"code": "artifact_read_unavailable", "retryable": True,
        "message": "文件暂时无法读取，请稍后重试。"}
    assert time.monotonic() - started < 1
    assert "AAAA" not in response.text
